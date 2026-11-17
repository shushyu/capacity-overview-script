import json, os, subprocess, sys
from collections import defaultdict
from datetime import datetime

# -------------------------------------------------------------
# KUBECONFIG
# kubectl erbt die Umgebung des Skripts -> nimmt automatisch $KUBECONFIG.
# Ist nichts exportiert, wuerde kubectl still auf ~/.kube/config ausweichen.
# Das verhindern wir: ohne export kein Lauf.
# -------------------------------------------------------------

KUBECONFIG = os.environ.get('KUBECONFIG')
if not KUBECONFIG:
    sys.exit('KUBECONFIG nicht gesetzt. Erst: export KUBECONFIG=/pfad/zur/config')
if ':' in KUBECONFIG:
    sys.exit(f'KUBECONFIG enthaelt mehrere Dateien ({KUBECONFIG}). Bitte genau eine exportieren.')
if not os.path.isfile(KUBECONFIG):
    sys.exit(f'KUBECONFIG zeigt auf keine Datei: {KUBECONFIG}')

# -------------------------------------------------------------
# DATEN HOLEN (stderr bleibt sichtbar, landet nicht im HTML)
# -------------------------------------------------------------

def kc_json(args):
    return json.loads(subprocess.check_output(['kubectl'] + args + ['-o', 'json']))

def kc_text(args):
    return subprocess.check_output(['kubectl'] + args).decode()

context = kc_text(['config', 'current-context']).strip()
print(f'Kubeconfig: {KUBECONFIG} | Kontext: {context}', file=sys.stderr)

nodejson = kc_json(['get', 'nodes'])
podjson  = kc_json(['get', 'pods', '-A'])
top_raw  = kc_text(['top', 'nodes', '--no-headers'])   # braucht metrics-server (bei RKE2 dabei)

now = datetime.now().astimezone().strftime('%d.%m.%Y %H:%M %Z')
# RKE2 hat kein "api.<cluster>"-Schema wie OpenShift.
# Clustername = 1. Argument, sonst kubeconfig-Kontext.
cluster = sys.argv[1] if len(sys.argv) > 1 else context

# -------------------------------------------------------------
# PARSER
# -------------------------------------------------------------

def pc(v):
    """CPU-String -> Cores"""
    if not v: return 0.0
    v = str(v)
    try:
        if v.endswith('m'): return float(v[:-1]) / 1000
        return float(v)
    except ValueError: return 0.0

MEM_UNITS = {'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'Ti': 1024**4,
             'k': 1e3, 'K': 1e3, 'M': 1e6, 'G': 1e9, 'T': 1e12}

def pm_gb(v):
    """Memory-String -> GiB. Versteht Ki/Mi/Gi/Ti, k/M/G/T und nackte Bytes."""
    if not v: return 0.0
    v = str(v)
    try:
        for suf, mult in MEM_UNITS.items():
            if v.endswith(suf):
                return float(v[:-len(suf)]) * mult / 1024**3
        return float(v) / 1024**3          # ohne Suffix = Bytes
    except ValueError: return 0.0

def pm_mb(v):
    return pm_gb(v) * 1024

def parse_top_line(line):
    # Spalten: NAME  CPU(cores)  CPU%  MEMORY(bytes)  MEMORY%
    parts = line.split()
    if len(parts) < 5: return None
    return parts[0], pc(parts[1]), pm_mb(parts[3])

# -------------------------------------------------------------------
# Usage pro Node (kubectl top nodes)
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
t_cpu_a = t_cpu_r = t_cpu_u = t_cpu_c = 0.0
t_mem_a = t_mem_r = t_mem_u = t_mem_c = 0.0

