"""
scan_hypervisors.py
-------------------
Collects hardware and OS-level information from hypervisor nodes:
  - CPU topology (lscpu), isolated CPUs (/proc/cmdline), IRQ affinity
  - PTP synchronisation status (ptp4l)
  - Pacemaker cluster state and placement constraints

Entry points:
    collect_host_info(runner, hostname) -> HostInfo
    collect_pacemaker(runner)           -> (raw_str, [PacemakerVM], [PacemakerConstraint])
"""

import re

from models import HostInfo
from parsers import (parse_cpuset, parse_lscpu,
                     parse_pacemaker_status, parse_pacemaker_constraints)


def collect_host_info(runner, hostname: str) -> HostInfo:
    """
    Gather CPU topology, kernel version, isolcpus, IRQ affinity settings,
    and PTP sync status from one hypervisor node.
    """
    kernel, _    = runner.run("uname -r")
    lscpu_out, _ = runner.run("lscpu")
    lscpu        = parse_lscpu(lscpu_out)

    cpu_model    = lscpu.get("Model name", lscpu.get("CPU", "unknown"))
    phys_cores   = (int(lscpu.get("Core(s) per socket", 1))
                    * int(lscpu.get("Socket(s)", 1)))
    log_cpus     = int(lscpu.get("CPU(s)", 0))
    thr_per_core = int(lscpu.get("Thread(s) per core", 1))

    # isolcpus is set in the kernel command line to reserve CPUs for RT VMs
    cmdline, _    = runner.run("cat /proc/cmdline")
    isolated_cpus = ""
    m = re.search(r"isolcpus=([^\s]+)", cmdline)
    if m:
        isolated_cpus = m.group(1)

    # IRQBALANCE_BANNED_CPUS prevents IRQ balancer from using RT CPUs
    irq_banned = ""
    irq_conf, _ = runner.run(
        "cat /etc/default/irqbalance 2>/dev/null || true")
    m = re.search(
        r"IRQBALANCE_BANNED_CPUS[=\s]+[\"']?([^\"'\s]+)", irq_conf)
    if m:
        irq_banned = m.group(1)

    # PTP must be active for accurate timestamping in RT VMs
    ptp_out, _ = runner.run(
        "systemctl is-active ptp4l 2>/dev/null || echo inactive")
    ptp_ok = "active" in ptp_out and "inactive" not in ptp_out

    return HostInfo(
        hostname=hostname,
        kernel=kernel,
        cpu_model=cpu_model,
        physical_cores=phys_cores,
        logical_cpus=log_cpus,
        threads_per_core=thr_per_core,
        isolated_cpus=isolated_cpus,
        isolated_cpus_list=parse_cpuset(isolated_cpus),
        irq_affinity_banned=irq_banned,
        ptp_sync_ok=ptp_ok,
    )


def collect_pacemaker(runner) -> tuple:
    """
    Fetch Pacemaker cluster state and placement constraints from a node.
    Tries crm first, then pcs as fallback. Only needs to run on one node.

    Returns:
        (raw_text, list[PacemakerVM], list[PacemakerConstraint])
    """
    raw = ""

    # Which VMs are started and on which node
    status_out, _ = runner.run(
        "crm status 2>/dev/null || crm_mon -1 2>/dev/null "
        "|| pcs status 2>/dev/null || echo PACEMAKER_UNAVAILABLE",
        sudo=True)
    if "PACEMAKER_UNAVAILABLE" not in status_out:
        raw += "=== Cluster status ===\n" + status_out + "\n"
    pacemaker_vms = parse_pacemaker_status(status_out)

    # Placement constraints: preferred/forbidden node assignments per VM
    conf_out, _ = runner.run(
        "crm configure show 2>/dev/null "
        "|| pcs constraint list --full 2>/dev/null || echo ''",
        sudo=True)
    if conf_out:
        raw += "\n=== Constraints ===\n" + conf_out
    constraints = parse_pacemaker_constraints(conf_out)

    return raw, pacemaker_vms, constraints
