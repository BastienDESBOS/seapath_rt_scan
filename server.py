"""
server.py
---------
Flask web application for the SEAPATH RT scan tool.

Start:  python3 server.py        (or via docker-compose up)
Access: http://localhost:5000

API endpoints consumed by templates/index.html:
  GET  /                               Serve the single-page app
  POST /api/scan                       Start a background scan, returns {scan_id}
  GET  /api/scan/<id>/stream           SSE stream: progress lines + completion event
  GET  /api/reports                    List saved reports (metadata only)
  GET  /api/reports/<id>               Full report JSON + annotations
  PUT  /api/reports/<id>/annotations   Save RT vCPU annotations
  GET  /api/compare?a=<id>&b=<id>      Structured diff between two reports

Storage layout in REPORTS_DIR:
  <scan_id>.json              full report data
  <scan_id>.annotations.json  RT vCPU annotations (optional sidecar)
"""

import contextlib
import json
import os
import queue
import sys
import threading
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from models import VmConfig, HostInfo
from runners import LocalRunner, SSHRunner, run_local
from scan_hypervisors import collect_host_info, collect_pacemaker
from scan_vms import collect_vms_on_host

app = Flask(__name__)

REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "./seapath_reports"))
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Registry of active scans: scan_id -> {status, queue}
_scans: dict = {}


# ─── Storage helpers ──────────────────────────────────────────────────────────

def _rpath(sid: str) -> Path:
    return REPORTS_DIR / f"{sid}.json"

def _apath(sid: str) -> Path:
    return REPORTS_DIR / f"{sid}.annotations.json"

def _list_reports() -> list:
    """Return brief metadata for all saved reports, newest first."""
    items = []
    for p in sorted(REPORTS_DIR.glob("*.json"), reverse=True):
        if p.name.endswith(".annotations.json"):
            continue
        try:
            with open(p) as f:
                d = json.load(f)
            items.append({
                "id":           p.stem,
                "generated_at": d.get("generated_at", ""),
                "local_host":   d.get("local_host", ""),
                "n_hosts":      len(d.get("hosts", [])),
                "n_vms":        len(d.get("vms", [])),
                "n_running":    sum(1 for v in d.get("vms", [])
                                    if "running" in v.get("state", "")),
            })
        except Exception:
            pass
    return items


# ─── Background scan ─────────────────────────────────────────────────────────

@contextlib.contextmanager
def _capture_stdout(q: queue.Queue):
    """
    Redirect stdout to a queue during a scan thread so progress lines
    can be streamed to the browser via SSE.
    Note: not thread-safe if two scans run simultaneously.
    """
    class _Writer:
        def write(self, s):
            if s.strip():
                q.put({"type": "log", "msg": s.strip()})
        def flush(self):
            pass
    old, sys.stdout = sys.stdout, _Writer()
    try:
        yield
    finally:
        sys.stdout = old


