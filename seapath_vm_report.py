#!/usr/bin/env python3
"""
seapath_vm_report.py  v2
========================
Rapport d'allocation de ressources des VMs SEAPATH.

MODE LOCAL (recommandé si vous êtes déjà sur un nœud du cluster) :
    python3 seapath_vm_report.py --local --html --json

MODE SSH (si vous lancez depuis une machine externe) :
    python3 seapath_vm_report.py \
        --hosts ccv1,ccv2,ccv3 \
        --user virtu \
        [--key ~/.ssh/id_rsa] \
        --html --json

Le mode local :
  - Lit les VMs depuis  virsh list --all
  - Récupère l'état cluster depuis  crm status / pcs status
  - Parse les contraintes Pacemaker depuis  crm configure show
  - Pour les nœuds distants (VM sur autre nœud), peut compléter via SSH si --key fourni

Dépendances :
    pip install rich          # obligatoire pour l'affichage riche
    pip install paramiko      # optionnel, seulement pour le mode SSH
"""

import argparse
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


# ─────────────────────────────────────────────────────────────────────────────
# Structures de données
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VcpuPin:
    vcpu: int
    cpuset: str
    physical_cpus: list   # list[int]

@dataclass
class VmConfig:
    name: str
    uuid: str
    state: str
    host: str
    vcpus: int
    vcpu_pins: list
    emulator_pin: str
    emulator_pin_cpus: list
    vcpu_scheduler: str
    vcpu_scheduler_priority: str
    cpu_mode: str
    cgroup_partition: str
    memory_kib: int
    hugepages: bool
    memballoon: bool
    disks: list
    interfaces: list
    raw_xml: str

@dataclass
class HostInfo:
    hostname: str
    kernel: str
    cpu_model: str
    physical_cores: int
    logical_cpus: int
    threads_per_core: int
    isolated_cpus: str
    isolated_cpus_list: list
    irq_affinity_banned: str
    ptp_sync_ok: bool

@dataclass
class PacemakerVM:
    name: str
    node: str
    state: str
    disabled: bool

@dataclass
class PacemakerConstraint:
    vm_name: str
    constraint_type: str
    node: str
    score: str
    rule: str

@dataclass
class ClusterReport:
    generated_at: str
    local_host: str
    hosts: list
    vms: list
    pacemaker_vms: list
    pacemaker_constraints: list
    pacemaker_raw: str


# ─────────────────────────────────────────────────────────────────────────────
# Runners : local et SSH
# ─────────────────────────────────────────────────────────────────────────────

def run_local(cmd: str, sudo: bool = False) -> tuple:
    if sudo and os.geteuid() != 0:
        cmd = f"sudo {cmd}"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=60)
        return r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT"
    except Exception as e:
        return "", str(e)


class LocalRunner:
    def __init__(self, hostname: str):
        self.host = hostname

    def run(self, cmd: str, sudo: bool = False) -> tuple:
        return run_local(cmd, sudo=sudo)

    def close(self):
        pass


class SSHRunner:
    def __init__(self, host, user, key_path=None, password=None, port=22):
        self.host = host
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(hostname=host, username=user, port=port, timeout=30)
        if key_path:
            kw["key_filename"] = str(key_path)
        if password:
            kw["password"] = password
        self._client.connect(**kw)

    def run(self, cmd: str, sudo: bool = False) -> tuple:
        if sudo:
            cmd = f"sudo {cmd}"
        _, o, e = self._client.exec_command(cmd, timeout=60)
        return (o.read().decode("utf-8", errors="replace").strip(),
                e.read().decode("utf-8", errors="replace").strip())

    def close(self):
        self._client.close()


# ─────────────────────────────────────────────────────────────────────────────
# Parseurs
# ─────────────────────────────────────────────────────────────────────────────

def parse_cpuset(s: str) -> list:
    """'5,7-9,12' → [5,7,8,9,12]"""
    cpus = []
    for part in str(s or "").split(","):
        part = part.strip()
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                cpus.extend(range(int(a), int(b) + 1))
            except ValueError:
                pass
        elif part.isdigit():
            cpus.append(int(part))
    return sorted(set(cpus))


def parse_virsh_list(output: str) -> list:
    vms = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("Id") or line.startswith("-"):
            continue
        parts = line.split(None, 2)
        if len(parts) >= 2:
            vms.append({
                "id":    parts[0],
                "name":  parts[1],
                "state": " ".join(parts[2:]) if len(parts) > 2 else "unknown"
            })
    return vms


