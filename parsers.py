"""
parsers.py
----------
Pure functions that convert raw command output (strings) into structured
data objects. No I/O, no network calls — only string processing.
"""

import re
import xml.etree.ElementTree as ET

from models import VcpuPin, VmConfig, PacemakerVM, PacemakerConstraint


def parse_cpuset(s: str) -> list:
    """
    Expand a CPU set string into a sorted list of integer CPU ids.
    Example: '5,7-9,12' -> [5, 7, 8, 9, 12]
    """
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
    """
    Parse 'virsh list --all' output into a list of dicts with keys:
    id, name, state.
    """
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


def parse_lscpu(output: str) -> dict:
    """Parse 'lscpu' output into a key/value dict."""
    info = {}
    for line in output.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            info[k.strip()] = v.strip()
    return info


def parse_pacemaker_status(output: str) -> list:
    """
    Extract VirtualDomain resources from 'crm status' / 'pcs status' output.
    Returns a list of PacemakerVM objects.

    Handled formats:
      * PORUN (ocf:seapath:VirtualDomain): Started node2
      * SEdemo4 (ocf:seapath:VirtualDomain): Stopped (disabled)
    """
    pattern = re.compile(
        r"\*\s+(\S+)\s+\([^)]*VirtualDomain[^)]*\)\s*:\s*"
        r"(Started|Stopped)\s*(?:\((\w+)\))?\s*(\S+)?",
        re.IGNORECASE
    )
    vms = []
    for line in output.splitlines():
        m = pattern.search(line)
        if m:
            name      = m.group(1)
            state     = m.group(2)
            qualifier = m.group(3) or ""
            node      = m.group(4) or ""
            disabled  = qualifier.lower() == "disabled"
            vms.append(PacemakerVM(
                name=name,
                node=node if state.lower() == "started" else "",
                state=state + (f" ({qualifier})" if qualifier else ""),
                disabled=disabled,
            ))
    return vms


def parse_pacemaker_constraints(output: str) -> list:
    """
    Extract placement constraints from 'crm configure show' or
    'pcs constraint list' output. Returns a list of PacemakerConstraint.
    """
    constraints = []
    p_loc  = re.compile(
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


def parse_vm_xml(xml_str: str, name: str, host: str, state: str) -> VmConfig:
    """
    Parse a libvirt XML domain definition (from 'virsh dumpxml') into a
    VmConfig. Returns a mostly-empty VmConfig if the XML cannot be parsed.
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return VmConfig(
            name=name, uuid="", state=state, host=host,
            vcpus=0, vcpu_pins=[], emulator_pin="", emulator_pin_cpus=[],
            vcpu_scheduler="", vcpu_scheduler_priority="", cpu_mode="",
            cgroup_partition="", memory_kib=0, hugepages=False,
            memballoon=True, disks=[], interfaces=[], raw_xml=xml_str)

    uuid     = (root.findtext("uuid") or "").strip()
    vcpus    = int(root.findtext("vcpu") or 0)
    cpu_el   = root.find("cpu")
    cpu_mode = cpu_el.get("mode", "") if cpu_el is not None else ""

    # CPU tuning: per-vCPU pinning, emulator pin, RT scheduler
    vcpu_pins = []
    vcpu_scheduler = vcpu_scheduler_priority = ""
    emulator_pin_str = ""
    emulator_pin_cpus = []

    cputune = root.find("cputune")
    if cputune is not None:
        for pin in cputune.findall("vcpupin"):
            cs = pin.get("cpuset", "")
            vcpu_pins.append(
                VcpuPin(int(pin.get("vcpu", 0)), cs, parse_cpuset(cs)))
        ep = cputune.find("emulatorpin")
        if ep is not None:
            emulator_pin_str  = ep.get("cpuset", "")
            emulator_pin_cpus = parse_cpuset(emulator_pin_str)
        vs = cputune.find("vcpusched")
        if vs is not None:
            vcpu_scheduler          = vs.get("scheduler", "")
            vcpu_scheduler_priority = vs.get("priority", "")

    # cgroup partition determines RT vs noRT classification:
    # /machine/rt -> real-time, /machine/nort -> non-RT
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
            path = (src.get("file") or src.get("dev")
                    or src.get("name") or "")
        dev = tgt.get("dev", "") if tgt is not None else ""
        if path or dev:
            disks.append(f"{dev} -> {path}" if path else dev)

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
        disks=disks, interfaces=interfaces, raw_xml=xml_str,
    )
