"""Command line: python -m pex_checker TRACE.csv --out OUTDIR [options]"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import Counter

from . import __version__
from . import context as ctxmod
from .config import Config
from .coverage import coverage_counts, coverage_table, write_coverage_md
from .engine import Engine
from . import schema as S
from .report import write_report


def _int(s):
    return int(s, 0)


def build_parser():
    p = argparse.ArgumentParser(prog="python -m pex_checker",
                                description="Stream a Teledyne LeCroy pex->CSV export, rebuild every NVMe command and report where "
                                            "and why each problem happened (atlas card, fault side, evidence, repro).")
    p.add_argument("trace", help="CSV exported from the .pex capture (packet level, Data column populated)")
    p.add_argument("--out", required=True, help="output directory")
    g = p.add_argument_group("thresholds (configurable defaults, not spec values unless stated in the README)")
    for name, typ, hlp in [
        ("io-timeout-s", float, "NVMe I/O command timeout (default 30, Linux default)"),
        ("admin-timeout-s", float, "NVMe admin command timeout (default 60, Linux default)"),
        ("pending-min-ms", float, "end of trace: report commands without CQE older than this (default 100)"),
        ("cpl-timeout-ms", float, "PCIe completion timeout (default 50)"),
        ("ack-latency-us", float, "Ack latency threshold (default 10)"),
        ("fc-stall-us", float, "flow-control credit stall threshold (default 30)"),
        ("ltssm-episode-ms", float, "Recovery episode length threshold (default 10)"),
        ("l1-exit-us", float, "L1 exit limit when no capability register was captured (default 64)"),
        ("msix-window-us", float, "CQE -> MSI-X window (default 1000)"),
        ("host-stall-ms", float, "MSI-X -> CQ head doorbell threshold (default 100)"),
        ("cq-stall-ms", float, "CQ non-empty and head not moving (default 1000)"),
        ("outlier-k", float, "latency outlier factor k in max(k*p99, floor) (default 3)"),
        ("outlier-floor-us", float, "latency outlier floor (default 1000)"),
        ("outlier-min-samples", int, "samples per (opcode,size) before outliers are judged (default 50)"),
        ("recovery-storm-n", int, "Recovery entries per window for a storm (default 5)"),
        ("gap-ms", float, "silence in L0 treated as a capture gap (default 100)"),
        ("max-retries", int, "retry budget (default 5)"),
    ]:
        g.add_argument("--" + name, type=typ, help=hlp)
    o = p.add_argument_group("overrides for values normally learned from the trace")
    o.add_argument("--bar0", type=_int, help="BAR0 base (hex) if enumeration is not in the capture")
    o.add_argument("--dstrd", type=int, help="CAP.DSTRD")
    o.add_argument("--mps", type=int, help="Max_Payload_Size in bytes")
    o.add_argument("--mrrs", type=int, help="Max_Read_Request_Size in bytes")
    o.add_argument("--rcb", type=int, help="Read Completion Boundary in bytes (default 64)")
    o.add_argument("--mdts-bytes", type=int, help="MDTS in bytes")
    o.add_argument("--lba-size", type=int, help="LBA size in bytes")
    o.add_argument("--page-size", type=int, help="CC.MPS memory page size in bytes")
    o.add_argument("--sq", action="append", default=[], metavar="QID:BASE:SIZE:CQID",
                   help="declare an I/O SQ created before the capture (repeatable)")
    o.add_argument("--cq", action="append", default=[], metavar="QID:BASE:SIZE:VECTOR",
                   help="declare an I/O CQ created before the capture (repeatable)")
    o.add_argument("--time-unit", choices=["auto", "s", "ms", "us", "ns", "ps"], default="auto")
    o.add_argument("--data-format", choices=["auto", "bytes", "dwords"], default="auto",
                   help="Data column byte order: bytes (wire order) or dwords (8-hex-digit DWs printed MSB first)")
    o.add_argument("--link", help="analyse only rows whose Link column equals this value")
    x = p.add_argument_group("output")
    x.add_argument("--verify-data", action="store_true", help="hash written blocks and compare read-backs (D-15/D-03/D-05/D-20)")
    x.add_argument("--context-rows", type=int, help="rows before/after each finding in the evidence table (default 3)")
    x.add_argument("--no-context", action="store_true", help="skip pass 2 (faster; no evidence rows)")
    x.add_argument("--max-per-card", type=int, help="findings kept per card (default 200; all are counted)")
    x.add_argument("--quiet", action="store_true")
    x.add_argument("--fail-on", choices=["never", "error", "warning"], default="never", help="exit code 1 when findings exist")
    return p


def make_config(a) -> Config:
    c = Config()
    for k in vars(a):
        v = getattr(a, k)
        if v is None:
            continue
        key = k.replace("-", "_")
        if hasattr(c, key) and key not in ("link",):
            setattr(c, key, v)
    c.link = a.link
    return c


def _q(spec, kind):
    parts = spec.split(":")
    if len(parts) != 4:
        raise SystemExit(f"--{kind} expects QID:BASE:SIZE:{'CQID' if kind == 'sq' else 'VECTOR'}, got {spec}")
    return int(parts[0], 0), int(parts[1], 16 if not parts[1].startswith("0x") else 0), int(parts[2], 0), int(parts[3], 0)


def peak_rss_mb():
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return None


def build_summary(e: Engine, path, findings, cov_counts, t_wall, t_pass2):
    sev = Counter(f["severity"] for f in findings)
    by_cat = Counter(f["category"] for f in findings)
    cards = {}
    for f in findings:
        r = cards.setdefault(f["card_id"], {"card": f["card_id"], "name": f["card_name"], "category": f["category"],
                                            "severity": f["severity"], "count": 0, "side": f["fault_side"], "first_pkt": f.get("pkt")})
        if {"error": 0, "warning": 1, "info": 2}[f["severity"]] < {"error": 0, "warning": 1, "info": 2}[r["severity"]]:
            r["severity"] = f["severity"]
    for cid, n in e.card_counts.items():
        if cid in cards:
            cards[cid]["count"] = n
    by_card = sorted(cards.values(), key=lambda r: ({"error": 0, "warning": 1, "info": 2}[r["severity"]], -r["count"]))
    dur = (e.t_last - e.t_first) if (e.t_first is not None and e.t_last is not None) else 0.0
    lat = {k: v.summary() for k, v in sorted(e.lat_all.items(), key=lambda kv: -kv[1].n)}
    stage = {}
    for (adm, name, sz), d in e.stage_hist.items():
        stage[f"{name} {sz}"] = {k: v.summary() for k, v in d.items() if v.n}
    sp = {k: v for k, v in e.counts_nvme.items()}
    rows = e.rows_total
    return {
        "tool": "pex_checker", "version": __version__, "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "file": os.path.abspath(path), "file_bytes": os.path.getsize(path), "rows": rows,
        "trace_start_s": e.t_first, "trace_end_s": e.t_last, "trace_duration_s": dur,
        "wall_s_pass1": round(e.wall_s, 3), "wall_s_pass2": round(t_pass2, 3), "wall_s_total": round(t_wall, 3),
        "rows_per_s": rows / e.wall_s if e.wall_s else 0, "peak_rss_mb": peak_rss_mb(),
        "schema": {"columns": len(e.header), "mapping": e.mapping, "missing_columns": e.missing_cols,
                   "missing_features": e.missing_features, "time_unit": e.time_unit_used, "delimiter": e.reader.delimiter},
        "learned": {
            "BAR0": f"0x{e.bar0:X} ({e.bar0_src})" if e.bar0 is not None else "not found (no CfgWr to BAR0, no doorbell-like writes)",
            "doorbell stride": f"{e.doorbell_stride()} B (DSTRD {e.dstrd}, {e.dstrd_src})",
            "memory page size": f"{e.page_size} B ({e.page_src})",
            "Max_Payload_Size": f"{e.mps} B ({e.mps_src})" if e.mps else "unknown (Device Control not captured; MPS rule off)",
            "Max_Read_Request_Size": f"{e.mrrs} B ({e.mrrs_src})" if e.mrrs else "unknown (MRRS rule off)",
            "MDTS": f"{e.mdts_bytes} B ({e.mdts_src})" if e.mdts_bytes else "unknown (Identify Controller not captured; MDTS rule off)",
            "LBA size": f"{e.lba_size} B ({e.lba_src})",
            "CAP.MQES+1": e.mqes, "CAP.TO (ms)": e.cap_to_ms,
            "admin queues": f"ASQ 0x{e.asq:X} ACQ 0x{e.acq:X} AQA 0x{e.aqa:X}" if e.asq is not None and e.acq is not None and e.aqa is not None else "not captured",
            "I/O queues at end": ", ".join(f"SQ{q.qid}->CQ{q.cqid} {q.size} entries @0x{q.base:X}" for q in e.sqs.values() if q.qid) or "none",
            "MSI-X": f"table size {e.msix_table_size}, {len(e.msix_entries)} vectors programmed in capture" if e.msix_table_size else "capability not captured",
            "Identify Controller": {k: v for k, v in e.ctrl_info.items() if k in ("mn", "sn", "fr", "mdts", "vwc", "sgls", "acl", "nn")} or "not captured",
        },
        "nvme": {"commands_completed": sp.get("completed", 0), "counts": sp, "latency_by_opcode_us": lat, "stage_latency_us": stage,
                 "io_stats": e.io_stats, "outstanding_at_end": len(e.cmds)},
        "pcie": {"tlp_types": {S.TLP_NAMES[i]: n for i, n in enumerate(e.tlp_kind_n) if n}, "completion_latency_us": e.cpl_hist.summary(),
                 "outstanding_requests_at_end": len(e.np)},
        "link": {"final_ltssm": e.ltssm, "final_speed_gts": e.speed or None, "final_width": e.width or None,
                 "max_rate_seen_gts": e.max_rate_seen or None, "max_width_seen": e.max_width_seen or None,
                 "recoveries": e.recovery_count, "l1_episodes": e.l1_episodes, "speed_changes": e.speed_changes[:50],
                 "dll": e.dll_stats, "ack_latency_us": {d: h.summary() for d, h in e.ack_hist.items()},
                 "events_seen": e.events_seen},
        "findings": {"total": len(findings), "total_including_suppressed": sum(e.card_counts.values()),
                     "by_severity": dict(sev), "by_category": dict(by_cat), "by_card": by_card,
                     "suppressed_by_card": dict(e.suppressed)},
        "coverage": cov_counts,
        "config": e.cfg.as_dict(),
        "notes": ["All thresholds are configurable defaults (see config); they are not quoted from a specification unless the "
                  "README says so.", "Packet numbers are the export's Packet column; row = 1-based data row in the CSV."],
    }


def main(argv=None):
    a = build_parser().parse_args(argv)
    cfg = make_config(a)
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    e = Engine(cfg)
    if a.sq or a.cq:
        e.apply_queue_overrides([_q(s, "sq") for s in a.sq], [_q(s, "cq") for s in a.cq])
    e.open_commands_csv(os.path.join(a.out, "commands.csv"))
    try:
        e.run(a.trace, quiet=a.quiet)
    finally:
        e.close_commands_csv()
    findings = sorted(e.findings, key=lambda f: (f["row"], f["id"]))
    t2 = time.time()
    if not a.no_context and findings:
        want = ctxmod.collect_targets(findings, cfg.context_rows, cfg.context_max_rows)
        rows = ctxmod.extract(a.trace, e.ix, e.width, want, quiet=a.quiet, every=cfg.progress_every_s)
        ctxmod.attach(findings, rows, cfg.context_rows)
    t_pass2 = time.time() - t2
    cov = coverage_table()
    seen = dict(e.card_counts)
    cov_counts = write_coverage_md(os.path.join(a.out, "coverage.md"), cov, seen)
    with open(os.path.join(a.out, "findings.jsonl"), "w", encoding="utf-8") as fh:
        for f in findings:
            fh.write(json.dumps(f, default=str) + "\n")
    summary = build_summary(e, a.trace, findings, cov_counts, time.time() - t0, t_pass2)
    with open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    write_report(os.path.join(a.out, "report.html"), summary, findings)
    if not a.quiet:
        s = summary["findings"]["by_severity"]
        sys.stderr.write(f"done: {summary['rows']:,} rows in {summary['wall_s_total']:.1f}s ({summary['rows_per_s']:,.0f} rows/s pass 1), "
                         f"{len(findings)} findings (errors {s.get('error', 0)}, warnings {s.get('warning', 0)}, info {s.get('info', 0)}), "
                         f"{e.csv_rows:,} commands -> {a.out}\n")
    if a.fail_on == "error" and summary["findings"]["by_severity"].get("error"):
        return 1
    if a.fail_on == "warning" and (summary["findings"]["by_severity"].get("error") or summary["findings"]["by_severity"].get("warning")):
        return 1
    return 0