def parse_vm_xml(xml_str: str, name: str, host: str, state: str) -> VmConfig:
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return VmConfig(name=name, uuid="", state=state, host=host,
                        vcpus=0, vcpu_pins=[], emulator_pin="",
                        emulator_pin_cpus=[], vcpu_scheduler="",
                        vcpu_scheduler_priority="", cpu_mode="",
                        cgroup_partition="", memory_kib=0,
                        hugepages=False, memballoon=True,
                        disks=[], interfaces=[], raw_xml=xml_str)

    uuid     = (root.findtext("uuid") or "").strip()
    vcpus    = int(root.findtext("vcpu") or 0)
    cpu_el   = root.find("cpu")
    cpu_mode = cpu_el.get("mode", "") if cpu_el is not None else ""

    vcpu_pins = []
    vcpu_scheduler = vcpu_scheduler_priority = ""
    emulator_pin_str = ""
    emulator_pin_cpus = []

    cputune = root.find("cputune")
    if cputune is not None:
        for pin in cputune.findall("vcpupin"):
            cs = pin.get("cpuset", "")
            vcpu_pins.append(VcpuPin(int(pin.get("vcpu", 0)), cs, parse_cpuset(cs)))
        ep = cputune.find("emulatorpin")
        if ep is not None:
            emulator_pin_str = ep.get("cpuset", "")
            emulator_pin_cpus = parse_cpuset(emulator_pin_str)
        vs = cputune.find("vcpusched")
        if vs is not None:
            vcpu_scheduler          = vs.get("scheduler", "")
            vcpu_scheduler_priority = vs.get("priority", "")

    cgroup_partition = ""
    res_el = root.find("resource")
    if res_el is not None:
        p = res_el.find("partition")
        if p is not None:
            cgroup_partition = (p.text or "").strip()

    mem_el     = root.find("memory")
    memory_kib = int(mem_el.text) if mem_el is not None else 0
    hugepages  = root.find(".//memoryBacking/hugepages") is not None

    memballoon = True
    for b in root.findall(".//devices/memballoon"):
        if b.get("model", "") == "none":
            memballoon = False

    disks = []
    for disk in root.findall(".//devices/disk"):
        src = disk.find("source")
        tgt = disk.find("target")
        path = ""
        if src is not None:
            path = src.get("file") or src.get("dev") or src.get("name") or ""
        dev = tgt.get("dev", "") if tgt is not None else ""
        if path or dev:
            disks.append(f"{dev} → {path}" if path else dev)

    interfaces = []
    for iface in root.findall(".//devices/interface"):
        mac_el = iface.find("mac")
        mac = mac_el.get("address", "") if mac_el is not None else ""
        src_el = iface.find("source")
        src = ""
        if src_el is not None:
            src = (src_el.get("bridge") or src_el.get("network")
                   or src_el.get("dev") or "")
        interfaces.append(f"{mac} src={src}" if src else mac)

    return VmConfig(
        name=name, uuid=uuid, state=state, host=host,
        vcpus=vcpus, vcpu_pins=vcpu_pins,
        emulator_pin=emulator_pin_str, emulator_pin_cpus=emulator_pin_cpus,
        vcpu_scheduler=vcpu_scheduler,
        vcpu_scheduler_priority=vcpu_scheduler_priority,
        cpu_mode=cpu_mode, cgroup_partition=cgroup_partition,
        memory_kib=memory_kib, hugepages=hugepages, memballoon=memballoon,
        disks=disks, interfaces=interfaces, raw_xml=xml_str
    )


def parse_lscpu(output: str) -> dict:
    info = {}
    for line in output.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            info[k.strip()] = v.strip()
    return info


def parse_pacemaker_status(output: str) -> list:
    """
    Extrait les ressources VirtualDomain depuis crm status / pcs status.
    Lignes attendues :
      * PORUN (ocf:seapath:VirtualDomain): Started node2
      * SEdemo4 (ocf:seapath:VirtualDomain): Stopped (disabled)
    """
    pat = re.compile(
        r"\*\s+(\S+)\s+\([^)]*VirtualDomain[^)]*\)\s*:\s*"
        r"(Started|Stopped)\s*(?:\((\w+)\))?\s*(\S+)?",
        re.IGNORECASE
    )
    vms = []
    for line in output.splitlines():
        m = pat.search(line)
        if m:
            nm        = m.group(1)
            state     = m.group(2)
            qualifier = m.group(3) or ""
            node      = m.group(4) or ""
            disabled  = qualifier.lower() == "disabled"
            vms.append(PacemakerVM(
                name=nm,
                node=node if state.lower() == "started" else "",
                state=state + (f" ({qualifier})" if qualifier else ""),
                disabled=disabled
            ))
    return vms


def parse_pacemaker_constraints(output: str) -> list:
    constraints = []
    p_loc = re.compile(
        r"location\s+\S+\s+(\S+)\s+.*?(INFINITY|-INFINITY|\d+):\s*(\S+)")
    p_pref = re.compile(r"(\S+)\s+(prefers|avoids)\s+(\S+?)(?::(\S+))?$")
    for line in output.splitlines():
        line = line.strip()
        m = p_loc.search(line)
        if m:
            constraints.append(PacemakerConstraint(
                vm_name=m.group(1), constraint_type="location",
                node=m.group(3), score=m.group(2), rule=line))
            continue
        m = p_pref.search(line)
        if m:
            constraints.append(PacemakerConstraint(
                vm_name=m.group(1), constraint_type=m.group(2),
                node=m.group(3), score=m.group(4) or "INFINITY", rule=line))
    return constraints


# ─────────────────────────────────────────────────────────────────────────────
# Collecte
# ─────────────────────────────────────────────────────────────────────────────

def collect_host_info(runner, hostname: str) -> HostInfo:
    kernel, _    = runner.run("uname -r")
    lscpu_out, _ = runner.run("lscpu")
    lscpu        = parse_lscpu(lscpu_out)

    cpu_model    = lscpu.get("Model name", lscpu.get("CPU", "unknown"))
    phys_cores   = (int(lscpu.get("Core(s) per socket", 1))
                    * int(lscpu.get("Socket(s)", 1)))
    log_cpus     = int(lscpu.get("CPU(s)", 0))
    thr_per_core = int(lscpu.get("Thread(s) per core", 1))

    cmdline, _    = runner.run("cat /proc/cmdline")
    isolated_cpus = ""
    m = re.search(r"isolcpus=([^\s]+)", cmdline)
    if m:
        isolated_cpus = m.group(1)

    irq_banned = ""
    irq_conf, _ = runner.run("cat /etc/default/irqbalance 2>/dev/null || true")
    m = re.search(r"IRQBALANCE_BANNED_CPUS[=\s]+[\"']?([^\"'\s]+)", irq_conf)
    if m:
        irq_banned = m.group(1)

    ptp_out, _ = runner.run("systemctl is-active ptp4l 2>/dev/null || echo inactive")
    ptp_ok = "active" in ptp_out and "inactive" not in ptp_out

    return HostInfo(
        hostname=hostname, kernel=kernel, cpu_model=cpu_model,
        physical_cores=phys_cores, logical_cpus=log_cpus,
        threads_per_core=thr_per_core,
        isolated_cpus=isolated_cpus,
        isolated_cpus_list=parse_cpuset(isolated_cpus),
        irq_affinity_banned=irq_banned,
        ptp_sync_ok=ptp_ok,
    )