def _run_scan(scan_id: str, params: dict):
    """
    Background thread: connects to cluster nodes, collects all data,
    saves the ClusterReport as JSON, then signals completion via SSE queue.
    """
    q = _scans[scan_id]["queue"]
    _scans[scan_id]["status"] = "running"

    with _capture_stdout(q):
        try:
            # Build runner list from scan parameters
            runners = []
            if params.get("local"):
                host, _ = run_local("hostname -s")
                runners.append(LocalRunner(host or "localhost"))
            for h in [x.strip() for x in params.get("hosts", "").split(",") if x.strip()]:
                try:
                    key = params.get("key") or None
                    if key:
                        key = os.path.expanduser(key)
                    runners.append(SSHRunner(
                        host=h,
                        user=params.get("user", "root"),
                        key_path=key,
                        password=params.get("password") or None,
                        port=int(params.get("port", 22)),
                    ))
                except Exception as e:
                    print(f"SSH {h} failed: {e}")

            if not runners:
                q.put({"type": "error", "msg": "No nodes reachable."})
                _scans[scan_id]["status"] = "error"
                return

            print(f"=== Scanning {len(runners)} node(s) ===\n")
            all_hosts, all_vms = [], []
            pacemaker_raw, pacemaker_vms, pacemaker_constraints = "", [], []
            pacemaker_done = False

            for runner in runners:
                hn = runner.host
                print(f"[{hn}]")
                try:
                    hi = collect_host_info(runner, hn)
                    all_hosts.append(hi)
                    print(f"  CPU : {hi.cpu_model[:45]} "
                          f"({hi.physical_cores}P/{hi.logical_cpus}L)")
                    print(f"  isolcpus : {hi.isolated_cpus or '—'}")
                except Exception as e:
                    print(f"  ERROR host info: {e}")
                    runner.close()
                    continue

                try:
                    vms = collect_vms_on_host(
                        runner, hn,
                        running_only=params.get("running_only", False))
                    all_vms.extend(vms)
                    print(f"  VMs : {len(vms)} found")
                    for vm in vms:
                        print(f"    {vm.name} [{vm.state}]")
                except Exception as e:
                    print(f"  ERROR VMs: {e}")

                if not pacemaker_done:
                    try:
                        pacemaker_raw, pacemaker_vms, pacemaker_constraints = \
                            collect_pacemaker(runner)
                        if pacemaker_vms:
                            pacemaker_done = True
                            print(f"  Pacemaker : {len(pacemaker_vms)} resource(s)")
                    except Exception as e:
                        print(f"  ERROR Pacemaker: {e}")

                runner.close()
                print()

            # Add placeholder entries for VMs/nodes known to Pacemaker
            # but not directly reachable via virsh (e.g. on other nodes)
            found = {v.name     for v in all_vms}
            known = {h.hostname for h in all_hosts}
            for pvm in pacemaker_vms:
                if pvm.name not in found and pvm.node and not pvm.disabled:
                    all_vms.append(VmConfig(
                        name=pvm.name, uuid="", state=pvm.state, host=pvm.node,
                        vcpus=0, vcpu_pins=[], emulator_pin="",
                        emulator_pin_cpus=[], vcpu_scheduler="",
                        vcpu_scheduler_priority="", cpu_mode="",
                        cgroup_partition="", memory_kib=0,
                        hugepages=False, memballoon=False,
                        disks=[], interfaces=[],
                        raw_xml="(VM on remote node — XML not collected)"))
                if pvm.node and pvm.node not in known:
                    all_hosts.append(HostInfo(
                        hostname=pvm.node, kernel="—", cpu_model="—",
                        physical_cores=0, logical_cpus=0, threads_per_core=2,
                        isolated_cpus="", isolated_cpus_list=[],
                        irq_affinity_banned="", ptp_sync_ok=False))
                    known.add(pvm.node)

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            local_host, _ = run_local("hostname -s")
            report = {
                "generated_at":          now,
                "local_host":            local_host or "unknown",
                "hosts":                 [asdict(h) for h in all_hosts],
                "vms":                   [asdict(v) for v in all_vms],
                "pacemaker_vms":         [asdict(p) for p in pacemaker_vms],
                "pacemaker_constraints": [asdict(c) for c in pacemaker_constraints],
                "pacemaker_raw":         pacemaker_raw,
            }
            with open(_rpath(scan_id), "w") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)

            print("\nScan complete.")
            _scans[scan_id]["status"] = "done"
            q.put({"type": "done", "report_id": scan_id})

        except Exception as e:
            q.put({"type": "error", "msg": str(e)})
            _scans[scan_id]["status"] = "error"


# ─── Flask routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scan", methods=["POST"])
def start_scan():
    """Start a background scan and return its ID immediately."""
    params  = request.json or {}
    scan_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    q       = queue.Queue()
    _scans[scan_id] = {"status": "starting", "queue": q}
    threading.Thread(target=_run_scan, args=(scan_id, params), daemon=True).start()
    return jsonify({"scan_id": scan_id})


@app.route("/api/scan/<scan_id>/stream")
def scan_stream(scan_id: str):
    """Server-Sent Events stream: log lines during scan, then done/error event."""
    if scan_id not in _scans:
        return jsonify({"error": "not found"}), 404

    def generate():
        q = _scans[scan_id]["queue"]
        while True:
            try:
                event = q.get(timeout=30)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'   # keep connection alive

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/reports")
def list_reports():
    return jsonify(_list_reports())


@app.route("/api/reports/<report_id>")
def get_report(report_id: str):
    p = _rpath(report_id)
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    with open(p) as f:
        data = json.load(f)
    ap = _apath(report_id)
    data["annotations"] = json.load(open(ap)) if ap.exists() else {}
    return jsonify(data)


