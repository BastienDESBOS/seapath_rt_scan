"""
models.py
---------
Data structures used throughout the seapath_rt_scan toolchain.
All classes are plain dataclasses, serialisable with dataclasses.asdict().
"""

from dataclasses import dataclass


@dataclass
class VcpuPin:
    """Mapping between one virtual CPU and the physical CPUs it is pinned to."""
    vcpu: int
    cpuset: str           # raw string from libvirt XML, e.g. "5,7-9"
    physical_cpus: list   # expanded list of CPU ids, e.g. [5, 7, 8, 9]


@dataclass
class VmConfig:
    """Full resource configuration of a single virtual machine."""
    name: str
    uuid: str
    state: str            # "running", "shut off", etc.
    host: str             # hypervisor hostname where the VM lives
    vcpus: int
    vcpu_pins: list       # list[VcpuPin]
    emulator_pin: str
    emulator_pin_cpus: list
    vcpu_scheduler: str   # e.g. "fifo"
    vcpu_scheduler_priority: str
    cpu_mode: str
    cgroup_partition: str # e.g. "/machine/rt" or "/machine/nort"
    memory_kib: int
    hugepages: bool
    memballoon: bool
    disks: list           # list[str]
    interfaces: list      # list[str]
    raw_xml: str


@dataclass
class HostInfo:
    """Hardware and OS-level configuration of one hypervisor node."""
    hostname: str
    kernel: str
    cpu_model: str
    physical_cores: int
    logical_cpus: int
    threads_per_core: int
    isolated_cpus: str        # raw isolcpus= value from /proc/cmdline
    isolated_cpus_list: list  # expanded list[int]
    irq_affinity_banned: str
    ptp_sync_ok: bool


@dataclass
class PacemakerVM:
    """State of one VirtualDomain resource as reported by Pacemaker."""
    name: str
    node: str    # current host, empty if not running
    state: str   # "Started", "Stopped", "Stopped (disabled)", etc.
    disabled: bool


@dataclass
class PacemakerConstraint:
    """Single placement constraint from the Pacemaker configuration."""
    vm_name: str
    constraint_type: str  # "location", "prefers", "avoids"
    node: str
    score: str            # "INFINITY", "-INFINITY", or numeric
    rule: str             # raw constraint line


@dataclass
class ClusterReport:
    """Complete snapshot of the cluster RT configuration at scan time."""
    generated_at: str
    local_host: str
    hosts: list           # list[HostInfo]
    vms: list             # list[VmConfig]
    pacemaker_vms: list   # list[PacemakerVM]
    pacemaker_constraints: list  # list[PacemakerConstraint]
    pacemaker_raw: str