for n in sorted(nodes, key=lambda x: x['metadata']['name']):
    name = n['metadata']['name']
    cpu_a = pc(n['status']['allocatable']['cpu'])
    mem_a_mb = pm_mb(n['status']['allocatable']['memory'])
    # Usage (kubectl top) misst den ganzen Node -> gegen capacity rechnen,
    # Requests gegen allocatable (Scheduler-Sicht).
    cpu_c = pc(n['status'].get('capacity', {}).get('cpu', '0')) or cpu_a
    mem_c_mb = pm_mb(n['status'].get('capacity', {}).get('memory', '0')) or mem_a_mb

    labels = n['metadata'].get('labels', {})
    roles = sorted(k.split('/', 1)[1] for k in labels if k.startswith('node-role.kubernetes.io/'))
    role = ', '.join(roles) if roles else 'worker'   # RKE2-Agents haben oft kein Rollen-Label
    # RKE2-Server sind per Default schedulebar. Worker = kein Rollen-Taint mit NoSchedule/NoExecute.
    is_worker = not any(t.get('key', '').startswith('node-role.kubernetes.io/')
                        and t.get('effect') in ('NoSchedule', 'NoExecute')
                        for t in n['spec'].get('taints', []))

    cr = node_req_cpu.get(name, 0)
    mr = node_req_mem.get(name, 0)
    cu = node_usage_cpu.get(name, 0)
    mu = node_usage_mem.get(name, 0)

    if is_worker:
        t_cpu_a += cpu_a; t_cpu_r += cr; t_cpu_u += cu; t_cpu_c += cpu_c
        t_mem_a += mem_a_mb; t_mem_r += mr; t_mem_u += mu; t_mem_c += mem_c_mb

    node_rows.append({
        'name': name, 'role': role, 'is_worker': is_worker,
        'cpu_alloc': cpu_a, 'cpu_req': cr, 'cpu_use': cu,
        'cpu_req_pct': cr / cpu_a * 100 if cpu_a > 0 else 0,
        'cpu_use_pct': cu / cpu_c * 100 if cpu_c > 0 else 0,
        'mem_alloc_mb': mem_a_mb, 'mem_req_mb': mr, 'mem_use_mb': mu,
        'mem_req_pct': mr / mem_a_mb * 100 if mem_a_mb > 0 else 0,
        'mem_use_pct': mu / mem_c_mb * 100 if mem_c_mb > 0 else 0,
    })

worker_count = sum(1 for r in node_rows if r['is_worker'])

# Cluster totals
pod_count = len(running)
cpu_alloc = t_cpu_a; cpu_req = t_cpu_r; cpu_use = t_cpu_u
cpu_cap = t_cpu_c
mem_alloc_gb = t_mem_a / 1024; mem_req_gb = t_mem_r / 1024; mem_use_gb = t_mem_u / 1024
mem_cap_gb = t_mem_c / 1024
cpu_req_pct = cpu_req / cpu_alloc * 100 if cpu_alloc > 0 else 0
cpu_use_pct = cpu_use / cpu_cap * 100 if cpu_cap > 0 else 0
mem_req_pct = mem_req_gb / mem_alloc_gb * 100 if mem_alloc_gb > 0 else 0
mem_use_pct = mem_use_gb / mem_cap_gb * 100 if mem_cap_gb > 0 else 0

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

# ===================================================================
# STYLE (Pastell)
# ===================================================================

C_GREEN  = '#8fd3a4'   # Balken <60%
C_AMBER  = '#ffcf96'   # Balken 60-80%
C_RED    = '#f5a8a8'   # Balken >80%
C_TRACK  = '#eceff5'   # Balken-Hintergrund
C_HEAD   = '#eef2fa'   # Tabellenkopf neutral
C_CPU    = '#e1ecfd'   # Kopf CPU (Pastellblau)
C_RAM    = '#eee5fb'   # Kopf RAM (Pastelllila)
C_STRIPE = '#f8f9fc'   # Zebra
C_LINE   = '#e4e8f0'   # Linien
C_TEXT   = '#3b4252'
C_SUB    = '#5b6478'
C_MUTED  = '#8a93a6'
FONT     = "'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"

def bar(pct, width=80):
    color = C_GREEN if pct < 60 else C_AMBER if pct < 80 else C_RED
    w = min(pct, 100)
    return (f'<div style="display:inline-block;width:{width}px;height:10px;background:{C_TRACK};'
            f'border-radius:5px;overflow:hidden;vertical-align:middle;margin-left:6px;">'
            f'<div style="width:{w:.0f}%;height:100%;background:{color};border-radius:5px;"></div></div>')