def collect_vms_on_host(runner, hostname: str,
                        running_only: bool = False) -> list:
    flag = "--state-running" if running_only else "--all"
    out, err = runner.run(f"virsh list {flag}", sudo=True)
    if "error" in err.lower() and not out:
        print(f"    ⚠ virsh list: {err}")
        return []
    vms = []
    for entry in parse_virsh_list(out):
        name  = entry["name"]
        state = entry["state"]
        if running_only and "running" not in state:
            continue
        xml_out, xml_err = runner.run(f"virsh dumpxml {name}", sudo=True)
        if "error" in xml_err.lower() and not xml_out.startswith("<"):
            print(f"    ⚠ dumpxml {name}: {xml_err}")
            continue
        vms.append(parse_vm_xml(xml_out, name, hostname, state))
    return vms


def collect_pacemaker(runner) -> tuple:
    raw = ""
    status_out, _ = runner.run(
        "crm status 2>/dev/null || crm_mon -1 2>/dev/null "
        "|| pcs status 2>/dev/null || echo PACEMAKER_UNAVAILABLE",
        sudo=True)
    if "PACEMAKER_UNAVAILABLE" not in status_out:
        raw += "=== Cluster status ===\n" + status_out + "\n"
    pacemaker_vms = parse_pacemaker_status(status_out)

    conf_out, _ = runner.run(
        "crm configure show 2>/dev/null "
        "|| pcs constraint list --full 2>/dev/null || echo ''",
        sudo=True)
    if conf_out:
        raw += "\n=== Constraints ===\n" + conf_out
    constraints = parse_pacemaker_constraints(conf_out)
    return raw, pacemaker_vms, constraints


# ─────────────────────────────────────────────────────────────────────────────
# Affichage console (rich)
# ─────────────────────────────────────────────────────────────────────────────

def kib_to_human(kib: int) -> str:
    if kib >= 1024 * 1024:
        return f"{kib/1024/1024:.1f} GiB"
    return f"{kib/1024:.0f} MiB"


