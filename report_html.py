"""
report_html.py
--------------
Generates a self-contained HTML report file from a ClusterReport.
The file embeds all CSS inline — no external dependencies, shareable as-is.

Entry point:
    export_html(report, output_dir, running_only=False) -> Path
"""

from pathlib import Path

from models import HostInfo, ClusterReport
from report_console import kib_to_human


def _cpu_td(cpu_id: int, cpu_to_vms: dict, isolated: set, n: int) -> str:
    """Return an HTML <td> cell for one logical CPU in the CPU map table."""
    if cpu_id >= n:
        return '<td class="cpu-empty"></td>'

    vms_here = list({v.name: v for v in cpu_to_vms.get(cpu_id, [])}.values())

    if len(vms_here) > 1:
        cls, lbl, tip = "cpu-conflict", "!!", " / ".join(v.name for v in vms_here)
    elif len(vms_here) == 1:
        vm  = vms_here[0]
        lbl = vm.name[:3]
        tip = vm.name
        cls = ("cpu-nort" if "nort" in vm.cgroup_partition
               else "cpu-rt" if "rt" in vm.cgroup_partition
               else "cpu-vm")
    elif cpu_id in isolated:
        cls, lbl, tip = "cpu-iso", "~~", f"CPU {cpu_id} isolated free"
    else:
        cls, lbl, tip = "cpu-sys", str(cpu_id), f"CPU {cpu_id} system"

    return (f'<td class="cpu-cell {cls}" title="{tip}">'
            f'<div class="cpu-num">{cpu_id}</div>'
            f'<div class="cpu-lbl">{lbl}</div></td>')


def _cpu_map_html(host: HostInfo, vms_on_host: list) -> str:
    """Return the HTML block for one node's physical CPU map."""
    cpu_to_vms: dict = {}
    for vm in vms_on_host:
        for pin in vm.vcpu_pins:
            for cpu_id in pin.physical_cpus:
                cpu_to_vms.setdefault(cpu_id, []).append(vm)

    isolated = set(host.isolated_cpus_list)
    n, threads, phys = host.logical_cpus, host.threads_per_core, host.physical_cores
    COLS = 12

    rows = ""
    for rs in range(0, phys, COLS):
        cores = list(range(rs, min(rs + COLS, phys)))
        hdr   = "".join(f'<th class="ch">C{c:02d}</th>' for c in cores)
        t0    = "".join(_cpu_td(c, cpu_to_vms, isolated, n) for c in cores)
        rows += f"<tr><th class='rl'></th>{hdr}</tr>"
        rows += f"<tr><th class='rl'>T0</th>{t0}</tr>"
        if threads == 2:
            t1 = "".join(_cpu_td(c + phys, cpu_to_vms, isolated, n) for c in cores)
            rows += f"<tr><th class='rl'>T1</th>{t1}</tr>"
        rows += "<tr class='sep'><td colspan='99'></td></tr>"

    ptp = "OK" if host.ptp_sync_ok else "INACTIVE"
    return f"""
<div class="cpumap">
  <h3>{host.hostname}
    <small>{host.cpu_model[:55]}
      &nbsp;&middot;&nbsp;{phys} cores / {n} CPUs
      &nbsp;&middot;&nbsp;isolcpus:<code>{host.isolated_cpus or '—'}</code>
      &nbsp;&middot;&nbsp;PTP:{ptp}</small>
  </h3>
  <div class="legend">
    <span class="cpu-cell cpu-rt">RT</span>&nbsp;/machine/rt&nbsp;&nbsp;
    <span class="cpu-cell cpu-nort">noRT</span>&nbsp;/machine/nort&nbsp;&nbsp;
    <span class="cpu-cell cpu-vm">VM</span>&nbsp;other&nbsp;&nbsp;
    <span class="cpu-cell cpu-iso">~~</span>&nbsp;isolated free&nbsp;&nbsp;
    <span class="cpu-cell cpu-sys">sys</span>&nbsp;system&nbsp;&nbsp;
    <span class="cpu-cell cpu-conflict">!!</span>&nbsp;conflict
  </div>
  <table class="cmap"><tbody>{rows}</tbody></table>
</div>"""


