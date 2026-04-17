# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This tool scans a running SEAPATH vPAC cluster over SSH and produces a structured inventory of its real-time (RT) configuration — CPU pinning, cgroup slices, Pacemaker placement, memory settings. It is used before RT and robustness test campaigns to document and verify the cluster state.

## Running the tool

```bash
pip install rich paramiko

# Primary use case: from a remote machine, SSH into cluster nodes
python3 main.py --hosts ccv1,ccv2,ccv3 --user virtu --key ~/.ssh/id_rsa --html --json

# Local mode (when already on a cluster node)
python3 main.py --local --html --json
```

## Code conventions

- All code and comments are in **English**
- Files are kept short and focused on a single responsibility
- Each file starts with a docstring describing its role and entry points

## Architecture

The codebase is split into nine single-responsibility modules:

```
models.py            Data structures (dataclasses, no logic)
runners.py           LocalRunner / SSHRunner — unified .run(cmd) interface
parsers.py           Pure functions: string input → dataclass output
scan_hypervisors.py  Collect HostInfo + Pacemaker state from a node
scan_vms.py          Collect VmConfig list via virsh list + dumpxml
report_console.py    Rich terminal output (CPU grid, tables, alerts)
report_html.py       Self-contained HTML report generator
report_json.py       JSON export
main.py              CLI entry point — wires everything together
```

**Data flow:**
1. `main.py` parses args → creates `runners.py` runner objects
2. `scan_hypervisors.py` + `scan_vms.py` use runners to collect raw data
3. `parsers.py` converts raw command output to `models.py` dataclasses
4. `main.py` assembles a `ClusterReport` and calls the report modules

**Key domain concepts:**
- CPU pinning is read from `virsh dumpxml` → `<cputune>/<vcpupin>`. `parse_cpuset()` expands ranges like `5,7-9` → `[5,7,8,9]`.
- RT vs noRT classification is determined by `VmConfig.cgroup_partition` containing `"rt"` or `"nort"` (`/machine/rt` and `/machine/nort` cgroup slices).
- Pacemaker state is only fetched from the first reachable node (cluster-wide data).
- All runners expose the same `.run(cmd, sudo=False) -> (stdout, stderr)` interface, making collection functions SSH/local agnostic.

## Network environment

The deployment environment uses an authenticated HTTP proxy (`http_proxy` / `https_proxy` env vars). Git push via HTTPS requires `git config http.proxy "$https_proxy"`. When git push fails, use the GitHub Contents API via `curl -x "$https_proxy"` instead.