def render_cpu_map_rich(host: HostInfo, vms_on_host: list, console) -> None:
    """
    Affiche une grille graphique des CPUs physiques pour un nœud.
    Chaque cœur physique = une colonne, avec 2 lignes si HT (thread 0, thread 1).

    Couleurs :
      violet  = VM /machine/rt        (pinné RT)
      vert    = VM /machine/nort      (pinné noRT)
      cyan    = VM slice inconnue
      jaune   = isolé mais libre
      gris    = système (non isolé)
      rouge   = conflit (deux VMs sur même CPU)
    """
    cpu_to_vms: dict = {}
    for vm in vms_on_host:
        for pin in vm.vcpu_pins:
            for cpu_id in pin.physical_cpus:
                cpu_to_vms.setdefault(cpu_id, []).append(vm)

    # Index vcpu par cpu pour l'étiquette
    cpu_to_vcpu: dict = {}
    for vm in vms_on_host:
        for pin in vm.vcpu_pins:
            for cpu_id in pin.physical_cpus:
                cpu_to_vcpu.setdefault(cpu_id, []).append(
                    (vm.name, pin.vcpu))

    isolated = set(host.isolated_cpus_list)
    n = host.logical_cpus
    threads = host.threads_per_core
    phys = host.physical_cores

    iso_str = host.isolated_cpus or "—"
    ptp_str = "[green]✓[/green]" if host.ptp_sync_ok else "[red]✗[/red]"
    console.print(
        f"\n  [bold white]{host.hostname}[/bold white]  "
        f"[dim]{host.cpu_model[:55]}[/dim]\n"
        f"  {phys} cœurs phys. · {n} CPUs logiques · "
        f"HT={threads} · isolcpus=[yellow]{iso_str}[/yellow] · PTP {ptp_str}"
    )

    def cpu_style_label(cpu_id):
        """Retourne (style_rich, label_2chars, tooltip)."""
        if cpu_id >= n:
            return None, None, None
        vms_here = list({v.name: v for v in cpu_to_vms.get(cpu_id, [])}.values())
        if len(vms_here) > 1:
            return "bold white on red", "!!", "/".join(v.name for v in vms_here)
        elif len(vms_here) == 1:
            vm = vms_here[0]
            lbl = vm.name[:2].upper()
            if "nort" in vm.cgroup_partition:
                return "bold white on dark_green", lbl, vm.name
            elif "rt" in vm.cgroup_partition:
                return "bold white on dark_violet", lbl, vm.name
            else:
                return "bold white on dark_cyan", lbl, vm.name
        elif cpu_id in isolated:
            return "bold black on yellow3", "~~", f"CPU{cpu_id} isolé libre"
        else:
            return "dim white on grey30", "  ", f"CPU{cpu_id} système"

    COLS = 12   # cœurs physiques par ligne de grille

    for row_start in range(0, phys, COLS):
        cores = list(range(row_start, min(row_start + COLS, phys)))

        # Ligne d'en-tête des numéros de cœur physique
        hdr = "  " + " ".join(f"[dim]C{c:02d}[/dim]" for c in cores)
        console.print(hdr)

        # Ligne thread 0 (cpu_id = core_phys)
        t0_parts = []
        for c in cores:
            style, lbl, _ = cpu_style_label(c)
            if style is None:
                t0_parts.append("    ")
            else:
                t0_parts.append(f"[{style}]{lbl}[/{style}][dim]{c:02d}[/dim]")
        console.print("  " + " ".join(t0_parts))

        # Ligne thread 1 (cpu_id = core_phys + phys) si HT activé
        if threads == 2:
            t1_parts = []
            for c in cores:
                cpu1 = c + phys
                style, lbl, _ = cpu_style_label(cpu1)
                if style is None:
                    t1_parts.append("    ")
                else:
                    t1_parts.append(
                        f"[{style}]{lbl}[/{style}][dim]{cpu1:02d}[/dim]")
            console.print("  " + " ".join(t1_parts))

        console.print()

    # Légende
    console.print(
        "  [bold white on dark_violet] RT [/bold white on dark_violet] /machine/rt  "
        "[bold white on dark_green] noRT [/bold white on dark_green] /machine/nort  "
        "[bold white on dark_cyan] VM [/bold white on dark_cyan] autre  "
        "[bold black on yellow3] ~~ [/bold black on yellow3] isolé libre  "
        "[dim white on grey30]    [/dim white on grey30] système  "
        "[bold white on red] !! [/bold white on red] conflit"
    )

    # Tableau des affectations vCPU
    if vms_on_host:
        t = Table(box=box.MINIMAL, show_header=True,
                  header_style="bold dim", padding=(0, 1))
        t.add_column("VM", style="bold", min_width=16)
        t.add_column("État")
        t.add_column("vCPU → CPU physique", style="yellow")
        t.add_column("Sched.")
        t.add_column("Slice", style="magenta")
        t.add_column("RAM")
        t.add_column("HP")
        for vm in sorted(vms_on_host, key=lambda v: v.name):
            state_s = ("[green]running[/green]" if "running" in vm.state
                       else f"[dim]{vm.state}[/dim]")
            pins_s = "  ".join(
                f"vCPU{p.vcpu}→{','.join(map(str,p.physical_cpus))}"
                for p in sorted(vm.vcpu_pins, key=lambda x: x.vcpu)
            ) or "[dim]non pinné[/dim]"
            sched = (vm.vcpu_scheduler or "—") + (
                f" p{vm.vcpu_scheduler_priority}" if vm.vcpu_scheduler_priority else "")
            hp = "[green]✓[/green]" if vm.hugepages else "[dim]✗[/dim]"
            sc = ("magenta" if "rt" in vm.cgroup_partition
                              and "nort" not in vm.cgroup_partition
                  else "green" if "nort" in vm.cgroup_partition else "cyan")
            t.add_row(
                f"[{sc}]{vm.name}[/{sc}]", state_s, pins_s, sched,
                vm.cgroup_partition or "—", kib_to_human(vm.memory_kib), hp)
        console.print(t)