# Runde Ecken: geht nur mit border-collapse:separate + overflow:hidden
TBL  = (f'border-collapse:separate;border-spacing:0;width:100%;font-family:{FONT};font-size:13px;'
        f'color:{C_TEXT};font-variant-numeric:tabular-nums;border:1px solid {C_LINE};'
        f'border-radius:12px;overflow:hidden;margin-bottom:20px;')
TBLH = TBL.replace('margin-bottom:20px;', 'margin-bottom:12px;')
TH   = (f'background:{C_HEAD};color:{C_SUB};padding:8px 12px;text-align:left;font-weight:600;'
        f'font-size:12px;white-space:nowrap;')
THR  = TH.replace('text-align:left', 'text-align:right')
THC  = TH.replace('text-align:left', 'text-align:center')
THC_CPU = THC.replace(C_HEAD, C_CPU); THR_CPU = THR.replace(C_HEAD, C_CPU)
THC_RAM = THC.replace(C_HEAD, C_RAM); THR_RAM = THR.replace(C_HEAD, C_RAM)
TD   = f'border-top:1px solid {C_LINE};padding:7px 12px;text-align:left;white-space:nowrap;'
TDR  = TD.replace('text-align:left', 'text-align:right')
TD2  = TD  + f'background:{C_STRIPE};'
TDR2 = TDR + f'background:{C_STRIPE};'

H1   = f'font-family:{FONT};font-size:24px;font-weight:600;color:{C_TEXT};margin:0 0 2px;'
DAT  = f'font-family:{FONT};font-size:13px;color:{C_MUTED};margin:0 0 24px;'
H2   = f'font-family:{FONT};font-size:16px;font-weight:600;color:{C_TEXT};margin:28px 0 6px;'
H3   = f'font-family:{FONT};font-size:14px;font-weight:600;color:{C_SUB};margin:16px 0 6px;'
META = f'font-family:{FONT};font-size:13px;color:{C_MUTED};margin:4px 0 10px;'
LEG  = f'font-family:{FONT};font-size:12px;color:{C_MUTED};margin:2px 0 14px;'
HR   = f'<hr style="border:none;border-top:1px solid {C_LINE};margin:28px 0;">'

def dot(c):
    return f'<span style="display:inline-block;width:10px;height:10px;border-radius:5px;background:{c};vertical-align:middle;"></span>'

# ===================================================================
# HTML
# ===================================================================

o = []; a = o.append    # o sammelt das HTML, a() haengt an
a('<div style="max-width:1400px;">')
a(f'<h1 style="{H1}">{cluster}</h1>')
a(f'<p style="{DAT}">{now}</p>')

# ===================================================================
# NODES
# ===================================================================
a(f'<h2 style="{H2}">Nodes ({node_count})</h2>')
a(f'<p style="{LEG}">'
  f'<b>Req</b> = Reserviert (Requests) | '
  f'<b>Use</b> = Tatsächlich genutzt | '
  f'<b>Alloc</b> = Verfügbar (Allocatable) | '
  f'<b>Req %</b> bezogen auf Allocatable, <b>Use %</b> bezogen auf physische Capacity | '
  f'{dot(C_GREEN)} &lt;60% &nbsp;{dot(C_AMBER)} 60-80% &nbsp;{dot(C_RED)} &gt;80%'
  f'</p>')

a(f'<table style="{TBL}">')
a(f'<tr>'
  f'<th style="{TH}" rowspan="2">Node</th>'
  f'<th style="{TH}" rowspan="2">Rolle</th>'
  f'<th style="{THC_CPU}" colspan="5">CPU (Cores)</th>'
  f'<th style="{THC_RAM}" colspan="5">RAM (MB)</th>'
  f'</tr>')
