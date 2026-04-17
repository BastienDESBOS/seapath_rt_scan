# SEAPATH RT Scan

A diagnostic tool that connects to a running [SEAPATH](https://github.com/seapath) vPAC cluster over SSH and produces a structured inventory of its real-time (RT) configuration.

Use it **before running RT or robustness test campaigns** to document and verify the cluster state, and to reproduce the exact setup if tests need to be replayed.

---

## What it collects

| Category | Details |
|----------|---------|
| **Hypervisor nodes** | Kernel version, CPU model, physical/logical core count, HyperThreading, `isolcpus` kernel parameter, IRQ affinity mask, PTP sync status |
| **Virtual machines** | Full libvirt XML config: vCPU count, CPU pinning (`vcpupin`), emulator pin, RT scheduler (FIFO/priority), cgroup slice (`/machine/rt` vs `/machine/nort`), RAM, hugepages, memballoon, disks, network |
| **Pacemaker** | State of all `VirtualDomain` resources (started/stopped/disabled), current host, placement constraints (preferred/forbidden node, score) |

---

## Requirements

Python 3.8 or later.

```bash
pip install rich      # terminal colour output
pip install paramiko  # SSH connectivity (required for remote mode)
```

---

## Usage

The tool is designed to run **from a machine outside the cluster**, connecting to nodes over SSH.

```bash
# Scan three nodes via SSH and export both reports
python3 main.py --hosts ccv1,ccv2,ccv3 --user virtu --key ~/.ssh/id_rsa --html --json

# Only show VMs currently running
python3 main.py --hosts ccv1,ccv2,ccv3 --user virtu --key ~/.ssh/id_rsa --running-only --html

# Run locally if you are already on a cluster node
python3 main.py --local --html --json
```

Reports are written to `./seapath_reports/` by default. Override with `--output-dir PATH`.

### All options

| Option | Description |
|--------|-------------|
| `--hosts ccv1,ccv2,ccv3` | Comma-separated list of cluster node hostnames |
| `--user virtu` | SSH username |
| `--key ~/.ssh/id_rsa` | SSH private key path |
| `--password` | SSH password (prefer `--key`) |
| `--port 22` | SSH port (default: 22) |
| `--local` | Run locally without SSH |
| `--running-only` | Only collect VMs in `running` state |
| `--html` | Export an HTML report |
| `--json` | Export a JSON report |
| `--output-dir PATH` | Output directory (default: `./seapath_reports`) |
| `--no-color` | Disable terminal colours |

---

## Output formats

### Terminal
Four sections printed with colour:
1. **Pacemaker table** — resource state, current node, placement constraints
2. **CPU map** — physical CPU grid per node, colour-coded by VM assignment
3. **VM details** — full resource configuration per VM
4. **Alerts** — automatic warnings for RT configuration issues

### HTML
Self-contained file with the same four sections. Can be shared without any dependencies.

### JSON
Full structured dump of all collected data, suitable for automated processing or archiving.

---

## CPU map colour coding

| Colour | Meaning |
|--------|---------|
| Violet | VM in `/machine/rt` (RT-pinned) |
| Green | VM in `/machine/nort` (non-RT pinned) |
| Cyan | VM in unknown cgroup slice |
| Yellow `~~` | Isolated CPU, currently free |
| Grey | System CPU (not isolated) |
| Red `!!` | **Conflict** — two VMs share the same physical CPU |

---

## Automatic alerts

| Alert | Severity |
|-------|----------|
| Two VMs pinned to the same CPU | ERROR |
| RT VM without `vcpupin` | WARN |
| RT VM with `memballoon` enabled | WARN |
| RT VM without `hugepages` | WARN |
| RT VM with non-FIFO scheduler | WARN |
| `ptp4l` inactive on a node | ERROR |

---

## File structure

| File | Role |
|------|------|
| `main.py` | Entry point — argument parsing and scan orchestration |
| `models.py` | Data structures (`VmConfig`, `HostInfo`, `ClusterReport`, …) |
| `runners.py` | `LocalRunner` / `SSHRunner` — unified command execution |
| `parsers.py` | Pure functions: parse virsh output, VM XML, lscpu, Pacemaker |
| `scan_hypervisors.py` | Collect host info and Pacemaker state from a node |
| `scan_vms.py` | Collect VM configurations via `virsh list` + `virsh dumpxml` |
| `report_console.py` | Rich terminal output (CPU grid, tables, alerts) |
| `report_html.py` | Self-contained HTML report generator |
| `report_json.py` | JSON report export |