def print_console_report(report: ClusterReport,
                         no_color: bool = False,
                         running_only: bool = False):
    if not HAS_RICH or no_color:
        _print_plain(report)
        return

    console = Console()
    vms = [v for v in report.vms if not running_only or "running" in v.state]
    n_run = sum(1 for v in report.vms if "running" in v.state)

    console.print(Panel(
        f"[bold white]Rapport allocation ressources — Cluster SEAPATH vPAC[/bold white]\n"
        f"Généré le [cyan]{report.generated_at}[/cyan]  |  "
        f"Nœud local : [yellow]{report.local_host}[/yellow]  |  "
        f"[yellow]{len(report.hosts)}[/yellow] nœud(s)  |  "
        f"[green]{n_run}[/green]/{len(report.vms)} VMs actives",
        box=box.DOUBLE_EDGE, style="blue"
    ))

    # ① Pacemaker
    console.print("\n[bold blue]① Vue cluster Pacemaker[/bold blue]")
    if report.pacemaker_vms:
        cmap: dict = {}
        for c in report.pacemaker_constraints:
            cmap.setdefault(c.vm_name, []).append(c)

        pt = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan")
        pt.add_column("Ressource VM", style="bold")
        pt.add_column("État Pacemaker")
        pt.add_column("Nœud courant")
        pt.add_column("Contrainte placement")
        pt.add_column("Score")
        for pvm in sorted(report.pacemaker_vms, key=lambda v: v.name):
            if pvm.disabled:
                ss = "[dim]Stopped (disabled)[/dim]"
            elif "started" in pvm.state.lower():
                ss = f"[green]{pvm.state}[/green]"
            else:
                ss = f"[red]{pvm.state}[/red]"
            constrs = cmap.get(pvm.name, [])
            c_str = " / ".join(f"{c.constraint_type}→{c.node}" for c in constrs) or "[dim]aucune[/dim]"
            s_str = " / ".join(c.score for c in constrs) or ""
            pt.add_row(pvm.name, ss, pvm.node or "[dim]—[/dim]", c_str, s_str)
        console.print(pt)
    else:
        console.print("[dim]  Pacemaker non disponible[/dim]")

    # ② Carte CPU
    console.print("\n[bold blue]② Carte des CPUs physiques par nœud[/bold blue]")
    console.print(
        "[dim]  Chaque colonne = un cœur physique. "
        "2 lignes par cœur si HyperThreading (thread 0 en haut, thread 1 en bas).\n"
        "  Les 2 lettres dans chaque case = 2 premiers caractères du nom de VM pinnée.[/dim]"
    )
    for host in report.hosts:
        vms_here = [v for v in vms if v.host == host.hostname]
        render_cpu_map_rich(host, vms_here, console)

    # ③ Tableau VMs
    console.print("\n[bold blue]③ Configuration détaillée des VMs[/bold blue]")
    vt = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan")
    vt.add_column("VM", style="bold")
    vt.add_column("État", justify="center")
    vt.add_column("Nœud")
    vt.add_column("vCPU → CPU phys.", style="yellow")
    vt.add_column("Sched/Prio", justify="center")
    vt.add_column("emulatorpin")
    vt.add_column("Slice cgroup", style="magenta")
    vt.add_column("RAM")
    vt.add_column("HP", justify="center")
    vt.add_column("Balloon", justify="center")
    for vm in sorted(vms, key=lambda v: v.name):
        sc = ("magenta" if "rt" in vm.cgroup_partition
                          and "nort" not in vm.cgroup_partition
              else "green" if "nort" in vm.cgroup_partition else "cyan")
        ss = ("[green]running[/green]" if "running" in vm.state
              else f"[dim]{vm.state}[/dim]")
        pins = "\n".join(
            f"vCPU{p.vcpu}→CPU {','.join(map(str,p.physical_cpus))}"
            for p in sorted(vm.vcpu_pins, key=lambda x: x.vcpu)
        ) or "[dim]non pinné[/dim]"
        sched = (vm.vcpu_scheduler or "—") + (
            f" p={vm.vcpu_scheduler_priority}" if vm.vcpu_scheduler_priority else "")
        emul = ",".join(map(str, vm.emulator_pin_cpus)) if vm.emulator_pin_cpus else "—"
        hp  = "[green]✓[/green]" if vm.hugepages    else "[dim]✗[/dim]"
        bal = "[red]✓[/red]"     if vm.memballoon   else "[dim]✗[/dim]"
        vt.add_row(f"[{sc}]{vm.name}[/{sc}]", ss, vm.host,
                   pins, sched, emul,
                   vm.cgroup_partition or "—",
                   kib_to_human(vm.memory_kib), hp, bal)
    console.print(vt)

    # ④ Alertes
    console.print("\n[bold blue]④ Alertes[/bold blue]")
    alerts = []
    cpu_vm_map: dict = {}
    for vm in vms:
        for pin in vm.vcpu_pins:
            for cpu_id in pin.physical_cpus:
                cpu_vm_map.setdefault(cpu_id, []).append(vm.name)
    for cpu_id, names in cpu_vm_map.items():
        if len(set(names)) > 1:
            alerts.append(f"[red]🔴 CONFLIT[/red] CPU {cpu_id} partagé : "
                          f"{', '.join(set(names))}")
    for vm in vms:
        is_rt = ("rt" in vm.cgroup_partition
                 and "nort" not in vm.cgroup_partition)
        if is_rt and not vm.vcpu_pins:
            alerts.append(f"[yellow]⚠[/yellow] {vm.name}: VM RT sans vcpupin")
        if is_rt and vm.memballoon:
            alerts.append(f"[yellow]⚠[/yellow] {vm.name}: memballoon sur VM RT")
        if is_rt and not vm.hugepages:
            alerts.append(f"[yellow]⚠[/yellow] {vm.name}: hugepages désactivé sur VM RT")
        if is_rt and vm.vcpu_scheduler.lower() not in ("fifo", "sched_fifo"):
            alerts.append(f"[yellow]⚠[/yellow] {vm.name}: scheduler="
                          f"[yellow]{vm.vcpu_scheduler or 'non défini'}[/yellow] (attendu FIFO)")
    for h in report.hosts:
        if not h.ptp_sync_ok:
            alerts.append(f"[red]🔴[/red] {h.hostname}: ptp4l inactif")
    if alerts:
        for a in alerts:
            console.print(f"  {a}")
    else:
        console.print("  [green]✓ Aucune alerte[/green]")


def _print_plain(report: ClusterReport):
    print(f"\n=== SEAPATH {report.generated_at} | {report.local_host} ===")
    for pvm in report.pacemaker_vms:
        print(f"  {pvm.name}: {pvm.state} sur {pvm.node or '—'}")
    for vm in report.vms:
        print(f"\n  VM: {vm.name} [{vm.state}] sur {vm.host}")
        for p in vm.vcpu_pins:
            print(f"    vCPU{p.vcpu} → CPU {p.physical_cpus}")
        print(f"    Slice:{vm.cgroup_partition}  RAM:{kib_to_human(vm.memory_kib)}")


# ─────────────────────────────────────────────────────────────────────────────
# Export JSON
# ─────────────────────────────────────────────────────────────────────────────

def export_json(report: ClusterReport, output_dir: Path) -> Path:
    ts = report.generated_at.replace(":", "-").replace(" ", "_")
    fname = output_dir / f"seapath_report_{ts}.json"
    data = {
        "generated_at": report.generated_at,
        "local_host": report.local_host,
        "hosts": [asdict(h) for h in report.hosts],
        "vms": [asdict(v) for v in report.vms],
        "pacemaker_vms": [asdict(p) for p in report.pacemaker_vms],
        "pacemaker_constraints": [asdict(c) for c in report.pacemaker_constraints],
        "pacemaker_raw": report.pacemaker_raw,
    }
    with open(fname, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"✓ JSON : {fname}")
    return fname


