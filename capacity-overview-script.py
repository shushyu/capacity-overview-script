import json, subprocess, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# -------------------------------------------------------------
# DATEN HOLEN
# -------------------------------------------------------------

def oc_json(args):
    return json.loads(subprocess.check_output(
        ['oc'] + args + ['-o', 'json'], stderr=subprocess.DEVNULL))

def oc_text(args):
    return subprocess.check_output(
        ['oc'] + args, stderr=subprocess.DEVNULL).decode()

nodejson = oc_json(['get', 'nodes'])
podjson  = oc_json(['get', 'pods', '-A'])
top_raw  = oc_text(['adm', 'top', 'nodes', '--no-headers'])

now     = datetime.now().strftime('%d.%m.%Y %H:%M %Z')
cluster = subprocess.check_output(
    ['oc', 'whoami', '--show-server'], stderr=subprocess.DEVNULL
).decode().strip().replace('https://api.', '').split(':')[0]

# -------------------------------------------------------------
# PARSER
# -------------------------------------------------------------

def pc(v):
    if not v: return 0.0
    v = str(v)
    if v.endswith('m'): return int(v[:-1]) / 1000
    try: return float(v)
    except: return 0.0

def pm_gb(v):
    if not v: return 0.0
    v = str(v)
    if v.endswith('Ki'): return int(v[:-2]) / 1024 / 1024
    if v.endswith('Mi'): return int(v[:-2]) / 1024
    if v.endswith('Gi'): return float(v[:-2])
    if v.endswith('Ti'): return float(v[:-2]) * 1024
    try:
        f = float(v)
        if f > 1_000_000_000: return f / 1024**3
        if f > 1_000_000:     return f / 1024**2
        return f
    except: return 0.0

def pm_mb(v):
    return pm_gb(v) * 1024

def parse_top_line(line):
    parts = line.split()
    if len(parts) < 5: return None
    name = parts[0]
    cpu_used = pc(parts[1])
    mem_used_mi = int(parts[3].rstrip('Mi')) if parts[3].endswith('Mi') else pm_mb(parts[3])
    return name, cpu_used, mem_used_mi

# -------------------------------------------------------------------
# Usage pro Node (oc adm top nodes)
# -------------------------------------------------------------------

node_usage_cpu = {}
node_usage_mem = {}

for line in top_raw.strip().splitlines():
    parsed = parse_top_line(line)
    if parsed:
        name, cu, mu = parsed
        node_usage_cpu[name] = cu
        node_usage_mem[name] = mu

# -------------------------------------------------------------------
# Requests pro Node
# -------------------------------------------------------------------

nodes = nodejson['items']
node_count = len(nodes)

node_req_cpu = defaultdict(float)
node_req_mem = defaultdict(float)

running = [p for p in podjson['items'] if p['status'].get('phase') == 'Running']

for p in running:
    node_name = p['spec'].get('nodeName', '')
    for c in p['spec'].get('containers', []):
        res = c.get('resources', {}).get('requests', {})
        node_req_cpu[node_name] += pc(res.get('cpu', '0'))
        node_req_mem[node_name] += pm_mb(res.get('memory', '0'))

# -------------------------------------------------------------------
# Node-Rows
# -------------------------------------------------------------------

node_rows = []
t_cpu_a = t_cpu_r = t_cpu_u = 0.0
t_mem_a = t_mem_r = t_mem_u = 0.0