a('<tr>'
  + ''.join(f'<th style="{THR_CPU}">{h}</th>' for h in ('Alloc', 'Req', 'Req %', 'Use', 'Use %'))
  + ''.join(f'<th style="{THR_RAM}">{h}</th>' for h in ('Alloc', 'Req', 'Req %', 'Use', 'Use %'))
  + '</tr>')

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
# CLUSTER-SUMMEN
# ===================================================================
a(HR)
a(f'<h2 style="{H2}">Cluster-Summen</h2>')
a(f'<p style="{META}">Nodes: {node_count} (davon {worker_count} schedulebar) | Pods (Running): {pod_count}</p>')

def mini_table(rows, head_style):
    """rows = list of (label, value, total, pct)"""
    hl = head_style.replace('text-align:right', 'text-align:left')
    a(f'<table style="{TBLH}">')
    a(f'<tr><th style="{hl}">Kennzahl</th><th style="{head_style}">Wert</th>'
      f'<th style="{head_style}">von Gesamt</th><th style="{head_style}">%</th></tr>')
    for i, (label, val, total, pct) in enumerate(rows):
        s = TD2 if i%2 else TD; sr = TDR2 if i%2 else TDR
        pct_cell = f'{pct:.0f}% {bar(pct,120)}' if pct > 0 else ''
        a(f'<tr><td style="{s}">{label}</td><td style="{sr}">{val}</td>'
          f'<td style="{sr}">{total or ""}</td><td style="{sr}">{pct_cell}</td></tr>')
    a('</table>')

a('<div style="display:flex;gap:24px;flex-wrap:wrap;">')

a('<div style="flex:1;min-width:400px;">')
a(f'<h3 style="{H3}">CPU (Cores)</h3>')
mini_table([
    ('Capacity (physisch)',   f'{cpu_cap:.1f}', '', 0),
    ('Allocatable',           f'{cpu_alloc:.1f}', '', 0),
    ('Requests (reserviert)', f'{cpu_req:.1f}', f'{cpu_alloc:.1f}', cpu_req_pct),
    ('Usage (tatsächlich)',   f'{cpu_use:.1f}', f'{cpu_cap:.1f}', cpu_use_pct),
    ('Frei (Alloc &minus; Req)', f'{cpu_alloc-cpu_req:.1f}', '', 0),
], THR_CPU)
a('</div>')

a('<div style="flex:1;min-width:400px;">')
a(f'<h3 style="{H3}">RAM (GB)</h3>')
mini_table([
    ('Capacity (physisch)',   f'{mem_cap_gb:.1f}', '', 0),
    ('Allocatable',           f'{mem_alloc_gb:.1f}', '', 0),
    ('Requests (reserviert)', f'{mem_req_gb:.1f}', f'{mem_alloc_gb:.1f}', mem_req_pct),
    ('Usage (tatsächlich)',   f'{mem_use_gb:.1f}', f'{mem_cap_gb:.1f}', mem_use_pct),
    ('Frei (Alloc &minus; Req)', f'{mem_alloc_gb-mem_req_gb:.1f}', '', 0),
], THR_RAM)
a('</div>')

a('</div>')

# ===================================================================
# TOP 10 NAMESPACES & HAMSTER (groesste Differenz Requests vs. Usage)
# ===================================================================
a(HR)
a(f'<h2 style="{H2}">Top 10 Namespaces &amp; Top 10 Hamster</h2>')
a(f'<p style="{LEG}">Namespaces mit der größten Differenz zwischen reservierten und tatsächlich genutzten Ressourcen. Hohe Werte = Optimierungspotenzial.</p>')

try:
    top_pods_raw = kc_text(['top', 'pods', '-A', '--no-headers'])
    ns_usage_cpu = defaultdict(float)
    ns_usage_mem = defaultdict(float)
    for line in top_pods_raw.strip().splitlines():
        parts = line.split()
        if len(parts) < 4: continue
        ns_usage_cpu[parts[0]] += pc(parts[2])
        ns_usage_mem[parts[0]] += pm_gb(parts[3])
except Exception:
    ns_usage_cpu = defaultdict(float)
    ns_usage_mem = defaultdict(float)