@app.route("/api/reports/<report_id>/annotations", methods=["PUT"])
def save_annotations(report_id: str):
    """Save RT vCPU annotation. Body: {vm_name: [vcpu_idx, ...], ...}"""
    if not _rpath(report_id).exists():
        return jsonify({"error": "not found"}), 404
    with open(_apath(report_id), "w") as f:
        json.dump(request.json or {}, f, indent=2)
    return jsonify({"ok": True})


@app.route("/api/compare")
def compare():
    a_id = request.args.get("a")
    b_id = request.args.get("b")
    if not a_id or not b_id:
        return jsonify({"error": "provide ?a=<id>&b=<id>"}), 400
    try:
        with open(_rpath(a_id)) as f: a = json.load(f)
        with open(_rpath(b_id)) as f: b = json.load(f)
    except FileNotFoundError:
        return jsonify({"error": "one or both reports not found"}), 404
    return jsonify(_diff(a, b))


# ─── Report comparison ────────────────────────────────────────────────────────

def _diff(a: dict, b: dict) -> dict:
    """Return a structured diff between two report dicts."""
    result = {
        "a":         a.get("generated_at", ""),
        "b":         b.get("generated_at", ""),
        "hosts":     [],
        "vms":       [],
        "pacemaker": [],
        "summary":   "",
    }

    # Host-level changes (kernel, isolcpus, PTP, IRQ affinity)
    ah = {h["hostname"]: h for h in a.get("hosts", [])}
    bh = {h["hostname"]: h for h in b.get("hosts", [])}
    for hn in sorted(set(ah) | set(bh)):
        if hn not in ah:
            result["hosts"].append({"host": hn, "change": "added"})
        elif hn not in bh:
            result["hosts"].append({"host": hn, "change": "removed"})
        else:
            fields = {}
            for k in ("kernel", "isolated_cpus", "ptp_sync_ok", "irq_affinity_banned"):
                if ah[hn].get(k) != bh[hn].get(k):
                    fields[k] = {"before": ah[hn].get(k), "after": bh[hn].get(k)}
            if fields:
                result["hosts"].append({"host": hn, "change": "modified",
                                        "fields": fields})

    # VM-level changes
    av = {v["name"]: v for v in a.get("vms", [])}
    bv = {v["name"]: v for v in b.get("vms", [])}
    for name in sorted(set(av) | set(bv)):
        if name not in av:
            result["vms"].append({"vm": name, "change": "added",
                                  "host": bv[name].get("host")})
        elif name not in bv:
            result["vms"].append({"vm": name, "change": "removed",
                                  "host": av[name].get("host")})
        else:
            fields = {}
            for k in ("state", "host", "vcpu_scheduler", "vcpu_scheduler_priority",
                      "cgroup_partition", "memory_kib", "hugepages", "memballoon"):
                if av[name].get(k) != bv[name].get(k):
                    fields[k] = {"before": av[name].get(k), "after": bv[name].get(k)}
            a_pins = {p["vcpu"]: p["cpuset"] for p in av[name].get("vcpu_pins", [])}
            b_pins = {p["vcpu"]: p["cpuset"] for p in bv[name].get("vcpu_pins", [])}
            if a_pins != b_pins:
                fields["vcpu_pins"] = {"before": a_pins, "after": b_pins}
            if fields:
                result["vms"].append({"vm": name, "change": "modified",
                                      "fields": fields})

    # Pacemaker constraint changes
    ac = {c["vm_name"]: c for c in a.get("pacemaker_constraints", [])}
    bc = {c["vm_name"]: c for c in b.get("pacemaker_constraints", [])}
    for name in sorted(set(ac) | set(bc)):
        if name not in ac:
            result["pacemaker"].append({"vm": name, "change": "constraint added"})
        elif name not in bc:
            result["pacemaker"].append({"vm": name, "change": "constraint removed"})
        else:
            ca, cb = ac[name], bc[name]
            if ca.get("node") != cb.get("node") or ca.get("score") != cb.get("score"):
                result["pacemaker"].append({
                    "vm":     name,
                    "change": "constraint modified",
                    "before": f"{ca.get('node')} ({ca.get('score')})",
                    "after":  f"{cb.get('node')} ({cb.get('score')})",
                })

    n = len(result["hosts"]) + len(result["vms"]) + len(result["pacemaker"])
    result["summary"] = "No changes detected." if n == 0 else f"{n} change(s) detected."
    return result


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "0") == "1"
    print(f"SEAPATH RT Scan  —  http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