for n in sorted(nodes, key=lambda x: x['metadata']['name']):
    name = n['metadata']['name']
    cpu_a = pc(n['status']['allocatable']['cpu'])
    mem_a_mb = pm_mb(n['status']['allocatable']['memory'])

    roles = sorted([k.replace('node-role.kubernetes.io/', '')
                    for k in n['metadata'].get('labels', {})
                    if k.startswith('node-role.kubernetes.io/')])
    role = ', '.join(roles) if roles else '-'

    cr = node_req_cpu.get(name, 0)
    mr = node_req_mem.get(name, 0)
    cu = node_usage_cpu.get(name, 0)
    mu = node_usage_mem.get(name, 0)

    t_cpu_a += cpu_a; t_cpu_r += cr; t_cpu_u += cu
    t_mem_a += mem_a_mb; t_mem_r += mr; t_mem_u += mu

    node_rows.append({
        'name': name, 'role': role,
        'cpu_alloc': cpu_a, 'cpu_req': cr, 'cpu_use': cu,
        'cpu_req_pct': cr / cpu_a * 100 if cpu_a > 0 else 0,
        'cpu_use_pct': cu / cpu_a * 100 if cpu_a > 0 else 0,
        'mem_alloc_mb': mem_a_mb, 'mem_req_mb': mr, 'mem_use_mb': mu,
        'mem_req_pct': mr / mem_a_mb * 100 if mem_a_mb > 0 else 0,
        'mem_use_pct': mu / mem_a_mb * 100 if mem_a_mb > 0 else 0,
    })

# Cluster totals
pod_count = len(running)
cpu_alloc = t_cpu_a; cpu_req = t_cpu_r; cpu_use = t_cpu_u
mem_alloc_gb = t_mem_a / 1024; mem_req_gb = t_mem_r / 1024; mem_use_gb = t_mem_u / 1024
cpu_req_pct = cpu_req / cpu_alloc * 100 if cpu_alloc > 0 else 0
cpu_use_pct = cpu_use / cpu_alloc * 100 if cpu_alloc > 0 else 0
mem_req_pct = mem_req_gb / mem_alloc_gb * 100 if mem_alloc_gb > 0 else 0
mem_use_pct = mem_use_gb / mem_alloc_gb * 100 if mem_alloc_gb > 0 else 0

# Namespace aggregation
ns = defaultdict(lambda: {'cr': 0, 'cl': 0, 'mr': 0, 'ml': 0, 'p': 0})
for p in running:
    nsn = p['metadata']['namespace']
    ns[nsn]['p'] += 1
    for c in p['spec'].get('containers', []):
        res = c.get('resources', {})
        ns[nsn]['cr'] += pc(res.get('requests', {}).get('cpu', '0'))
        ns[nsn]['cl'] += pc(res.get('limits', {}).get('cpu', '0'))
        ns[nsn]['mr'] += pm_gb(res.get('requests', {}).get('memory', '0'))
        ns[nsn]['ml'] += pm_gb(res.get('limits', {}).get('memory', '0'))

ns_rows = sorted(ns.items(), key=lambda x: -x[1]['cr'])
ns_rows = [(name, v) for name, v in ns_rows if v['cr'] >= 0.01 or v['p'] >= 2][:10]

# ===================================================================
# HTML
# ===================================================================

def bar(pct, width=80):
    if pct < 60:    color = '#4caf50'
    elif pct < 80:  color = '#ff9800'
    else:           color = '#f44336'
    w = min(pct, 100)
    return (f'<div style="display:inline-block;width:{width}px;height:12px;'
            f'background:#e0e0e0;border-radius:2px;vertical-align:middle;">'
            f'<div style="width:{w:.0f}%;height:100%;background:{color};'
            f'border-radius:2px;"></div></div>')

TBL  = 'border-collapse:collapse;width:100%;font-family:Consolas,monospace;font-size:13px;margin-bottom:20px;'
TBLH = 'border-collapse:collapse;font-family:Consolas,monospace;font-size:13px;margin-bottom:12px;'
TH   = 'background:#f4f4f4;border:1px solid #ccc;padding:6px 10px;text-align:left;font-weight:bold;font-size:11px;white-space:nowrap;'
THR  = TH.replace('text-align:left','text-align:right')
THC  = TH.replace('text-align:left','text-align:center')
TD   = 'border:1px solid #ddd;padding:5px 10px;text-align:left;white-space:nowrap;'
TDR  = TD.replace('text-align:left','text-align:right')
TD2  = TD  + 'background:#fafafa;'
TDR2 = TDR + 'background:#fafafa;'