# All CSS is inlined so the HTML file is fully self-contained
_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;font-size:13px;background:#f4f6f9;color:#222}
header{background:#1F3864;color:#fff;padding:14px 28px}
h1{font-size:17px;font-weight:600}
.sub{font-size:11px;opacity:.7;margin-top:3px}
.wrap{max-width:1700px;margin:0 auto;padding:18px 12px}
section{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.1);margin-bottom:20px;overflow:hidden}
section>h2{background:#1F3864;color:#fff;padding:8px 14px;font-size:13px;font-weight:600}
table{width:100%;border-collapse:collapse}
th{background:#2E5F9E;color:#fff;padding:6px 8px;text-align:left;font-size:11px;font-weight:600}
td{padding:5px 8px;border-bottom:1px solid #eee;vertical-align:top}
tr:hover td{background:#f0f4ff}
.mono{font-family:Consolas,monospace;font-size:11px}
.small{font-size:11px}
.c{text-align:center}
.ok{color:#1a7a1a;font-weight:bold}
.al{color:#c00;font-weight:bold}
.dim{color:#888}
.slice-rt{background:#e1d5e7;color:#4a0072;border-radius:3px;padding:1px 4px;font-size:10px;font-weight:bold}
.slice-nort{background:#d5e8d4;color:#2d6a2d;border-radius:3px;padding:1px 4px;font-size:10px;font-weight:bold}
pre{background:#1e1e2e;color:#cdd6f4;padding:12px;font-size:11px;overflow-x:auto;line-height:1.5}
.cpumap{padding:12px 16px 8px}
.cpumap h3{font-size:13px;color:#1F3864;margin-bottom:6px}
.cpumap small{font-size:10px;color:#666;margin-left:6px;font-weight:400}
.legend{font-size:11px;color:#555;margin-bottom:8px}
.legend .cpu-cell{display:inline-flex;width:26px;height:20px;align-items:center;justify-content:center;font-size:9px;border-radius:3px;vertical-align:middle}
.cmap{border-collapse:separate;border-spacing:2px 2px}
.ch{background:none;color:#888;font-size:9px;text-align:center;padding:0 1px;border:none;font-weight:normal}
.rl{background:none;color:#aaa;font-size:9px;text-align:right;padding:0 3px;border:none;font-weight:normal;white-space:nowrap}
.sep td{height:5px;border:none;background:transparent}
.cpu-cell{width:38px;height:38px;text-align:center;vertical-align:middle;border-radius:5px;cursor:default;border:1px solid rgba(0,0,0,.08)}
.cpu-num{font-size:9px;opacity:.7;line-height:1}
.cpu-lbl{font-size:12px;font-weight:bold;line-height:1.3}
.cpu-rt{background:#7b2d8b;color:#fff}
.cpu-nort{background:#2d6a2d;color:#fff}
.cpu-vm{background:#1a5a8a;color:#fff}
.cpu-iso{background:#c8a000;color:#222}
.cpu-sys{background:#e0e0e0;color:#555}
.cpu-conflict{background:#c00;color:#fff}
.cpu-empty{background:transparent;border:none}
"""


def export_html(report: ClusterReport, output_dir: Path,
                running_only: bool = False) -> Path:
    """
    Write a self-contained HTML report to output_dir.
    Returns the path of the generated file.
    """
    ts    = report.generated_at.replace(":", "-").replace(" ", "_")
    fname = output_dir / f"seapath_report_{ts}.html"
    vms   = [v for v in report.vms if not running_only or "running" in v.state]

    # Pacemaker table rows
    cmap: dict = {}
    for c in report.pacemaker_constraints:
        cmap.setdefault(c.vm_name, []).append(c)
    pce_rows = ""
    for pvm in sorted(report.pacemaker_vms, key=lambda v: v.name):
        sc = ("ok" if "started" in pvm.state.lower() and not pvm.disabled
              else "dim" if pvm.disabled else "al")
        constrs = cmap.get(pvm.name, [])
        c_str = (" | ".join(
            f"{c.constraint_type}->{c.node} ({c.score})" for c in constrs) or "—")
        pce_rows += (f"<tr><td><b>{pvm.name}</b></td>"
                     f"<td><span class='{sc}'>{pvm.state}</span></td>"
                     f"<td>{pvm.node or '—'}</td>"
                     f"<td class='mono small'>{c_str}</td></tr>")

    # CPU maps, one per node
    maps_html = "".join(
        _cpu_map_html(h, [v for v in vms if v.host == h.hostname])
        for h in report.hosts)

    # VM detail table rows
    vm_rows = ""
    for vm in sorted(vms, key=lambda v: v.name):
        ss = "ok" if "running" in vm.state else "dim"
        pins = "<br>".join(
            f"vCPU{p.vcpu}-><b>CPU {','.join(map(str, p.physical_cpus))}</b>"
            for p in sorted(vm.vcpu_pins, key=lambda x: x.vcpu)
        ) or "<em>not pinned</em>"
        emul  = ",".join(map(str, vm.emulator_pin_cpus)) or "—"
        sched = (vm.vcpu_scheduler or "—") + (
            f" p={vm.vcpu_scheduler_priority}"
            if vm.vcpu_scheduler_priority else "")
        sc = ("slice-rt"
              if "rt" in vm.cgroup_partition and "nort" not in vm.cgroup_partition
              else "slice-nort" if "nort" in vm.cgroup_partition else "")
        hp  = "yes" if vm.hugepages else "no"
        bal = '<span class="al">yes</span>' if vm.memballoon else "no"
        disks = "<br>".join(vm.disks[:3]) or "—"
        vm_rows += (
            f"<tr><td><b>{vm.name}</b></td>"
            f"<td><span class='{ss}'>{vm.state}</span></td>"
            f"<td>{vm.host}</td>"
            f"<td class='mono'>{pins}</td>"
            f"<td class='mono'>{sched}</td>"
            f"<td class='mono'>{emul}</td>"
            f"<td><span class='{sc}'>{vm.cgroup_partition or '—'}</span></td>"
            f"<td>{kib_to_human(vm.memory_kib)}</td>"
            f"<td class='c'>{hp}</td><td class='c'>{bal}</td>"
            f"<td class='mono small'>{disks}</td></tr>")

    raw_esc = (report.pacemaker_raw
               .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEAPATH RT Scan — {report.generated_at}</title>
<style>{_CSS}</style>
</head><body>
<header>
  <h1>SEAPATH vPAC — RT Configuration Report</h1>
  <div class="sub">Generated: {report.generated_at} &nbsp;|&nbsp;
    Local host: {report.local_host} &nbsp;|&nbsp;
    {len(report.hosts)} node(s) &nbsp;|&nbsp;
    {sum(1 for v in report.vms if 'running' in v.state)}/{len(report.vms)} VMs active
  </div>
</header>
<div class="wrap">
<section>
<h2>1. Pacemaker — VirtualDomain resources</h2>
<table><thead><tr><th>Resource</th><th>State</th><th>Node</th><th>Constraints</th></tr></thead>
<tbody>{pce_rows or '<tr><td colspan="4"><em>Not available</em></td></tr>'}</tbody></table>
</section>
<section>
<h2>2. Physical CPU map per node</h2>
{maps_html}
</section>
<section>
<h2>3. Detailed VM configuration</h2>
<table><thead><tr>
  <th>VM</th><th>State</th><th>Node</th><th>vCPU->phys. CPU</th>
  <th>Sched.</th><th>emulatorpin</th><th>Slice</th>
  <th>RAM</th><th>HP</th><th>Balloon</th><th>Disks</th>
</tr></thead><tbody>{vm_rows}</tbody></table>
</section>
<section>
<h2>4. Raw Pacemaker state</h2>
<pre>{raw_esc or "Not available"}</pre>
</section>
</div></body></html>"""

    with open(fname, "w") as f:
        f.write(html)
    print(f"HTML report: {fname}")
    return fname
