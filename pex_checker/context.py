"""Pass 2: re-stream the file and pull only the rows around each finding (evidence packets +/- N rows, and every
lifecycle packet of the command involved).  Rows are stored in a compact form; non-target rows are not even split."""
from __future__ import annotations

from . import schema as S
from .reader import Progress, TraceReader

SHOW = [("pkt", "Packet"), ("dir", "Dir"), ("tlp_type", "TLP"), ("dllp_type", "DLLP"), ("os_type", "OS"),
        ("link_event", "Event"), ("psn", "PSN"), ("acknak", "AckNak"), ("length", "Len"), ("req", "ReqID"), ("tag", "Tag"),
        ("addr", "Address"), ("register", "Reg"), ("status", "CplSt"), ("bcount", "ByteCnt"), ("lwr_addr", "LwrAddr"),
        ("ep", "EP"), ("attr", "Attr"), ("msg_code", "Msg"), ("hdrfc", "HdrFC"), ("datafc", "DataFC"), ("ts_rate", "Rate"),
        ("ts_lane", "Lanes"), ("lcrc", "LCRC"), ("ecrc", "ECRC"), ("data", "Data"), ("time", "Time")]


def collect_targets(findings, n_ctx: int, cap: int):
    """-> (sorted target rows, per-finding primary rows)."""
    want = set()
    for f in findings:
        rows = {f["row"]}
        for e in f["evidence"]:
            if e.get("row"):
                rows.add(e["row"])
        for k, v in (f.get("command_packets") or {}).items():
            if v and v[0]:
                rows.add(v[0])
        for r in rows:
            if r is None:
                continue
            lo = max(1, r - (n_ctx if r == f["row"] else 1))
            hi = r + (n_ctx if r == f["row"] else 1)
            for x in range(lo, hi + 1):
                want.add(x)
        if len(want) > cap:
            break
    return want


def extract(path: str, ix: dict, width: int, want: set, quiet=False, every=2.0):
    """Stream the file again; return {row: compact dict} for wanted rows."""
    out = {}
    if not want:
        return out
    last = max(want)
    reader = TraceReader(path)
    prog = Progress(reader, "pass 2 (evidence rows)", every, quiet=quiet)
    row = 0
    cols = [(k, lbl, ix.get(k, width)) for k, lbl in SHOW]
    delim = None
    for blk in reader.blocks():
        if delim is None:
            delim = reader.delimiter
        n = len(blk)
        if row + n < min(want):
            row += n
            continue
        for rec in blk:
            row += 1
            if row in want:
                f = rec.split(delim) if rec.__class__ is str else rec
                f = f + [""] * (width + 1 - len(f)) if len(f) <= width else f
                d = {"row": row}
                for k, lbl, i in cols:
                    v = f[i] if i < len(f) else ""
                    if v:
                        if k == "data" and len(v) > 96:
                            v = v[:96] + "…"
                        d[lbl] = v.strip()
                out[row] = d
        prog.tick(row)
        if row >= last:
            break
    prog.done(row)
    return out


def attach(findings, rows: dict, n_ctx: int):
    for f in findings:
        for e in f["evidence"]:
            r = rows.get(e.get("row"))
            if r is not None and e.get("pkt") is None:
                p = r.get("Packet")
                try:
                    e["pkt"] = int(p) if p is not None else None
                except ValueError:
                    e["pkt"] = p
        if f.get("pkt") is None:
            r = rows.get(f["row"])
            if r is not None and r.get("Packet"):
                try:
                    f["pkt"] = int(r["Packet"])
                except ValueError:
                    f["pkt"] = r["Packet"]
        ctx = []
        for x in range(f["row"] - n_ctx, f["row"] + n_ctx + 1):
            r = rows.get(x)
            if r is not None:
                ctx.append(dict(r, focus=(x == f["row"])))
        f["context_rows"] = ctx
        ev_rows = []
        seen = set()
        for e in f["evidence"]:
            r = rows.get(e.get("row"))
            if r is not None and e["row"] not in seen:
                seen.add(e["row"])
                ev_rows.append(dict(r, role=e.get("role", "")))
        f["evidence_rows"] = ev_rows
        cp = f.get("command_packets")
        if cp:
            for k, v in cp.items():
                if v and v[0] and v[1] is None:
                    r = rows.get(v[0])
                    if r is not None and r.get("Packet"):
                        v[1] = r["Packet"]