H1   = 'font-family:Arial,sans-serif;font-size:22px;font-weight:bold;margin:0 0 2px;'
DAT  = 'font-family:Arial,sans-serif;font-size:14px;color:#666;margin:0 0 20px;'
H2   = 'font-family:Arial,sans-serif;font-size:16px;font-weight:bold;margin:24px 0 6px;padding-bottom:4px;'
H3   = 'font-family:Arial,sans-serif;font-size:14px;font-weight:bold;margin:16px 0 4px;color:#333;'
META = 'font-family:Arial,sans-serif;font-size:13px;color:#666;margin:4px 0 8px;'
LEG  = 'font-family:Arial,sans-serif;font-size:11px;color:#888;margin:2px 0 16px;'

o = []; a = o.append    #hier entsteht das HTML-Dokument. o ist eine Liste und alles wird mit o.append bzw a in die Liste geschrieben.
a('<div style="max-width:1400px;">')
a(f'<h1 style="{H1}">{cluster}</h1>')
a(f'<p style="{DAT}">{now}</p>')

# ===================================================================
# NODES (CPU + RAM, kein Disk)
# ===================================================================
a(f'<h2 style="{H2}">NODES ({node_count})</h2>')
a(f'<p style="{LEG}">'
  f'<b>Req</b> = Reserviert (Requests) | '
  f'<b>Use</b> = Tatsächlich genutzt | '
  f'<b>Alloc</b> = Verfügbar (Allocatable) |  '
  f'Balken: '
  f'<span style="color:#4caf50;">&#9632;</span> &lt;60% '
  f'<span style="color:#ff9800;">&#9632;</span> 60-80% '
  f'<span style="color:#f44336;">&#9632;</span> &gt;80%'
  f'</p>')

a(f'<table style="{TBL}">')
a(f'<tr>'
  f'<th style="{TH}" rowspan="2">Node</th>'
  f'<th style="{TH}" rowspan="2">Rolle</th>'
  f'<th style="{THC}" colspan="5">CPU (Cores)</th>'
  f'<th style="{THC}" colspan="5">RAM (MB)</th>'
  f'</tr>')
a(f'<tr>'
  f'<th style="{THR}">Alloc</th><th style="{THR}">Req</th><th style="{THR}">Req %</th>'
  f'<th style="{THR}">Use</th><th style="{THR}">Use %</th>'
  f'<th style="{THR}">Alloc</th><th style="{THR}">Req</th><th style="{THR}">Req %</th>'
  f'<th style="{THR}">Use</th><th style="{THR}">Use %</th>'
  f'</tr>')

for i, r in enumerate(node_rows):
    s = TD2 if i%2 else TD; sr = TDR2 if i%2 else TDR
    a(f'<tr>'
      f'<td style="{s}">{r["name"]}</td>'
      f'<td style="{s}">{r["role"]}</td>'
      f'<td style="{sr}">{r["cpu_alloc"]:.1f}</td>'
      f'<td style="{sr}">{r["cpu_req"]:.1f}</td>'
      f'<td style="{sr}">{r["cpu_req_pct"]:.0f}% {bar(r["cpu_req_pct"],60)}</td>'
      f'<td style="{sr}">{r["cpu_use"]:.1f}</td>'
      f'<td style="{sr}">{r["cpu_use_pct"]:.0f}% {bar(r["cpu_use_pct"],60)}</td>'
      f'<td style="{sr}">{r["mem_alloc_mb"]:.0f}</td>'
      f'<td style="{sr}">{r["mem_req_mb"]:.0f}</td>'
      f'<td style="{sr}">{r["mem_req_pct"]:.0f}% {bar(r["mem_req_pct"],60)}</td>'
      f'<td style="{sr}">{r["mem_use_mb"]:.0f}</td>'
      f'<td style="{sr}">{r["mem_use_pct"]:.0f}% {bar(r["mem_use_pct"],60)}</td>'
      f'</tr>')
a('</table>')

