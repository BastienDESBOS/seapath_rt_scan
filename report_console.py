"""
report_console.py
-----------------
Renders the cluster report to the terminal using the 'rich' library.

Four sections:
  1. Pacemaker overview table
  2. Physical CPU map per node (colour-coded grid)
  3. Detailed VM configuration table
  4. Automatic alerts

Colour coding for the CPU grid:
  violet  = VM in /machine/rt   (RT-pinned)
  green   = VM in /machine/nort (non-RT pinned)
  cyan    = VM in unknown slice
  yellow  = isolated CPU, currently free
  grey    = system CPU (not isolated)
  red !!  = conflict: two or more VMs share the same physical CPU
"""

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

from models import HostInfo, ClusterReport


def kib_to_human(kib: int) -> str:
    """Convert a KiB integer to a human-readable GiB or MiB string."""
    if kib >= 1024 * 1024:
        return f"{kib / 1024 / 1024:.1f} GiB"
    return f"{kib / 1024:.0f} MiB"


def _cpu_style_label(cpu_id: int, cpu_to_vms: dict,
                     isolated: set, n: int) -> tuple:
    """Return (rich_style, 2-char label, tooltip) for one logical CPU."""
    if cpu_id >= n:
        return None, None, None

    vms_here = list({v.name: v for v in cpu_to_vms.get(cpu_id, [])}.values())

    if len(vms_here) > 1:
        return ("bold white on red", "!!",
                "/".join(v.name for v in vms_here))
    elif len(vms_here) == 1:
        vm  = vms_here[0]
        lbl = vm.name[:2].upper()
        if "nort" in vm.cgroup_partition:
            return "bold white on dark_green", lbl, vm.name
        elif "rt" in vm.cgroup_partition:
            return "bold white on dark_violet", lbl, vm.name
        else:
            return "bold white on dark_cyan", lbl, vm.name
    elif cpu_id in isolated:
        return "bold black on yellow3", "~~", f"CPU{cpu_id} isolated free"
    else:
        return "dim white on grey30", "  ", f"CPU{cpu_id} system"


def render_cpu_map_rich(host: HostInfo, vms_on_host: list, console) -> None:
    """
    Print a graphical CPU grid for one hypervisor node.
    Each column = one physical core; two rows per core if HyperThreading.
    """
    # Build cpu_id -> [VmConfig] index from vcpupin data
    cpu_to_vms: dict = {}
    for vm in vms_on_host:
        for pin in vm.vcpu_pins:
            for cpu_id in pin.physical_cpus:
                cpu_to_vms.setdefault(cpu_id, []).append(vm)

    isolated = set(host.isolated_cpus_list)
    n        = host.logical_cpus
    threads  = host.threads_per_core
    phys     = host.physical_cores
    COLS     = 12   # physical cores displayed per grid row

    ptp_str = "[green]OK[/green]" if host.ptp_sync_ok else "[red]INACTIVE[/red]"
    console.print(
        f"\n  [bold white]{host.hostname}[/bold white]  "
        f"[dim]{host.cpu_model[:55]}[/dim]\n"
        f"  {phys} physical cores · {n} logical CPUs · HT={threads} · "
        f"isolcpus=[yellow]{host.isolated_cpus or '—'}[/yellow] · PTP {ptp_str}"
    )

    for row_start in range(0, phys, COLS):
        cores = list(range(row_start, min(row_start + COLS, phys)))

        # Header: physical core indices
        console.print("  " + " ".join(
            f"[dim]C{c:02d}[/dim]" for c in cores))

        # Thread 0 row (logical cpu_id == physical core index)
        t0 = []
        for c in cores:
            style, lbl, _ = _cpu_style_label(c, cpu_to_vms, isolated, n)
            t0.append("    " if style is None
                       else f"[{style}]{lbl}[/{style}][dim]{c:02d}[/dim]")
        console.print("  " + " ".join(t0))

        # Thread 1 row (logical cpu_id == core index + physical_cores)
        if threads == 2:
            t1 = []
            for c in cores:
                cpu1 = c + phys
                style, lbl, _ = _cpu_style_label(
                    cpu1, cpu_to_vms, isolated, n)
                t1.append("    " if style is None
                           else f"[{style}]{lbl}[/{style}][dim]{cpu1:02d}[/dim]")
            console.print("  " + " ".join(t1))

        console.print()

    console.print(
        "  [bold white on dark_violet] RT [/bold white on dark_violet] /machine/rt  "
        "[bold white on dark_green] noRT [/bold white on dark_green] /machine/nort  "
        "[bold white on dark_cyan] VM [/bold white on dark_cyan] other  "
        "[bold black on yellow3] ~~ [/bold black on yellow3] isolated free  "
        "[dim white on grey30]    [/dim white on grey30] system  "
        "[bold white on red] !! [/bold white on red] conflict"
    )

    if not vms_on_host:
        return

    t = Table(box=box.MINIMAL, show_header=True,
              header_style="bold dim", padding=(0, 1))
    t.add_column("VM", style="bold", min_width=16)
    t.add_column("State")
    t.add_column("vCPU -> physical CPU", style="yellow")
    t.add_column("Sched.")
    t.add_column("Slice", style="magenta")
    t.add_column("RAM")
    t.add_column("HP")
    for vm in sorted(vms_on_host, key=lambda v: v.name):
        state_s = ("[green]running[/green]" if "running" in vm.state
                   else f"[dim]{vm.state}[/dim]")
        pins_s = "  ".join(
            f"vCPU{p.vcpu}->{','.join(map(str, p.physical_cpus))}"
            for p in sorted(vm.vcpu_pins, key=lambda x: x.vcpu)
        ) or "[dim]not pinned[/dim]"
        sched = (vm.vcpu_scheduler or "—") + (
            f" p{vm.vcpu_scheduler_priority}"
            if vm.vcpu_scheduler_priority else "")
        hp = "[green]yes[/green]" if vm.hugepages else "[dim]no[/dim]"
        sc = ("magenta"
              if "rt" in vm.cgroup_partition and "nort" not in vm.cgroup_partition
              else "green" if "nort" in vm.cgroup_partition else "cyan")
        t.add_row(f"[{sc}]{vm.name}[/{sc}]", state_s, pins_s, sched,
                  vm.cgroup_partition or "—", kib_to_human(vm.memory_kib), hp)
    console.print(t)


