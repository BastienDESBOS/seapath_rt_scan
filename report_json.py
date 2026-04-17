"""
report_json.py
--------------
Serialises a ClusterReport to a timestamped JSON file.

Entry point:
    export_json(report, output_dir) -> Path
"""

import json
from dataclasses import asdict
from pathlib import Path

from models import ClusterReport


def export_json(report: ClusterReport, output_dir: Path) -> Path:
    """
    Write the full cluster report as JSON to output_dir.
    Returns the path of the generated file.
    """
    ts    = report.generated_at.replace(":", "-").replace(" ", "_")
    fname = output_dir / f"seapath_report_{ts}.json"

    data = {
        "generated_at":         report.generated_at,
        "local_host":           report.local_host,
        "hosts":                [asdict(h) for h in report.hosts],
        "vms":                  [asdict(v) for v in report.vms],
        "pacemaker_vms":        [asdict(p) for p in report.pacemaker_vms],
        "pacemaker_constraints": [asdict(c) for c in report.pacemaker_constraints],
        "pacemaker_raw":        report.pacemaker_raw,
    }

    with open(fname, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"JSON report: {fname}")
    return fname
