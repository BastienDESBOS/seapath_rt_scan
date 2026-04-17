#!/usr/bin/env python3
"""
main.py
-------
Command-line entry point for the SEAPATH RT scan tool.

Typical usage from a machine outside the cluster (SSH mode):

    python3 main.py --hosts ccv1,ccv2,ccv3 --user virtu --key ~/.ssh/id_rsa --html --json

See README.md for full documentation.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from models import VmConfig, HostInfo, ClusterReport
from runners import LocalRunner, SSHRunner, HAS_PARAMIKO, run_local
from scan_hypervisors import collect_host_info, collect_pacemaker
from scan_vms import collect_vms_on_host
from report_console import print_console_report
from report_html import export_html
from report_json import export_json


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SEAPATH vPAC — RT configuration scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # From an external machine via SSH (primary use case):
  python3 main.py --hosts ccv1,ccv2,ccv3 --user virtu --key ~/.ssh/id_rsa --html --json

  # Only running VMs:
  python3 main.py --hosts ccv1,ccv2,ccv3 --user virtu --running-only --html

  # From a cluster node directly (local mode):
  python3 main.py --local --html --json
        """
    )
    parser.add_argument(
        "--local", action="store_true",
        help="Run locally without SSH (use when already on a cluster node)")
    parser.add_argument(
        "--hosts", default="",
        help="Comma-separated cluster node hostnames (SSH mode)")
    parser.add_argument("--user",     default=None, help="SSH username")
    parser.add_argument("--key",      default=None, help="Path to SSH private key")
    parser.add_argument("--password", default=None, help="SSH password (prefer --key)")
    parser.add_argument("--port",     type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument(
        "--running-only", action="store_true",
        help="Only collect VMs currently in 'running' state")
    parser.add_argument(
        "--output-dir", default="./seapath_reports",
        help="Output directory for HTML/JSON files (default: ./seapath_reports)")
    parser.add_argument("--html",     action="store_true", help="Export HTML report")
    parser.add_argument("--json",     action="store_true", help="Export JSON report")
    parser.add_argument("--no-color", action="store_true", help="Disable terminal colours")
    return parser


def _build_runners(args) -> list:
    """Create runner objects (local and/or SSH) from CLI arguments."""
    runners = []

    if args.local:
        local_host, _ = run_local("hostname -s")
        runners.append(LocalRunner(local_host or "localhost"))

    if args.hosts:
        if not HAS_PARAMIKO:
            print("ERROR: paramiko is required for SSH mode.")
            print("       Run: pip install paramiko")
            sys.exit(1)
        for h in [x.strip() for x in args.hosts.split(",") if x.strip()]:
            try:
                runners.append(SSHRunner(
                    host=h, user=args.user or "root",
                    key_path=args.key, password=args.password, port=args.port))
            except Exception as e:
                print(f"  SSH connection failed for {h}: {e}")

    return runners


def _collect_all(runners: list, running_only: bool) -> tuple:
    """
    Iterate over all runners to gather host info, VM configs, and
    Pacemaker state. Pacemaker is only queried from the first node
    that returns valid data (it is cluster-wide, not per-node).

    Returns:
        (all_hosts, all_vms, pacemaker_raw, pacemaker_vms, pacemaker_constraints)
    """
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
            print(f"  ERROR host info: {e}")
            runner.close()
            continue

        try:
            vms = collect_vms_on_host(runner, hn, running_only=running_only)
            all_vms.extend(vms)
            print(f"  VMs : {len(vms)} found")
            for vm in vms:
                pins = [p.cpuset for p in vm.vcpu_pins] or ["not pinned"]
                print(f"    {vm.name} [{vm.state}]  pins={pins}")
        except Exception as e:
            print(f"  ERROR VMs: {e}")

        if not pacemaker_done:
            try:
                pacemaker_raw, pacemaker_vms, pacemaker_constraints = \
                    collect_pacemaker(runner)
                if pacemaker_vms:
                    pacemaker_done = True
                    print(f"  Pacemaker : {len(pacemaker_vms)} VM resource(s)")
                    for pvm in pacemaker_vms:
                        print(f"    {pvm.name}: {pvm.state}"
                              + (f" -> {pvm.node}" if pvm.node else ""))
            except Exception as e:
                print(f"  ERROR Pacemaker: {e}")

        runner.close()
        print()

    return all_hosts, all_vms, pacemaker_raw, pacemaker_vms, pacemaker_constraints


def _fill_missing_nodes(all_hosts: list, all_vms: list,
                        pacemaker_vms: list) -> None:
    """
    Add placeholder entries for VMs and nodes that Pacemaker knows about
    but whose XML was not collected (e.g. running on a node not in --hosts).
    This ensures they still appear in the report.
    """
    found_names = {v.name for v in all_vms}
    known_hosts = {h.hostname for h in all_hosts}

    for pvm in pacemaker_vms:
        if pvm.name not in found_names and pvm.node and not pvm.disabled:
            all_vms.append(VmConfig(
                name=pvm.name, uuid="", state=pvm.state, host=pvm.node,
                vcpus=0, vcpu_pins=[], emulator_pin="", emulator_pin_cpus=[],
                vcpu_scheduler="", vcpu_scheduler_priority="", cpu_mode="",
                cgroup_partition="", memory_kib=0, hugepages=False,
                memballoon=False, disks=[], interfaces=[],
                raw_xml="(VM on remote node — XML not collected)"))

        if pvm.node and pvm.node not in known_hosts:
            all_hosts.append(HostInfo(
                hostname=pvm.node, kernel="—", cpu_model="—",
                physical_cores=0, logical_cpus=0, threads_per_core=2,
                isolated_cpus="", isolated_cpus_list=[],
                irq_affinity_banned="", ptp_sync_ok=False))
            known_hosts.add(pvm.node)


def main():
    parser = _build_arg_parser()
    args   = parser.parse_args()

    if not args.local and not args.hosts:
        print("Specify --local or --hosts.\nExample:\n"
              "  python3 main.py --hosts ccv1,ccv2,ccv3 "
              "--user virtu --key ~/.ssh/id_rsa --html")
        parser.print_help()
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    now        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    local_host, _ = run_local("hostname -s")
    local_host = local_host or "unknown"

    runners = _build_runners(args)
    if not runners:
        print("No nodes reachable. Exiting.")
        sys.exit(1)

    print(f"\n=== Scanning {len(runners)} node(s) ===\n")

    (all_hosts, all_vms, pacemaker_raw,
     pacemaker_vms, pacemaker_constraints) = _collect_all(
        runners, running_only=args.running_only)

    _fill_missing_nodes(all_hosts, all_vms, pacemaker_vms)

    report = ClusterReport(
        generated_at=now,
        local_host=local_host,
        hosts=all_hosts,
        vms=all_vms,
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
        print("\nTip: add --html and/or --json to export the report to a file.")


if __name__ == "__main__":
    main()