# ===================================================================
# CLUSTER-SUMMEN  drei separate Mini-Tabellen
# ===================================================================
a('<hr style="border:none;border-top:2px dotted #ccc;margin:24px 0;">')
a(f'<h2 style="{H2}">CLUSTER-SUMMEN</h2>')
a(f'<p style="{META}">Nodes: {node_count} &middot; Pods (Running): {pod_count}</p>')

def mini_table(rows):
    """rows = list of (label, value, total, pct)"""
    a(f'<table style="{TBLH}width:100%;">')
    a(f'<tr><th style="{TH}">Kennzahl</th><th style="{THR}">Wert</th>'
      f'<th style="{THR}">von Gesamt</th><th style="{THR}">%</th></tr>')
    for i, (label, val, total, pct) in enumerate(rows):
        s = TD2 if i%2 else TD; sr = TDR2 if i%2 else TDR
        pct_cell = f'{pct:.0f}% {bar(pct,120)}' if pct > 0 else ''
        total_cell = total if total else ''
        a(f'<tr><td style="{s}">{label}</td><td style="{sr}">{val}</td>'
          f'<td style="{sr}">{total_cell}</td><td style="{sr}">{pct_cell}</td></tr>')
    a('</table>')

a('<div style="display:flex;gap:24px;">')

a('<div style="flex:1;min-width:400px;">')
a(f'<h3 style="{H3}">CPU (Cores)</h3>')
mini_table([
    ('Allocatable',           f'{cpu_alloc:.1f}', '', 0),
    ('Requests (reserviert)', f'{cpu_req:.1f}', f'{cpu_alloc:.1f}', cpu_req_pct),
    ('Usage (tatsächlich)',   f'{cpu_use:.1f}', f'{cpu_alloc:.1f}', cpu_use_pct),
    ('Frei (Alloc &minus; Req)', f'{cpu_alloc-cpu_req:.1f}', '', 0),
])
a('</div>')

a('<div style="flex:1;min-width:400px;">')
a(f'<h3 style="{H3}">RAM (GB)</h3>')
mini_table([
    ('Allocatable',           f'{mem_alloc_gb:.1f}', '', 0),
    ('Requests (reserviert)', f'{mem_req_gb:.1f}', f'{mem_alloc_gb:.1f}', mem_req_pct),
    ('Usage (tatsächlich)',   f'{mem_use_gb:.1f}', f'{mem_alloc_gb:.1f}', mem_use_pct),
    ('Frei (Alloc &minus; Req)', f'{mem_alloc_gb-mem_req_gb:.1f}', '', 0),
])
a('</div>')

a('</div>')

# ===================================================================
# TOP 10 HAMSTER-NAMESPACES (größte Differenz Requests vs. Usage)
# ===================================================================
a('<hr style="border:none;border-top:2px dotted #ccc;margin:24px 0;">')
a(f'<h2 style="{H2}">TOP 10 NAMESPACES & TOP 10 HAMSTER </h2>')
a(f'<p style="{LEG}">Namespaces mit der größten Differenz zwischen reservierten und tatsächlich genutzten Ressourcen. Hohe Werte = Optimierungspotenzial.</p>')

try:
    top_pods_raw = oc_text(['adm', 'top', 'pods', '-A', '--no-headers'])
    ns_usage_cpu = defaultdict(float)
    ns_usage_mem = defaultdict(float)
    for line in top_pods_raw.strip().splitlines():
        parts = line.split()
        if len(parts) < 4: continue
        ns_usage_cpu[parts[0]] += pc(parts[2])
        ns_usage_mem[parts[0]] += pm_gb(parts[3])
except:
    ns_usage_cpu = defaultdict(float)
    ns_usage_mem = defaultdict(float)

