"""
scan_vms.py
-----------
Collects the resource configuration of all virtual machines on a hypervisor
by running 'virsh list' and 'virsh dumpxml', then parsing the resulting XML.

Entry point:
    collect_vms_on_host(runner, hostname, running_only=False) -> list[VmConfig]
"""

from parsers import parse_virsh_list, parse_vm_xml


def collect_vms_on_host(runner, hostname: str,
                        running_only: bool = False) -> list:
    """
    List all VMs on a node and fetch their full libvirt XML configuration.
    Returns a list of VmConfig objects.

    If running_only=True, only VMs in 'running' state are collected.
    """
    flag = "--state-running" if running_only else "--all"
    out, err = runner.run(f"virsh list {flag}", sudo=True)

    if "error" in err.lower() and not out:
        print(f"    WARNING virsh list on {hostname}: {err}")
        return []

    vms = []
    for entry in parse_virsh_list(out):
        name  = entry["name"]
        state = entry["state"]

        if running_only and "running" not in state:
            continue

        # Fetch the full XML definition for this VM
        xml_out, xml_err = runner.run(f"virsh dumpxml {name}", sudo=True)
        if "error" in xml_err.lower() and not xml_out.startswith("<"):
            print(f"    WARNING dumpxml {name}: {xml_err}")
            continue

        vms.append(parse_vm_xml(xml_out, name, hostname, state))

    return vms