# ─────────────────────────────────────────────────────────────────────────────
# Export HTML
# ─────────────────────────────────────────────────────────────────────────────

def _cpu_td(cpu_id: int, cpu_to_vms: dict, isolated: set, n: int) -> str:
    if cpu_id >= n:
        return '<td class="cpu-empty"></td>'
    vms_here = list({v.name: v for v in cpu_to_vms.get(cpu_id, [])}.values())
    if len(vms_here) > 1:
        cls, lbl = "cpu-conflict", "!!"
        tip = " / ".join(v.name for v in vms_here)
    elif len(vms_here) == 1:
        vm  = vms_here[0]
        lbl = vm.name[:3]
        tip = vm.name
        if "nort" in vm.cgroup_partition:
            cls = "cpu-nort"
        elif "rt" in vm.cgroup_partition:
            cls = "cpu-rt"
        else:
            cls = "cpu-vm"
    elif cpu_id in isolated:
        cls, lbl, tip = "cpu-iso", "~~", f"CPU {cpu_id} isolé libre"
    else:
        cls, lbl, tip = "cpu-sys", str(cpu_id), f"CPU {cpu_id} système"
    return (f'<td class="cpu-cell {cls}" title="{tip}">'
            f'<div class="cpu-num">{cpu_id}</div>'
            f'<div class="cpu-lbl">{lbl}</div></td>')


def _cpu_map_html(host: HostInfo, vms_on_host: list) -> str:
    cpu_to_vms: dict = {}
    for vm in vms_on_host:
        for pin in vm.vcpu_pins:
            for cpu_id in pin.physical_cpus:
                cpu_to_vms.setdefault(cpu_id, []).append(vm)

    isolated = set(host.isolated_cpus_list)
    n        = host.logical_cpus
    threads  = host.threads_per_core
    phys     = host.physical_cores
    COLS     = 12

    rows = ""
    for rs in range(0, phys, COLS):
        cores = list(range(rs, min(rs + COLS, phys)))
        hdr = "".join(f'<th class="ch">C{c:02d}</th>' for c in cores)
        t0  = "".join(_cpu_td(c,        cpu_to_vms, isolated, n) for c in cores)
        rows += f"<tr><th class='rl'></th>{hdr}</tr>"
        rows += f"<tr><th class='rl'>T0</th>{t0}</tr>"
        if threads == 2:
            t1 = "".join(_cpu_td(c + phys, cpu_to_vms, isolated, n) for c in cores)
            rows += f"<tr><th class='rl'>T1</th>{t1}</tr>"
        rows += "<tr class='sep'><td colspan='99'></td></tr>"

    ptp = '✓' if host.ptp_sync_ok else '✗'
    return f"""
<div class="cpumap">
  <h3>{host.hostname}
    <small>{host.cpu_model[:55]}
      &nbsp;·&nbsp;{phys} cores / {n} CPUs
      &nbsp;·&nbsp;isolcpus:<code>{host.isolated_cpus or '—'}</code>
      &nbsp;·&nbsp;PTP:{ptp}</small>
  </h3>
  <div class="legend">
    <span class="cpu-cell cpu-rt">RT</span>&nbsp;/machine/rt&nbsp;&nbsp;
    <span class="cpu-cell cpu-nort">noRT</span>&nbsp;/machine/nort&nbsp;&nbsp;
    <span class="cpu-cell cpu-vm">VM</span>&nbsp;autre&nbsp;&nbsp;
    <span class="cpu-cell cpu-iso">~~</span>&nbsp;isolé libre&nbsp;&nbsp;
    <span class="cpu-cell cpu-sys">sys</span>&nbsp;système&nbsp;&nbsp;
    <span class="cpu-cell cpu-conflict">!!</span>&nbsp;conflit
  </div>
  <table class="cmap"><tbody>{rows}</tbody></table>
</div>"""