ns_full = []
for name, v in ns.items():
    if v['cr'] < 0.01 and v['p'] < 2: continue
    cu = ns_usage_cpu.get(name, 0)
    mu = ns_usage_mem.get(name, 0)
    ns_full.append({
        'name': name, 'pods': v['p'],
        'cr': v['cr'], 'cl': v['cl'], 'cu': cu, 'cpu_diff': max(0, v['cr'] - cu),
        'mr': v['mr'], 'ml': v['ml'], 'mu': mu, 'mem_diff': max(0, v['mr'] - mu),
    })

def ns_table(title, data, cols, head_style, alloc_ref=None):
    """
    cols = list of (header, key, fmt, bar_key)
    alloc_ref: gesetzt -> Balken = % von alloc_ref, sonst relativ zum Spaltenmaximum
    """
    hl = head_style.replace('text-align:right', 'text-align:left')
    a(f'<h3 style="{H3}">{title}</h3>')
    a(f'<table style="{TBLH}">')
    a(f'<tr><th style="{hl}">#</th><th style="{hl}">Namespace</th>'
      + ''.join(f'<th style="{head_style}">{c[0]}</th>' for c in cols) + '</tr>')
    maxvals = {}
    if not alloc_ref:
        for c in cols:
            if c[3]:
                maxvals[c[3]] = max((row[c[3]] for row in data[:10]), default=1) or 1
    for i, row in enumerate(data[:10]):
        s = TD2 if i%2 else TD; sr = TDR2 if i%2 else TDR
        cells = ''
        for c in cols:
            val = c[2].format(row[c[1]])
            if c[3]:
                ref = alloc_ref if alloc_ref else maxvals[c[3]]
                pct = row[c[3]] / ref * 100 if ref > 0 else 0
                val = f'{val} {bar(pct, 60)}'
            cells += f'<td style="{sr}">{val}</td>'
        a(f'<tr><td style="{sr}">{i+1}</td><td style="{s}">{row["name"]}</td>{cells}</tr>')
    a('</table>')

LAYOUT_L = 'width:50%;vertical-align:top;padding-right:12px;border:none;'
LAYOUT_R = 'width:50%;vertical-align:top;padding-left:12px;border:none;'

a('<table style="width:100%;border:none;border-collapse:collapse;"><tr>')
a(f'<td style="{LAYOUT_L}">')
ns_table('Top 10 &ndash; CPU Requests',
    sorted(ns_full, key=lambda x: -x['cr']),
    [('Pods', 'pods', '{:.0f}', None), ('CPU Req', 'cr', '{:.2f}', 'cr'), ('CPU Lim', 'cl', '{:.2f}', None)],
    THR_CPU, alloc_ref=cpu_alloc)
a('</td>')
a(f'<td style="{LAYOUT_R}">')
ns_table('Top 10 &ndash; RAM Requests (GB)',
    sorted(ns_full, key=lambda x: -x['mr']),
    [('Pods', 'pods', '{:.0f}', None), ('RAM Req', 'mr', '{:.2f}', 'mr'), ('RAM Lim', 'ml', '{:.2f}', None)],
    THR_RAM, alloc_ref=mem_alloc_gb)
a('</td>')
a('</tr></table>')

a('<table style="width:100%;border:none;border-collapse:collapse;"><tr>')
a(f'<td style="{LAYOUT_L}">')
ns_table('Top 10 &ndash; CPU Hamster (Req &minus; Use)',
    sorted(ns_full, key=lambda x: -x['cpu_diff']),
    [('CPU Req', 'cr', '{:.2f}', None), ('CPU Use', 'cu', '{:.2f}', None), ('Diff', 'cpu_diff', '{:.2f}', 'cpu_diff')],
    THR_CPU)
a('</td>')
a(f'<td style="{LAYOUT_R}">')
ns_table('Top 10 &ndash; RAM Hamster (Req &minus; Use) (GB)',
    sorted(ns_full, key=lambda x: -x['mem_diff']),
    [('RAM Req', 'mr', '{:.2f}', None), ('RAM Use', 'mu', '{:.2f}', None), ('Diff', 'mem_diff', '{:.2f}', 'mem_diff')],
    THR_RAM)
a('</td>')
a('</tr></table>')

a('</div>')
print('\n'.join(o))