# Namespace-Daten anreichern
ns_full = []
for name, v in ns.items():
    if v['cr'] < 0.01 and v['p'] < 2: continue
    cu = ns_usage_cpu.get(name, 0)
    mu = ns_usage_mem.get(name, 0)
    cpu_diff = max(0, v['cr'] - cu)
    mem_diff = max(0, v['mr'] - mu)
    ns_full.append({
        'name': name, 'pods': v['p'],
        'cr': v['cr'], 'cl': v['cl'], 'cu': cu, 'cpu_diff': cpu_diff,
        'mr': v['mr'], 'ml': v['ml'], 'mu': mu, 'mem_diff': mem_diff,
    })

def ns_table(title, data, cols, alloc_ref=None):
    """
    cols = list of (header, key, fmt, bar_key)
    alloc_ref: if set, bars show percentage of this value (e.g. cpu_alloc)
               if None, bars scale relative to max in column
    """
    a(f'<h3 style="{H3}">{title}</h3>')
    a(f'<table style="{TBLH}width:100%;">')
    a('<tr><th style="' + TH + '">#</th><th style="' + TH + '">Namespace</th>'
      + ''.join(f'<th style="{THR}">{c[0]}</th>' for c in cols) + '</tr>')
    if not alloc_ref:
        maxvals = {}
        for c in cols:
            if len(c) > 3 and c[3]:
                maxvals[c[3]] = max((row[c[3]] for row in data[:10]), default=1) or 1
    for i, row in enumerate(data[:10]):
        s = TD2 if i%2 else TD; sr = TDR2 if i%2 else TDR
        cells = ''
        for c in cols:
            val = c[2].format(row[c[1]])
            if len(c) > 3 and c[3]:
                if alloc_ref:
                    pct = row[c[3]] / alloc_ref * 100 if alloc_ref > 0 else 0
                else:
                    pct = row[c[3]] / maxvals[c[3]] * 100 if maxvals[c[3]] > 0 else 0
                val = f'{val} {bar(pct, 60)}'
            cells += f'<td style="{sr}">{val}</td>'
        a(f'<tr><td style="{sr}">{i+1}</td><td style="{s}">{row["name"]}</td>{cells}</tr>')
    a('</table>')

a('<table style="width:100%;border:none;border-collapse:collapse;"><tr>')

a('<td style="width:50%;vertical-align:top;padding-right:12px;border:none;">')
ns_table('Top 10  CPU Requests',
    sorted(ns_full, key=lambda x: -x['cr']),
    [('Pods', 'pods', '{:.0f}', None), ('CPU Req', 'cr', '{:.2f}', 'cr'), ('CPU Lim', 'cl', '{:.2f}', None)],
    alloc_ref=cpu_alloc)
a('</td>')

a('<td style="width:50%;vertical-align:top;padding-left:12px;border:none;">')
ns_table('Top 10  RAM Requests (GB)',
    sorted(ns_full, key=lambda x: -x['mr']),
    [('Pods', 'pods', '{:.0f}', None), ('RAM Req', 'mr', '{:.2f}', 'mr'), ('RAM Lim', 'ml', '{:.2f}', None)],
    alloc_ref=mem_alloc_gb)
a('</td>')

a('</tr></table>')

a('<table style="width:100%;border:none;border-collapse:collapse;"><tr>')

a('<td style="width:50%;vertical-align:top;padding-right:12px;border:none;">')
ns_table('Top 10  CPU Hamster (Req  Use)',
    sorted(ns_full, key=lambda x: -x['cpu_diff']),
    [('CPU Req', 'cr', '{:.2f}', None), ('CPU Use', 'cu', '{:.2f}', None), ('Diff', 'cpu_diff', '{:.2f}', 'cpu_diff')])
a('</td>')

a('<td style="width:50%;vertical-align:top;padding-left:12px;border:none;">')
ns_table('Top 10  RAM Hamster (Req  Use) (GB)',
    sorted(ns_full, key=lambda x: -x['mem_diff']),
    [('RAM Req', 'mr', '{:.2f}', None), ('RAM Use', 'mu', '{:.2f}', None), ('Diff', 'mem_diff', '{:.2f}', 'mem_diff')])
a('</td>')

a('</tr></table>')

print('\n'.join(o))