def export_html(report: ClusterReport, output_dir: Path,
                running_only: bool = False) -> Path:
    ts    = report.generated_at.replace(":", "-").replace(" ", "_")
    fname = output_dir / f"seapath_report_{ts}.html"

    vms = [v for v in report.vms if not running_only or "running" in v.state]

    # Pacemaker table
    cmap: dict = {}
    for c in report.pacemaker_constraints:
        cmap.setdefault(c.vm_name, []).append(c)

    pce_rows = ""
    for pvm in sorted(report.pacemaker_vms, key=lambda v: v.name):
        sc = ("ok" if "started" in pvm.state.lower() and not pvm.disabled
              else "dim" if pvm.disabled else "al")
        constrs = cmap.get(pvm.name, [])
        c_str = " | ".join(
            f"{c.constraint_type}→{c.node} ({c.score})" for c in constrs) or "—"
        pce_rows += (f"<tr><td><b>{pvm.name}</b></td>"
                     f"<td><span class='{sc}'>{pvm.state}</span></td>"
                     f"<td>{pvm.node or '—'}</td>"
                     f"<td class='mono small'>{c_str}</td></tr>")

    # CPU maps
    maps_html = "".join(
        _cpu_map_html(h, [v for v in vms if v.host == h.hostname])
        for h in report.hosts)

    # VM table
    vm_rows = ""
    for vm in sorted(vms, key=lambda v: v.name):
        ss = ("ok" if "running" in vm.state else "dim")
        pins = "<br>".join(
            f"vCPU{p.vcpu}→<b>CPU {','.join(map(str,p.physical_cpus))}</b>"
            for p in sorted(vm.vcpu_pins, key=lambda x: x.vcpu)
        ) or "<em>non pinné</em>"
        emul  = ",".join(map(str, vm.emulator_pin_cpus)) or "—"
        sched = (vm.vcpu_scheduler or "—") + (
            f" p={vm.vcpu_scheduler_priority}" if vm.vcpu_scheduler_priority else "")
        sc = ("slice-rt"   if "rt" in vm.cgroup_partition
                              and "nort" not in vm.cgroup_partition
              else "slice-nort" if "nort" in vm.cgroup_partition else "")
        hp  = "✓" if vm.hugepages  else "✗"
        bal = f'<span class="al">✓</span>' if vm.memballoon else "✗"
        disks = "<br>".join(vm.disks[:3]) or "—"
        vm_rows += (f"<tr>"
                    f"<td><b>{vm.name}</b></td>"
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
<html lang="fr"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEAPATH — {report.generated_at}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',Arial,sans-serif;font-size:13px;background:#f4f6f9;color:#222}}
header{{background:#1F3864;color:#fff;padding:14px 28px}}
h1{{font-size:17px;font-weight:600}}
.sub{{font-size:11px;opacity:.7;margin-top:3px}}
.wrap{{max-width:1700px;margin:0 auto;padding:18px 12px}}
section{{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.1);margin-bottom:20px;overflow:hidden}}
section>h2{{background:#1F3864;color:#fff;padding:8px 14px;font-size:13px;font-weight:600}}
table{{width:100%;border-collapse:collapse}}
th{{background:#2E5F9E;color:#fff;padding:6px 8px;text-align:left;font-size:11px;font-weight:600}}
td{{padding:5px 8px;border-bottom:1px solid #eee;vertical-align:top}}
tr:hover td{{background:#f0f4ff}}
.mono{{font-family:Consolas,monospace;font-size:11px}}
.small{{font-size:11px}}
.c{{text-align:center}}
.ok{{color:#1a7a1a;font-weight:bold}}
.al{{color:#c00;font-weight:bold}}
.dim{{color:#888}}
.slice-rt{{background:#e1d5e7;color:#4a0072;border-radius:3px;padding:1px 4px;font-size:10px;font-weight:bold}}
.slice-nort{{background:#d5e8d4;color:#2d6a2d;border-radius:3px;padding:1px 4px;font-size:10px;font-weight:bold}}
pre{{background:#1e1e2e;color:#cdd6f4;padding:12px;font-size:11px;overflow-x:auto;line-height:1.5}}
/* CPU map */
.cpumap{{padding:12px 16px 8px}}
.cpumap h3{{font-size:13px;color:#1F3864;margin-bottom:6px}}
.cpumap small{{font-size:10px;color:#666;margin-left:6px;font-weight:400}}
.legend{{font-size:11px;color:#555;margin-bottom:8px}}
.legend .cpu-cell{{display:inline-flex;width:26px;height:20px;align-items:center;justify-content:center;font-size:9px;border-radius:3px;vertical-align:middle}}
.cmap{{border-collapse:separate;border-spacing:2px 2px}}
.ch{{background:none;color:#888;font-size:9px;text-align:center;padding:0 1px;border:none;font-weight:normal}}
.rl{{background:none;color:#aaa;font-size:9px;text-align:right;padding:0 3px;border:none;font-weight:normal;white-space:nowrap}}
.sep td{{height:5px;border:none;background:transparent}}
.cpu-cell{{width:38px;height:38px;text-align:center;vertical-align:middle;border-radius:5px;cursor:default;border:1px solid rgba(0,0,0,.08)}}
.cpu-num{{font-size:9px;opacity:.7;line-height:1}}
.cpu-lbl{{font-size:12px;font-weight:bold;line-height:1.3}}
.cpu-rt{{background:#7b2d8b;color:#fff}}
.cpu-nort{{background:#2d6a2d;color:#fff}}
.cpu-vm{{background:#1a5a8a;color:#fff}}
.cpu-iso{{background:#c8a000;color:#222}}
.cpu-sys{{background:#e0e0e0;color:#555}}
.cpu-conflict{{background:#c00;color:#fff}}
.cpu-empty{{background:transparent;border:none}}
</style></head><body>
<header>
  <h1>Rapport allocation ressources — Cluster SEAPATH vPAC</h1>
  <div class="sub">Généré le {report.generated_at} &nbsp;|&nbsp;
    Nœud local : {report.local_host} &nbsp;|&nbsp;
    {len(report.hosts)} nœud(s) &nbsp;|&nbsp;
    {sum(1 for v in report.vms if 'running' in v.state)}/{len(report.vms)} VMs actives
  </div>
</header>
<div class="wrap">

<section>
<h2>① Vue Pacemaker — ressources VirtualDomain</h2>
<table><thead><tr><th>Ressource</th><th>État</th><th>Nœud</th><th>Contraintes</th></tr></thead>
<tbody>{pce_rows or '<tr><td colspan="4"><em>Non disponible</em></td></tr>'}</tbody></table>
</section>

<section>
<h2>② Carte des CPUs physiques par nœud</h2>
{maps_html}
</section>

<section>
<h2>③ Configuration détaillée des VMs</h2>
<table><thead><tr>
  <th>VM</th><th>État</th><th>Nœud</th><th>vCPU→CPU phys.</th>
  <th>Sched.</th><th>emulatorpin</th><th>Slice</th>
  <th>RAM</th><th>HP</th><th>Balloon</th><th>Disques</th>
</tr></thead><tbody>{vm_rows}</tbody></table>
</section>

<section>
<h2>④ État Pacemaker brut</h2>
<pre>{raw_esc or "Non disponible"}</pre>
</section>

</div></body></html>"""

    with open(fname, "w") as f:
        f.write(html)
    print(f"✓ HTML : {fname}")
    return fname


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Rapport allocation ressources SEAPATH vPAC v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  # Depuis un nœud du cluster (recommandé) :
  python3 seapath_vm_report.py --local --html --json

  # Seulement les VMs en cours :
  python3 seapath_vm_report.py --local --running-only --html

  # Depuis une machine externe (SSH) :
  python3 seapath_vm_report.py --hosts ccv1,ccv2,ccv3 --user virtu --key ~/.ssh/id_rsa --html
        """
    )
    parser.add_argument("--local", action="store_true",
                        help="Exécution locale (virsh + crm sans SSH)")
    parser.add_argument("--hosts", default="",
                        help="Nœuds distants séparés par virgule (mode SSH)")
    parser.add_argument("--user",     default=None)
    parser.add_argument("--key",      default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--port",     type=int, default=22)
    parser.add_argument("--running-only", action="store_true",
                        help="Ne traiter que les VMs en état 'running'")
    parser.add_argument("--output-dir", default="./seapath_reports")
    parser.add_argument("--html",     action="store_true")
    parser.add_argument("--json",     action="store_true")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    if not args.local and not args.hosts:
        print("Exemple : python3 seapath_vm_report.py --local --html --json")
        parser.print_help()
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    now        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    local_host, _ = run_local("hostname -s")
    local_host = local_host or "unknown"

    runners = []
    if args.local:
        runners.append(LocalRunner(local_host))
    if args.hosts:
        if not HAS_PARAMIKO:
            print("ERROR: pip install paramiko  (requis pour --hosts)")
            sys.exit(1)
        for h in [x.strip() for x in args.hosts.split(",") if x.strip()]:
            try:
                runners.append(SSHRunner(
                    host=h, user=args.user or "root",
                    key_path=args.key, password=args.password, port=args.port))
            except Exception as e:
                print(f"  ✗ SSH {h}: {e}")

    if not runners:
        print("Aucun nœud accessible.")
        sys.exit(1)

    print(f"\n=== Collecte sur {len(runners)} nœud(s) ===\n")

    all_hosts, all_vms = [], []
    pacemaker_raw, pacemaker_vms, pacemaker_constraints = "", [], []
    pacemaker_done = False

    for runner in runners:
        hn = runner.host
        print(f"[{hn}]")
        try:
            hi = collect_host_info(runner, hn)
            all_hosts.append(hi)
            print(f"  CPU : {hi.cpu_model[:45]}  "
                  f"({hi.physical_cores}P/{hi.logical_cpus}L HT={hi.threads_per_core})")
            print(f"  isolcpus : {hi.isolated_cpus or '—'}")
        except Exception as e:
            print(f"  ✗ host info: {e}")
            runner.close(); continue

        try:
            vms = collect_vms_on_host(runner, hn, running_only=args.running_only)
            all_vms.extend(vms)
            print(f"  VMs : {len(vms)} trouvée(s)")
            for vm in vms:
                pin_str = [p.cpuset for p in vm.vcpu_pins] or ["non pinné"]
                print(f"    {vm.name} [{vm.state}]  pins={pin_str}")
        except Exception as e:
            print(f"  ✗ VMs: {e}")

        if not pacemaker_done:
            try:
                pacemaker_raw, pacemaker_vms, pacemaker_constraints = \
                    collect_pacemaker(runner)
                if pacemaker_vms:
                    pacemaker_done = True
                    print(f"  Pacemaker : {len(pacemaker_vms)} ressource(s) VM")
                    for pvm in pacemaker_vms:
                        print(f"    {pvm.name}: {pvm.state}"
                              + (f" → {pvm.node}" if pvm.node else ""))
            except Exception as e:
                print(f"  ✗ Pacemaker: {e}")

        runner.close()
        print()

    # Compléter avec les infos Pacemaker pour les nœuds non collectés via virsh
    found_names  = {v.name for v in all_vms}
    known_hosts  = {h.hostname for h in all_hosts}
    for pvm in pacemaker_vms:
        if pvm.name not in found_names and pvm.node and not pvm.disabled:
            all_vms.append(VmConfig(
                name=pvm.name, uuid="", state=pvm.state, host=pvm.node,
                vcpus=0, vcpu_pins=[], emulator_pin="", emulator_pin_cpus=[],
                vcpu_scheduler="", vcpu_scheduler_priority="",
                cpu_mode="", cgroup_partition="", memory_kib=0,
                hugepages=False, memballoon=False,
                disks=[], interfaces=[],
                raw_xml="(VM sur nœud distant — XML non collecté)"))
        if pvm.node and pvm.node not in known_hosts:
            all_hosts.append(HostInfo(
                hostname=pvm.node, kernel="—", cpu_model="—",
                physical_cores=0, logical_cpus=0, threads_per_core=2,
                isolated_cpus="", isolated_cpus_list=[],
                irq_affinity_banned="", ptp_sync_ok=False))
            known_hosts.add(pvm.node)

    report = ClusterReport(
        generated_at=now, local_host=local_host,
        hosts=all_hosts, vms=all_vms,
        pacemaker_vms=pacemaker_vms,
        pacemaker_constraints=pacemaker_constraints,
        pacemaker_raw=pacemaker_raw,
    )

    print_console_report(report, no_color=args.no_color,
                         running_only=args.running_only)
    if args.html:
        export_html(report, output_dir, running_only=args.running_only)
    if args.json:
        export_json(report, output_dir)
    if not args.html and not args.json:
        print("\n[Astuce] Ajoutez --html et/ou --json pour exporter")


if __name__ == "__main__":
    main()