def _generate_alerts(report: ClusterReport, vms: list) -> list:
    """
    Inspect the report and return a list of alert strings.
    Detects: CPU conflicts, misconfigured RT VMs, PTP inactive.
    """
    alerts = []

    # Two VMs pinned to the same physical CPU
    cpu_vm_map: dict = {}
    for vm in vms:
        for pin in vm.vcpu_pins:
            for cpu_id in pin.physical_cpus:
                cpu_vm_map.setdefault(cpu_id, []).append(vm.name)
    for cpu_id, names in cpu_vm_map.items():
        if len(set(names)) > 1:
            alerts.append(
                f"[red]CONFLICT[/red] CPU {cpu_id} shared by: "
                f"{', '.join(set(names))}")

    for vm in vms:
        is_rt = "rt" in vm.cgroup_partition and "nort" not in vm.cgroup_partition
        if is_rt and not vm.vcpu_pins:
            alerts.append(f"[yellow]WARN[/yellow] {vm.name}: RT VM has no vcpupin")
        if is_rt and vm.memballoon:
            alerts.append(f"[yellow]WARN[/yellow] {vm.name}: memballoon enabled on RT VM")
        if is_rt and not vm.hugepages:
            alerts.append(f"[yellow]WARN[/yellow] {vm.name}: hugepages disabled on RT VM")
        if is_rt and vm.vcpu_scheduler.lower() not in ("fifo", "sched_fifo"):
            alerts.append(
                f"[yellow]WARN[/yellow] {vm.name}: scheduler="
                f"[yellow]{vm.vcpu_scheduler or 'not set'}[/yellow] (expected FIFO)")

    for h in report.hosts:
        if not h.ptp_sync_ok:
            alerts.append(f"[red]ERROR[/red] {h.hostname}: ptp4l inactive")

    return alerts


