# Capacity Overview for OpenShift Clusters

## Background

This script was developed through an iterative AI-assisted dialogue. Requirements, metric selection, layout decisions, and testing were provided by the user. Code generation, parser logic, and HTML/CSS templating were implemented by the AI.

## Overview

A Python script that generates an HTML capacity overview of an OpenShift cluster. Runs on the bastion host, requires `oc` (logged in) and Python 3. No additional dependencies needed. The capacity overview is a point-in-time snapshot.

## Usage

```bash
python3 capacity-overview-script.py > capacity-$(date +%F).html
```

## Output

A single HTML file with inline CSS.

## How It Works

### Step 1: Fetch Data

Four `oc` calls — everything the script needs:

```bash
oc get nodes -o json
oc get pods -A -o json
oc get pv -o json
oc adm top nodes
```

### Step 2: Parse

Three parser functions convert cluster resource values into readable numbers:

```python
pc(v)                    # CPU values → cores
pm_gb(v)                 # Memory → GB
pm_mb(v)                 # Wrapper, calls pm_gb() and multiplies by 1024
parse_top_line(line)     # Parses a single line from oc adm top nodes --no-headers
```

### Step 3: Aggregate Data

Three aggregations run over the pod list:

- **Requests per Node** — The script iterates over all running pods, reads `spec.nodeName`, and sums up `resources.requests.cpu` / `resources.requests.memory` for all containers on each node. Result: two dictionaries `node_req_cpu` and `node_req_mem`, keyed by node name.
- **Usage per Node** — Sourced directly from `oc adm top nodes`. Stored in `node_usage_cpu` and `node_usage_mem`.
- **Requests/Limits per Namespace** — Same pod loop, but grouped by `metadata.namespace`.

### Step 4: Build Node Rows

For each node, the three data sources are merged and the necessary calculations are performed for rendering.

### Step 5: Calculate Cluster Totals

Simple sums across all nodes, including percentage calculations.

### Step 6: Namespace Rankings

| Ranking | Sort Order | Bar Reference |
|---|---|---|
| **Top 10 CPU Requests** | CPU requests descending | Ratio of reserved to total allocatable (Req / Alloc) |
| **Top 10 RAM Requests** | RAM requests descending | Ratio of reserved to total allocatable (Req / Alloc) |
| **Top 10 CPU Hoarders** | CPU delta descending (relative to largest delta) | Ratio of reserved to actually consumed |
| **Top 10 RAM Hoarders** | RAM delta descending (relative to largest delta) | Ratio of reserved to actually consumed |