def print_console_report(report: ClusterReport,
                         no_color: bool = False,
                         running_only: bool = False) -> None:
    """Print the full cluster report to the terminal."""
    if not HAS_RICH or no_color:
        _print_plain(report)
        return

    console = Console()
    vms   = [v for v in report.vms if not running_only or "running" in v.state]
    n_run = sum(1 for v in report.vms if "running" in v.state)

    console.print(Panel(
        f"[bold white]SEAPATH vPAC — RT Configuration Report[/bold white]\n"
        f"Generated: [cyan]{report.generated_at}[/cyan]  |  "
        f"Local host: [yellow]{report.local_host}[/yellow]  |  "
        f"[yellow]{len(report.hosts)}[/yellow] node(s)  |  "
        f"[green]{n_run}[/green]/{len(report.vms)} VMs active",
        box=box.DOUBLE_EDGE, style="blue"
    ))

    # 1 — Pacemaker
    console.print("\n[bold blue]1. Pacemaker cluster view[/bold blue]")
    if report.pacemaker_vms:
        cmap: dict = {}
        for c in report.pacemaker_constraints:
            cmap.setdefault(c.vm_name, []).append(c)
        pt = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan")
        pt.add_column("VM resource", style="bold")
        pt.add_column("Pacemaker state")
        pt.add_column("Current node")
        pt.add_column("Placement constraint")
        pt.add_column("Score")
        for pvm in sorted(report.pacemaker_vms, key=lambda v: v.name):
            if pvm.disabled:
                ss = "[dim]Stopped (disabled)[/dim]"
            elif "started" in pvm.state.lower():
                ss = f"[green]{pvm.state}[/green]"
            else:
                ss = f"[red]{pvm.state}[/red]"
            constrs = cmap.get(pvm.name, [])
            c_str = (" / ".join(
                f"{c.constraint_type}->{c.node}" for c in constrs)
                     or "[dim]none[/dim]")
            s_str = " / ".join(c.score for c in constrs) or ""
            pt.add_row(pvm.name, ss, pvm.node or "[dim]—[/dim]",
                       c_str, s_str)
        console.print(pt)
    else:
        console.print("[dim]  Pacemaker not available[/dim]")

    # 2 — CPU map
    console.print("\n[bold blue]2. Physical CPU map per node[/bold blue]")
    console.print(
        "[dim]  Each column = one physical core. "
        "2 rows per core when HyperThreading is active (T0 top, T1 bottom).\n"
        "  The 2 letters in each cell = first 2 characters of the pinned VM name.[/dim]"
    )
    for host in report.hosts:
        render_cpu_map_rich(
            host, [v for v in vms if v.host == host.hostname], console)

    # 3 — VM details
    console.print("\n[bold blue]3. Detailed VM configuration[/bold blue]")
    vt = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan")
    vt.add_column("VM", style="bold")
    vt.add_column("State", justify="center")
    vt.add_column("Node")
    vt.add_column("vCPU -> phys. CPU", style="yellow")
    vt.add_column("Sched/Prio", justify="center")
    vt.add_column("emulatorpin")
    vt.add_column("cgroup slice", style="magenta")
    vt.add_column("RAM")
    vt.add_column("HP", justify="center")
    vt.add_column("Balloon", justify="center")
    for vm in sorted(vms, key=lambda v: v.name):
        sc = ("magenta"
              if "rt" in vm.cgroup_partition and "nort" not in vm.cgroup_partition
              else "green" if "nort" in vm.cgroup_partition else "cyan")
        ss = ("[green]running[/green]" if "running" in vm.state
              else f"[dim]{vm.state}[/dim]")
        pins = "\n".join(
            f"vCPU{p.vcpu}->CPU {','.join(map(str, p.physical_cpus))}"
            for p in sorted(vm.vcpu_pins, key=lambda x: x.vcpu)
        ) or "[dim]not pinned[/dim]"
        sched = (vm.vcpu_scheduler or "—") + (
            f" p={vm.vcpu_scheduler_priority}"
            if vm.vcpu_scheduler_priority else "")
        emul  = ",".join(map(str, vm.emulator_pin_cpus)) if vm.emulator_pin_cpus else "—"
        hp  = "[green]yes[/green]" if vm.hugepages  else "[dim]no[/dim]"
        bal = "[red]yes[/red]"    if vm.memballoon else "[dim]no[/dim]"
        vt.add_row(f"[{sc}]{vm.name}[/{sc}]", ss, vm.host,
                   pins, sched, emul,
                   vm.cgroup_partition or "—",
                   kib_to_human(vm.memory_kib), hp, bal)
    console.print(vt)

    # 4 — Alerts
    console.print("\n[bold blue]4. Alerts[/bold blue]")
    alerts = _generate_alerts(report, vms)
    if alerts:
        for a in alerts:
            console.print(f"  {a}")
    else:
        console.print("  [green]No alerts[/green]")


def _print_plain(report: ClusterReport) -> None:
    """Fallback plain-text output when 'rich' is not installed."""
    print(f"\n=== SEAPATH {report.generated_at} | {report.local_host} ===")
    for pvm in report.pacemaker_vms:
        print(f"  {pvm.name}: {pvm.state} on {pvm.node or '—'}")
    for vm in report.vms:
        print(f"\n  VM: {vm.name} [{vm.state}] on {vm.host}")
        for p in vm.vcpu_pins:
            print(f"    vCPU{p.vcpu} -> CPU {p.physical_cpus}")
        print(f"    Slice: {vm.cgroup_partition}  RAM: {kib_to_human(vm.memory_kib)}")
