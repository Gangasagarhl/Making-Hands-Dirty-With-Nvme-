"""Streaming analysis engine.

Pass 1 streams every row once (constant memory): classifies the packet, runs the data-link state machine, the
transaction layer (non-posted request table keyed by (Requester ID, Tag), split completions), config space and NVMe
controller register tracking, and hands memory traffic to the NVMe decoder (nvme.py).  State is bounded by the number
of queues, queue depth, outstanding tags and a few fixed-size windows.

Pass 2 (context.py) re-reads the file and extracts only the rows around each finding.
"""
from __future__ import annotations

import math
import struct
import time
from collections import Counter, deque

from . import schema as S
from .config import Config
from .findings import FindingsMixin
from .link import LinkMixin
from .nvme import NvmeMixin
from .reader import Progress, TraceReader
from .stats import LogHist

NAN = float("nan")


class NP:
    """One outstanding non-posted request."""
    __slots__ = ("kind", "addr", "nbytes", "remaining", "ts", "row", "pkt", "purpose", "obj", "extra", "d", "next_addr",
                 "buf", "ncpl", "length", "reg", "tag", "req", "at")

    def __init__(self, kind, addr, nbytes, ts, row, pkt, d, length, tag, req):
        self.kind = kind
        self.addr = addr
        self.nbytes = nbytes
        self.remaining = nbytes
        self.ts = ts
        self.row = row
        self.pkt = pkt
        self.purpose = None
        self.obj = None
        self.extra = None
        self.d = d
        self.next_addr = addr
        self.buf = None
        self.ncpl = 0
        self.length = length
        self.reg = -1
        self.tag = tag
        self.req = req
        self.at = 0


# PCIe capability register offsets (relative to the PCI Express capability)
DEVCAP, DEVCTL, LINKCAP, LINKCTL, DEVCAP2, DEVCTL2, LINKCAP2, LINKCTL2 = 0x04, 0x08, 0x0C, 0x10, 0x24, 0x28, 0x2C, 0x30
AER_UNCOR = [(0, "Undefined (legacy Training Error)", "L-09"), (4, "Data Link Protocol Error", "L-10"),
             (5, "Surprise Down Error", "L-11"), (12, "Poisoned TLP Received", "L-12"), (13, "Flow Control Protocol Error", "L-13"),
             (14, "Completion Timeout", "L-14"), (15, "Completer Abort", "L-15"), (16, "Unexpected Completion", "L-16"),
             (17, "Receiver Overflow", "L-17"), (18, "Malformed TLP", "L-18"), (19, "ECRC Error", "L-19"),
             (20, "Unsupported Request", "L-20"), (21, "ACS Violation", "L-21"), (22, "Uncorrectable Internal Error", "L-22"),
             (23, "MC Blocked TLP", "L-23"), (24, "AtomicOp Egress Blocked", "L-23"), (25, "TLP Prefix Blocked", "L-23")]
AER_COR = [(0, "Receiver Error", "L-01"), (6, "Bad TLP", "L-02"), (7, "Bad DLLP", "L-03"), (8, "REPLAY_NUM Rollover", "L-04"),
           (12, "Replay Timer Timeout", "L-05"), (13, "Advisory Non-Fatal Error", "L-06"), (14, "Corrected Internal Error", "L-07"),
           (15, "Header Log Overflow", "L-08")]


class Engine(FindingsMixin, LinkMixin, NvmeMixin):
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self._init_findings()
        self.row = 0
        self.f = None
        self.now = 0.0
        self.t_first = None
        self.t_last = None
        self.np: dict = {}
        self.timed_out = {}             # (req, tag) -> (ts, row) of requests reported as timed out
        self.tcache = {}
        self.dcache = {}
        self.dllp_cache = {}
        self.counts = Counter()
        self.tlp_types = Counter()
        self.cpl_hist = LogHist()
        self.mps = self.cfg.mps
        self.mps_src = "cli" if self.cfg.mps else ""
        self.mrrs = self.cfg.mrrs
        self.mrrs_src = "cli" if self.cfg.mrrs else ""
        self.cfg_regs: dict[int, int] = {}
        self.cfg_last_wr: dict[int, int] = {}
        self.cap_ptrs: set[int] = set()
        self.pcie_cap = None
        self.msix_cap = None
        self.pm_cap = None
        self.aer_cap = None
        self.bars = {}                  # bar index -> base
        self.bar_sizes = {}
        self.bar_probe = set()
        self.ext_tag = None
        self.tag10 = None
        self.ro_enabled = None
        self.cto_disabled = None
        self.device_ids = Counter()     # requester IDs of upstream requests (the SSD)
        self.host_ids = Counter()
        self.data_cap = 0               # largest payload shown in the Data column (display truncation guard)
        self.last_uncorr_msg = None
        self.err_msgs = deque()
        self.crs = None                 # [first_ts, count, ev, flagged_l30, flagged_t41]
        self.d3_to_d0 = None
        self.flr = None
        self.back_in_time = 0
        self.gap_flagged = 0
        self.prev_ts = None
        self.recent = deque(maxlen=self.cfg.recent_rows)
        self.missing_features = []
        self.time_scale = 1.0
        self.time_unit_used = "s"
        self._init_link()
        self._init_nvme()

    # ================================================================================================ helpers
    def pkt(self) -> int | None:
        s = self.f[self.ix["pkt"]] if self.f is not None else ""
        if s:
            try:
                return int(s)
            except ValueError:
                v = S.autoint(s)
                if v >= 0:
                    return v
        return None

    def pkt_of_row(self, row):
        return self.pkt() if row == self.row else None

    @staticmethod
    def dname(d):
        return "upstream (device->host)" if d == "U" else "downstream (host->device)"

    def tlp_label(self):
        return self.f[self.ix["tlp_type"]].strip()

    def payload(self, s: str) -> bytes:
        mode = self.cfg.data_format
        if mode == "dwords":
            return S.data_bytes(s, True)[0]
        if mode == "auto":
            sp = s.find(" ")
            if sp == 8 or (sp < 0 and False):
                return S.data_bytes(s, True)[0]
        return S.data_bytes(s, False)[0]

    def outstanding_count(self):
        return len(self.cmds)

    # ================================================================================================ main loop
    def run(self, path: str, quiet=False):
        reader = TraceReader(path)
        rows = reader.rows()
        try:
            first = next(rows)
        except StopIteration:
            first = None
        self.header = reader.header
        self.ix = S.detect_mapping(self.header)
        self.mapping = {k: self.header[v] for k, v in self.ix.items()}
        width = len(self.header)
        self.missing_cols = [c for c in S.CANONICAL if c not in self.ix]
        for c in S.CANONICAL:
            if c not in self.ix:
                self.ix[c] = width
        for feat, cols in S.FEATURE_COLUMNS.items():
            miss = [c for c in cols if c in self.missing_cols]
            if miss:
                self.missing_features.append({"feature": feat, "missing_columns": miss,
                                              "effect": "rules needing these columns are disabled / degraded"})
        self.no_time = "time" in self.missing_cols and "delta" in self.missing_cols
        self.use_delta = "time" in self.missing_cols and "delta" not in self.missing_cols
        self.width = width
        self.reader = reader
        prog = Progress(reader, "pass 1", self.cfg.progress_every_s, quiet=quiet)
        self.t_start_wall = time.time()
        if first is not None:
            self._loop(self._chain(first, rows), prog)
        self.finish()
        prog.done(self.row)
        self.wall_s = time.time() - self.t_start_wall
        return self

    @staticmethod
    def _chain(first, rows):
        yield first
        yield from rows

    def _detect_time_unit(self, s: str):
        cu = self.cfg.time_unit
        if cu != "auto":
            self.time_scale = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12}[cu]
            self.time_unit_used = cu + " (cli)"
            return
        t = s.strip().lower()
        if any(t.endswith(u) for u in ("ns", "us", "µs", "ms", "ps")) or t.endswith("s") and not t[-2:-1].isdigit():
            self.time_scale = 1.0
            self.time_unit_used = "unit suffix in cells"
            return
        if "." in t or "," in t or " " in t:
            self.time_scale = 1.0
            self.time_unit_used = "s (decimal seconds assumed)"
        else:
            self.time_scale = 1e-9
            self.time_unit_used = "ns (integer timestamps assumed; override with --time-unit)"

    def _loop(self, rows, prog):
        ix = self.ix
        i_tlp, i_dllp, i_os, i_ev, i_dir, i_time, i_link = (ix["tlp_type"], ix["dllp_type"], ix["os_type"], ix["link_event"],
                                                             ix["dir"], ix["time"], ix["link"])
        i_delta = ix["delta"]
        width = self.width
        pad = [""] * (width + 1)
        dcache = self.dcache
        link_filter = self.cfg.link
        use_delta = self.use_delta
        no_time = self.no_time
        scale_set = False
        scale = 1.0
        parse_time = S.parse_time
        recent = self.recent
        acc_t = 0.0
        next_periodic = 4096
        n = 0
        tick_every = 65536
        for row, f in rows:
            n += 1
            nf = len(f)
            if nf == width:
                f.append("")
            elif nf < width:
                f.extend(pad[: width + 1 - nf])
            else:
                f[width] = ""
            if link_filter is not None and f[i_link].strip() != link_filter:
                continue
            # ---- time
            if no_time:
                ts = row * 1e-9
            elif use_delta:
                dv = f[i_delta]
                if not scale_set:
                    self._detect_time_unit(dv)
                    scale, scale_set = self.time_scale, True
                try:
                    acc_t += float(dv) * scale
                except ValueError:
                    v = parse_time(dv, scale)
                    if v == v:
                        acc_t += v
                ts = acc_t
            else:
                tv = f[i_time]
                if not scale_set:
                    if not tv:
                        continue
                    self._detect_time_unit(tv)
                    scale, scale_set = self.time_scale, True
                try:
                    ts = float(tv) * scale
                except ValueError:
                    ts = parse_time(tv, scale)
                    if ts != ts:
                        ts = self.now
            self.row = row
            self.f = f
            pt = self.prev_ts
            if pt is not None:
                if ts < pt:
                    self.back_in_time += 1
                    if self.back_in_time == 1:
                        self.emit("X-30", "TIME-BACKWARDS", "info", "link-layer",
                                  f"Timestamp goes backwards ({pt:.9f} -> {ts:.9f} s): export from several captures or links merged, "
                                  f"or a time-base wrap. Timing rules around this row are unreliable.", side="A")
                    ts = pt
                elif ts - pt > self.cfg.gap_ms * 1e-3 and self.ltssm == "L0" and self.gap_flagged < 20:
                    self.gap_flagged += 1
                    self.now = ts
                    self.emit("X-30", "CAPTURE-GAP", "info", "link-layer",
                              f"No packet in either direction for {(ts - pt) * 1e3:.1f} ms while the link was in L0 (no Ack, UpdateFC "
                              f"or SKP either): likely a capture gap, buffer wrap or filter rather than real link silence "
                              f"(threshold {self.cfg.gap_ms} ms, configurable).", side="A",
                              evidence=[self.ev("last packet before the gap", row - 1, None, pt)])
            else:
                self.t_first = ts
            self.prev_ts = ts
            self.now = ts
            # ---- dispatch
            d = f[i_dir]
            dd = dcache.get(d)
            if dd is None:
                dd = S.direction(d)
                dcache[d] = dd
            t = f[i_tlp]
            if t:
                self.on_tlp(f, dd, t)
            else:
                t = f[i_dllp]
                if t:
                    self.on_dllp(f, dd, t)
                else:
                    t = f[i_os]
                    if t:
                        self.on_os(f, dd, t)
                    else:
                        t = f[i_ev]
                        if t:
                            self.on_event(f, dd, t)
            if n >= next_periodic:
                next_periodic = n + 4096
                self.periodic()
                if n % tick_every < 4096:
                    prog.tick(n)
        self.rows_total = n
        self.t_last = self.now

    # ================================================================================================ TLPs
    def on_tlp(self, f, d, ttext):
        ix = self.ix
        psn_s = f[ix["psn"]]
        psn = S.autoint(psn_s) if psn_s else -1
        lc = f[ix["lcrc"]]
        bad = bool(lc) and S.is_bad(lc)
        self.saw_dl_traffic(d)
        if not self.dll_tlp(d, psn, bad):
            return
        self.tlp_accepted(f, d)

    def tlp_accepted(self, f, d):
        ix = self.ix
        ttext = f[ix["tlp_type"]]
        k = self.tcache.get(ttext)
        if k is None:
            k = S.classify_tlp(ttext)
            self.tcache[ttext] = k
        self.tlp_types[S.TLP_NAMES[k]] += 1
        ls = f[ix["length"]]
        length = S.autoint(ls) if ls else 0
        if length < 0:
            length = 0
        if f[ix["ep"]] and S.truthy(f[ix["ep"]]):
            self.on_poisoned(k, d)
        ec = f[ix["ecrc"]]
        if ec and S.is_bad(ec):
            self.emit("V-34", "ECRC", "warning", "link-layer",
                      f"{ttext.strip()} {self.dname(d)} with a bad ECRC (TLP Digest mismatch: '{ec.strip()}'): corrupted inside a "
                      f"component after the link CRC was checked.", related=["L-19"], side="L")
        # flow-control consumption by the transmitter of this TLP
        if k in S.POSTED:
            self.fc_consume(d, "P", length * 4 if k != S.MSG else 0)
        elif k in S.NON_POSTED:
            self.fc_consume(d, "NP", length * 4 if k in S.WITH_DATA else 0)
        elif k in S.COMPLETIONS:
            self.fc_consume(d, "CPL", length * 4 if k == S.CPLD else 0)
        data = f[ix["data"]]
        if k in (S.MWR, S.CPLD, S.MSGD) and length:
            self.check_payload(k, d, length, data, ttext)
        if k == S.MWR or k == S.MRD:
            self.on_mem(f, d, k, length, data)
        elif k in S.COMPLETIONS:
            self.on_cpl(f, d, k, length, data)
        elif k == S.CFGRD or k == S.CFGWR:
            self.on_cfg_req(f, d, k, length, data)
        elif k == S.MSG or k == S.MSGD:
            self.on_msg(f, d, ttext)
        elif k in S.NON_POSTED:
            self.open_np(f, d, k, -1, length * 4, length)

    def check_payload(self, k, d, length, data, ttext):
        nbytes = length * 4
        mps = self.mps
        if mps and nbytes > mps:
            side = "D" if d == "U" else "H"
            self.emit("V-26", "TLP-MPS", "error", "protocol-state",
                      f"{ttext.strip()} {self.dname(d)} carries {nbytes} bytes ({length} DW) but Max_Payload_Size is {mps} bytes "
                      f"({self.mps_src}): the receiver must treat it as a Malformed TLP.",
                      related=["L-18", "L-32"], metrics={"payload_bytes": nbytes, "mps": mps}, side=side)
        if data:
            shown = S.payload_len(data)
            if shown > 0:
                if shown > self.data_cap:
                    self.data_cap = shown
                if shown > nbytes or (shown < nbytes and shown < self.data_cap and shown % 4 == 0 and self.data_cap >= nbytes):
                    self.emit("L-18", "LENGTH-PAYLOAD", "error", "protocol-state",
                              f"{ttext.strip()} {self.dname(d)}: Length field says {length} DW ({nbytes} B) but {shown} payload bytes "
                              f"follow: Malformed TLP.", related=["V-26"], metrics={"length_dw": length, "payload_bytes": shown},
                              side="D" if d == "U" else "H")

    # ------------------------------------------------------------------------------------------------ requests
    def open_np(self, f, d, k, addr, nbytes, length) -> NP:
        ix = self.ix
        req = f[ix["req"]].strip()
        tag = S.autoint(f[ix["tag"]])
        if d == "U":
            self.device_ids[req] += 1
        elif d == "D":
            self.host_ids[req] += 1
        key = (req, tag)
        rec = NP(k, addr, nbytes, self.now, self.row, self.pkt(), d, length, tag, req)
        if tag >= 0:
            prev = self.np.get(key)
            if prev is not None:
                age_ms = (self.now - prev.ts) * 1e3
                pev = self.ev(f"outstanding request with tag {tag}", prev.row, prev.pkt, prev.ts)
                if age_ms > self.cfg.cpl_timeout_ms:
                    self.report_cpl_timeout(prev, reused_at=True)
                else:
                    self.emit("V-32", "TAG-REUSE", "error", "protocol-state",
                              f"{S.TLP_NAMES[k]} {self.dname(d)} reuses tag {tag} of requester {req} while request at row {prev.row} "
                              f"({prev.remaining} of {prev.nbytes} bytes still owed, {age_ms * 1e3:.1f} us old) is outstanding: "
                              f"completions become ambiguous.", evidence=[pev], related=["L-40", "V-31"],
                              metrics={"tag": tag, "requester": req, "age_us": round(age_ms * 1e3, 3)},
                              side="D" if d == "U" else "H")
            self.np[key] = rec
            if d == "U":
                self.check_tag_caps(tag, req)
        at_s = f[ix["at"]]
        if at_s:
            rec.at = S.autoint(at_s)
        return rec

    def check_tag_caps(self, tag, req):
        if tag > 255 and self.tag10 is False and not getattr(self, "_tag10_flag", False):
            self._tag10_flag = True
            self.emit("L-40", "TAG-10BIT", "error", "protocol-state",
                      f"Device requester {req} uses tag {tag} (> 255, needs 10-bit tags) but 10-Bit Tag Requester Enable is 0 in "
                      f"Device Control 2.", related=["V-32"])
        elif 31 < tag <= 255 and self.ext_tag is False and not getattr(self, "_ext_tag_flag", False):
            self._ext_tag_flag = True
            self.emit("L-40", "TAG-EXT", "warning", "protocol-state",
                      f"Device requester {req} uses tag {tag} (> 31) while Extended Tag Field Enable is 0 in Device Control.",
                      related=["V-32"])

    def report_cpl_timeout(self, rec: NP, reused_at=False, at_end=False):
        key = (rec.req, rec.tag)
        age_ms = ((self.t_last if at_end and self.t_last is not None else self.now) - rec.ts) * 1e3
        what = (f"{S.TLP_NAMES[rec.kind]} {self.dname(rec.d)} (requester {rec.req}, tag {rec.tag}, {rec.nbytes} B"
                + (f" at 0x{rec.addr:X}" if rec.addr >= 0 else "") + f", purpose: {self.np_purpose(rec)}) "
                + (f"received {rec.nbytes - rec.remaining} of {rec.nbytes} bytes and then " if rec.remaining != rec.nbytes else "")
                + f"got no completion for {age_ms:.2f} ms (Completion Timeout threshold {self.cfg.cpl_timeout_ms} ms, configurable)"
                + (f"; the tag was reused at row {self.row}" if reused_at else "")
                + (" until the end of the trace" if at_end else "") + ".")
        cmd = rec.obj if rec.purpose in ("sqe", "data", "prpl", "sgl") and hasattr(rec.obj, "sqid") else None
        if rec.purpose == "sqe" and isinstance(rec.obj, tuple):
            cmd = None
        self.emit("T-33", "CPL-TIMEOUT", "error", "timeout", what,
                  at=self.ev("request without completion", rec.row, rec.pkt, rec.ts),
                  related=["L-14", "L-16"] + (["T-01"] if rec.purpose in ("sqe", "data") else []),
                  metrics={"age_ms": round(age_ms, 3), "tag": rec.tag, "requester": rec.req, "bytes": rec.nbytes,
                           "bytes_received": rec.nbytes - rec.remaining},
                  cmd=cmd, side="H" if rec.d == "U" else "D")
        self.timed_out[key] = (rec.ts, rec.row, rec.pkt)
        if len(self.timed_out) > 4096:
            self.timed_out.pop(next(iter(self.timed_out)))
        if self.np.get(key) is rec:
            del self.np[key]
        self.np_abandoned(rec)

    def np_purpose(self, rec):
        p = rec.purpose
        if p is None:
            return "unattributed"
        if p == "sqe":
            return f"SQE fetch SQ{rec.obj[0]} slot {rec.obj[1]}" + (f"+{rec.obj[2] - 1}" if rec.obj[2] > 1 else "")
        if p == "data":
            c = rec.obj
            return f"write data of {c.name} SQ{c.sqid} CID 0x{c.cid:X}"
        if p in ("prpl", "sgl"):
            c = rec.obj
            return f"{'PRP list' if p == 'prpl' else 'SGL segment'} of {c.name} SQ{c.sqid} CID 0x{c.cid:X}"
        if p == "mmio":
            return f"controller register read BAR0+0x{rec.extra:X}"
        if p == "cfg":
            return f"config read register 0x{rec.reg:03X}"
        return p

    # ------------------------------------------------------------------------------------------------ completions
    def on_cpl(self, f, d, k, length, data):
        ix = self.ix
        req = f[ix["req"]].strip()
        tag = S.autoint(f[ix["tag"]])
        st_text = f[ix["status"]]
        st = S.cpl_status(st_text)
        key = (req, tag)
        rec = self.np.get(key)
        bc_s = f[ix["bcount"]]
        bcount = S.autoint(bc_s) if bc_s else -1
        payload = length * 4 if k == S.CPLD else 0
        if rec is None:
            to = self.timed_out.pop(key, None)
            if to is not None:
                self.emit("L-16", "LATE-CPL", "error", "protocol-state",
                          f"{S.TLP_NAMES[k]} {self.dname(d)} for requester {req} tag {tag} arrived "
                          f"{(self.now - to[0]) * 1e3:.2f} ms after its request (row {to[1]}), which had already passed the completion "
                          f"timeout: the requester sees an Unexpected Completion.", evidence=[self.ev("timed-out request", to[1], to[2], to[0])],
                          related=["T-33", "V-31"], side="H" if d == "D" else "D")
            elif tag >= 0 and self.t_first is not None and self.now - self.t_first > self.cfg.cpl_timeout_ms * 1e-3:
                self.emit("V-31", "UNEXPECTED-CPL", "error", "protocol-state",
                          f"{S.TLP_NAMES[k]} {self.dname(d)} for requester {req} tag {tag} (status {st}) matches no outstanding request "
                          f"seen on this link.", related=["L-16"], metrics={"tag": tag, "requester": req},
                          side="H" if d == "D" else "D")
            return
        rec.ncpl += 1
        if st != "SC":
            del self.np[key]
            self.on_bad_cpl_status(rec, st, st_text, d)
            return
        # ---- byte count / lower address (memory reads only)
        if rec.kind == S.MRD and payload:
            la_s = f[ix["lwr_addr"]]
            if bcount >= 0 and bcount != rec.remaining and not (bcount == 0 and rec.remaining == 4096):
                self.emit("V-30", "BYTECOUNT", "error", "protocol-state",
                          f"CplD {self.dname(d)} for tag {tag}: Byte Count = {bcount} but {rec.remaining} bytes of the {rec.nbytes}-byte "
                          f"read were still owed.", evidence=[self.ev("request", rec.row, rec.pkt, rec.ts)],
                          metrics={"byte_count": bcount, "expected": rec.remaining}, side="H" if d == "D" else "D")
            if la_s:
                la = S.hexint(la_s)
                if la >= 0 and rec.next_addr >= 0 and la != (rec.next_addr & 0x7F):
                    self.emit("V-30", "LOWER-ADDR", "error", "protocol-state",
                              f"CplD {self.dname(d)} for tag {tag}: Lower Address = 0x{la:02X} but the next byte owed is at "
                              f"0x{rec.next_addr:X} (low 7 bits 0x{rec.next_addr & 0x7F:02X}).",
                              evidence=[self.ev("request", rec.row, rec.pkt, rec.ts)], side="H" if d == "D" else "D")
            nb_this = payload
            if rec.ncpl == 1 and rec.next_addr >= 0:
                nb_this = payload - (rec.next_addr & 3)
            nb_this = min(nb_this, rec.remaining)
            final = rec.remaining - nb_this <= 0
            if not final and rec.next_addr >= 0:
                end = rec.next_addr + nb_this
                rcb = self.cfg.rcb
                if end % rcb:
                    self.emit("V-29", "RCB", "warning", "protocol-state",
                              f"Partial CplD {self.dname(d)} for tag {tag} ends at 0x{end:X}, not on a {rcb}-byte Read Completion "
                              f"Boundary (RCB {rcb} B, configurable).", evidence=[self.ev("request", rec.row, rec.pkt, rec.ts)],
                              side="H" if d == "D" else "D")
        else:
            nb_this = min(payload, rec.remaining) if payload else rec.remaining
            final = True if rec.kind != S.MRD else rec.remaining - nb_this <= 0
        lat_us = (self.now - rec.ts) * 1e6
        if rec.ncpl == 1:
            self.cpl_hist.add(lat_us)
        if lat_us > self.cfg.cpl_timeout_ms * 1e3:
            self.emit("T-33", "CPL-LATE", "error", "timeout",
                      f"{S.TLP_NAMES[rec.kind]} {self.dname(rec.d)} tag {tag} ({self.np_purpose(rec)}) was answered only after "
                      f"{lat_us / 1e3:.2f} ms (Completion Timeout threshold {self.cfg.cpl_timeout_ms} ms, configurable).",
                      at=self.ev("request", rec.row, rec.pkt, rec.ts), evidence=[self.ev_here("late completion")],
                      related=["L-14"], metrics={"latency_ms": round(lat_us / 1e3, 3)}, side="H" if d == "D" else "D")
        start_addr = rec.next_addr
        rec.remaining -= nb_this
        if rec.next_addr >= 0:
            rec.next_addr += nb_this
        if k == S.CPLD and data:
            self.cpl_data(rec, start_addr, data, nb_this, final)
        if final or rec.remaining <= 0:
            if self.np.get(key) is rec:
                del self.np[key]
            self.np_done(rec)

    def cpl_data(self, rec: NP, start_addr, data, nbytes, final):
        p = rec.purpose
        if p is None:
            return
        b = None
        if p in ("sqe", "prpl", "sgl", "mmio", "cfg", "dsm"):
            b = self.payload(data)
            if rec.ncpl == 1 and rec.addr >= 0:
                b = b[rec.addr & 3:]
            if rec.buf is None:
                rec.buf = bytearray()
            rec.buf += b[:nbytes] if nbytes > 0 else b
        elif p == "data":
            self.data_arrived(rec.obj, start_addr, nbytes, data, rec, read=True)

    def np_done(self, rec: NP):
        p = rec.purpose
        if p is None:
            return
        if p == "sqe":
            self.sqe_fetched(rec)
        elif p == "prpl":
            self.prp_list_fetched(rec)
        elif p == "sgl":
            self.sgl_fetched(rec)
        elif p == "mmio":
            self.mmio_read_done(rec)
        elif p == "cfg":
            self.cfg_read_done(rec)
        elif p == "data":
            self.data_read_done(rec)

    def np_abandoned(self, rec: NP):
        if rec.purpose == "data" and rec.obj is not None:
            rec.obj.np_open -= 1

    def on_bad_cpl_status(self, rec: NP, st, st_text, d):
        what = (f"{st} completion ('{st_text.strip()}') {self.dname(d)} for {S.TLP_NAMES[rec.kind]} tag {rec.tag} from "
                f"{rec.req} ({self.np_purpose(rec)}" + (f", address 0x{rec.addr:X}" if rec.addr >= 0 else "") + ").")
        ev0 = self.ev("request", rec.row, rec.pkt, rec.ts)
        cmd = rec.obj if rec.purpose in ("data", "prpl", "sgl") else None
        if rec.kind in (S.CFGRD, S.CFGWR) and st == "CRS":
            self.on_crs(rec)
            return
        if st == "UR":
            if rec.at == 1 or rec.at == 0b01 and rec.kind == S.MRD and rec.d == "U" and rec.at:
                self.emit("L-34", "ATS-UR", "error", "protocol-state", what + " The request was an ATS Translation Request.",
                          at=ev0, evidence=[self.ev_here("UR completion")], related=["L-20", "L-36"])
            elif rec.d == "U":
                self.emit("L-20", "UR-DMA", "error", "protocol-state",
                          what + " The host rejected the SSD's DMA read: usually an IOMMU fault or an address the host never mapped "
                                 "(L-36). The command should fail with Data Transfer Error.",
                          at=ev0, evidence=[self.ev_here("UR completion")], related=["L-36", "S-04"], cmd=cmd, side="B")
            else:
                self.emit("L-20", "UR", "error", "protocol-state", what, at=ev0, evidence=[self.ev_here("UR completion")],
                          related=["V-03"] if rec.purpose == "mmio" else [], side="H")
        elif st == "CA":
            self.emit("L-15", "CA", "error", "protocol-state", what, at=ev0, evidence=[self.ev_here("CA completion")],
                      cmd=cmd, side="D" if d == "U" else "H")
        elif st == "CRS":
            self.emit("L-30", "CRS-NONCFG", "error", "protocol-state", what + " CRS/RRS is only legal for configuration requests.",
                      at=ev0, evidence=[self.ev_here("CRS completion")])
        else:
            self.emit("L-18", "CPL-STATUS", "error", "protocol-state", what + " Reserved completion status.", at=ev0,
                      evidence=[self.ev_here("completion")])
        if cmd is not None:
            cmd.pcie_error = (st, self.row)
        self.np_abandoned(rec)

    # ------------------------------------------------------------------------------------------------ poison
    def on_poisoned(self, k, d):
        self.poison_pending = (self.row, self.pkt(), self.now, d, S.TLP_NAMES[k])

    # ------------------------------------------------------------------------------------------------ messages
    def on_msg(self, f, d, ttext):
        ix = self.ix
        name = S.msg_name(f[ix["msg_code"]]) or S.msg_name(ttext)
        if name.startswith("ERR_"):
            ts = self.now
            em = self.err_msgs
            em.append(ts)
            while em and ts - em[0] > 1.0:
                em.popleft()
            last = self.findings[-1] if self.findings else None
            rel = []
            ctx = ""
            if last is not None and last["card_id"] != "X-26" and ts - last["timestamp"] < 1e-3:
                ctx = f" It follows finding #{last['id']} ({last['card_id']} {last['card_name']}) by {(ts - last['timestamp']) * 1e6:.1f} us."
                rel = [last["card_id"]]
            sev = "info" if name == "ERR_COR" else "error"
            sender = "SSD" if d == "U" else "host side"
            if name != "ERR_COR":
                self.last_uncorr_msg = (ts, self.ev_here(name))
            if len(em) >= self.cfg.err_msg_storm_n and not getattr(self, "_err_storm", False):
                self._err_storm = True
                self.emit("X-26", "ERR-MSG-STORM", "warning", "link-layer",
                          f"{len(em)} PCIe error messages within 1 s (threshold {self.cfg.err_msg_storm_n}, configurable).",
                          related=rel)
            elif len(em) < self.cfg.err_msg_storm_n // 2:
                self._err_storm = False
            self.emit("X-26", "ERR-MSG", sev if name != "ERR_COR" else "warning", "link-layer",
                      f"{name} message from the {sender}: an error was detected and logged in AER.{ctx}", related=rel,
                      side="D" if d == "U" else "B")
        elif name == "LTR":
            sv = S.autoint(f[ix["snoop_val"]])
            ss = S.autoint(f[ix["snoop_scale"]])
            if sv >= 0 and ss >= 0 and ss <= 5:
                self.ltr_tolerance_ns = sv * (32 ** ss)
                self.ltr_ev = self.ev_here("LTR message")
        elif name == "PME_Turn_Off":
            self.on_pme_turn_off()

    # ------------------------------------------------------------------------------------------------ config space
    def on_cfg_req(self, f, d, k, length, data):
        ix = self.ix
        reg = S.hexint(f[ix["register"]])
        if self.cfg.__dict__.get("cfg_reg_dword"):
            reg *= 4
        rec = self.open_np(f, d, k, -1, 4, length)
        rec.reg = reg
        rec.purpose = "cfg"
        be = S.hexint(f[ix["first_be"]])
        ts = self.now
        if self.d3_to_d0 is not None and ts - self.d3_to_d0[0] < self.cfg.d3_recovery_ms * 1e-3 and not self.d3_to_d0[3]:
            self.d3_to_d0[3] = True
            self.emit("T-42", "D3-RECOVERY", "warning", "timing",
                      f"Config request {(ts - self.d3_to_d0[0]) * 1e3:.3f} ms after the D3hot->D0 write (row {self.d3_to_d0[1]}); "
                      f"at least {self.cfg.d3_recovery_ms} ms of recovery time is expected (configurable).",
                      at=self.d3_to_d0[2], evidence=[self.ev_here("early config request")], side="H")
        if self.flr is not None and not self.flr[3]:
            dt_ms = (ts - self.flr[0]) * 1e3
            if dt_ms < self.cfg.flr_wait_ms:
                self.flr[3] = True
                self.emit("T-43", "FLR-EARLY", "warning", "timing",
                          f"Config request {dt_ms:.3f} ms after Initiate FLR (row {self.flr[1]}); the function needs "
                          f"{self.cfg.flr_wait_ms} ms (configurable) unless it reports Immediate Readiness.",
                          at=self.flr[2], evidence=[self.ev_here("config request during FLR")], side="H", related=["C-28"])
        if k == S.CFGWR and reg >= 0:
            b = self.payload(data)
            if len(b) >= 4:
                v = struct.unpack_from("<I", b, 0)[0]
                if be >= 0 and be != 0xF:
                    old = self.cfg_regs.get(reg, 0)
                    mask = sum(0xFF << (8 * i) for i in range(4) if be >> i & 1)
                    v = (old & ~mask) | (v & mask)
                self.cfg_write(reg, v)

    def cfg_write(self, reg, v):
        old = self.cfg_regs.get(reg)
        self.cfg_last_wr[reg] = v
        if 0x10 <= reg <= 0x24 and reg % 4 == 0:
            bi = (reg - 0x10) // 4
            if v == 0xFFFFFFFF:
                self.bar_probe.add(bi)
                return
            self.bar_probe.discard(bi)
            self.cfg_regs[reg] = v
            self.update_bars()
            return
        self.cfg_regs[reg] = v
        self.cfg_reg_changed(reg, v, old, write=True)

    def update_bars(self):
        r = self.cfg_regs
        i = 0
        while i < 6:
            lo = r.get(0x10 + 4 * i)
            if lo is None:
                i += 1
                continue
            if lo & 1:      # I/O BAR
                i += 1
                continue
            is64 = (lo >> 1) & 3 == 2
            base = lo & ~0xF
            if is64:
                hi = r.get(0x14 + 4 * i, 0)
                base |= hi << 32
            if base:
                self.bars[i] = base
                if i == 0 and self.cfg.bar0 is None:
                    self.set_bar0(base, "config write to BAR0")
            i += 2 if is64 else 1

    def cfg_read_done(self, rec: NP):
        if rec.kind != S.CFGRD or not rec.buf or len(rec.buf) < 4:
            if rec.kind == S.CFGRD:
                self.cfg_read_ok()
            return
        reg = rec.reg
        v = struct.unpack_from("<I", rec.buf, 0)[0]
        self.cfg_read_ok()
        if 0x10 <= reg <= 0x24 and reg % 4 == 0:
            bi = (reg - 0x10) // 4
            if bi in self.bar_probe:
                size = (~(v & ~0xF) & 0xFFFFFFFF) + 1 if v & ~0xF else 0
                if size:
                    self.bar_sizes[bi] = size
                return
            if v and v != 0xFFFFFFFF:
                self.cfg_regs[reg] = v
                self.update_bars()
            return
        old = self.cfg_regs.get(reg)
        self.cfg_regs[reg] = v
        self.cfg_reg_changed(reg, v, old, write=False)

    def cfg_read_ok(self):
        if self.crs is not None:
            c = self.crs
            dur = self.now - c[0]
            if dur > self.cfg.crs_timeout_s and not c[4]:
                self.emit("T-41", "RRS-LONG", "error", "timeout",
                          f"Configuration requests were answered with CRS/RRS for {dur:.3f} s ({c[1]} times) before the device "
                          f"responded (threshold {self.cfg.crs_timeout_s} s, configurable).", at=c[2],
                          evidence=[self.ev_here("first successful config completion")], related=["L-30"])
            self.crs = None
        if self.flr is not None:
            dt_ms = (self.now - self.flr[0]) * 1e3
            self.flr = None

    def on_crs(self, rec: NP):
        ts = self.now
        if self.crs is None:
            self.crs = [ts, 1, self.ev("first config request answered with CRS", rec.row, rec.pkt, rec.ts), False, False]
            return
        c = self.crs
        c[1] += 1
        if c[1] >= self.cfg.crs_loop_n and not c[3]:
            c[3] = True
            self.emit("L-30", "CRS-LOOP", "error", "timeout",
                      f"{c[1]} configuration requests in a row answered with CRS/RRS over {(ts - c[0]) * 1e3:.1f} ms: the device keeps "
                      f"asking for retry (threshold {self.cfg.crs_loop_n}, configurable).", at=c[2], evidence=[self.ev_here("latest CRS")],
                      related=["T-41"])
        if ts - c[0] > self.cfg.crs_timeout_s and not c[4]:
            c[4] = True
            self.emit("T-41", "RRS-1S", "error", "timeout",
                      f"Device still answers configuration requests with CRS/RRS {ts - c[0]:.3f} s after the first one "
                      f"(threshold {self.cfg.crs_timeout_s} s, configurable).", at=c[2], evidence=[self.ev_here("latest CRS")],
                      related=["L-30"])

    def cfg_reg_changed(self, reg, v, old, write):
        # capability list walk
        if reg == 0x34 and not write:
            p = v & 0xFC
            if p:
                self.cap_ptrs.add(p)
            return
        if reg == 0x04 and not write:
            if (v >> 16) & (1 << 4) and not self.cap_ptrs:
                pass
        if not write and reg in self.cap_ptrs:
            cid = v & 0xFF
            nxt = (v >> 8) & 0xFC
            if nxt:
                self.cap_ptrs.add(nxt)
            if cid == 0x10:
                self.pcie_cap = reg
            elif cid == 0x11:
                self.msix_cap = reg
                self.msix_table_size = ((v >> 16) & 0x7FF) + 1
                self.msix_enabled = bool(v >> 31 & 1)
                self.msix_fmask = bool(v >> 30 & 1)
            elif cid == 0x01:
                self.pm_cap = reg
            return
        if not write and reg >= 0x100 and (reg == 0x100 or reg in self.cap_ptrs):
            cid = v & 0xFFFF
            nxt = (v >> 20) & 0xFFC
            if nxt:
                self.cap_ptrs.add(nxt)
            if cid == 0x0001:
                self.aer_cap = reg
            return
        if self.pcie_cap is None and not write and 0x40 <= reg < 0x100 and (v & 0xFF) == 0x10 and ((v >> 16) & 0xF) in (1, 2):
            self.pcie_cap = reg       # PCIe capability header seen without the chain walk
            return
        # MSI-X message control (enable / function mask) and table location
        mc = self.msix_cap
        if mc is not None:
            if reg == mc:
                self.msix_enabled = bool(v >> 31 & 1)
                self.msix_fmask = bool(v >> 30 & 1)
                if not write:
                    self.msix_table_size = ((v >> 16) & 0x7FF) + 1
                return
            if reg == mc + 4 and not write:
                self.msix_table = (v & 7, v & ~7)
                return
            if reg == mc + 8 and not write:
                self.msix_pba = (v & 7, v & ~7)
                return
        pm = self.pm_cap
        if pm is not None and reg == pm + 4 and write:
            new = v & 3
            prev = (old & 3) if old is not None else None
            if new == 0 and prev == 3:
                self.d3_to_d0 = [self.now, self.row, self.ev_here("PMCSR write D3hot -> D0"), False]
            return
        pc = self.pcie_cap
        if pc is None:
            return
        off = reg - pc
        if off == DEVCTL:
            ctl = v & 0xFFFF
            if not self.cfg.mps:
                self.mps = 128 << ((ctl >> 5) & 7)
                self.mps_src = f"Device Control {'write' if write else 'read'} at row {self.row}"
            if not self.cfg.mrrs:
                self.mrrs = 128 << ((ctl >> 12) & 7)
                self.mrrs_src = f"Device Control {'write' if write else 'read'} at row {self.row}"
            self.ext_tag = bool(ctl >> 8 & 1)
            self.ro_enabled = bool(ctl >> 4 & 1)
            if write and ctl >> 15 & 1:
                self.flr = [self.now, self.row, self.ev_here("Initiate Function Level Reset"), False]
                self.on_flr()
            if not write:
                sta = v >> 16
                if sta & 0x7:
                    names = [n for b, n in ((0, "Correctable"), (1, "Non-Fatal"), (2, "Fatal"), (3, "Unsupported Request")) if sta >> b & 1]
                    self.emit("X-26", "DEVSTA", "info", "link-layer",
                              f"Device Status read shows error-detected bits: {', '.join(names)} Error Detected.",
                              related=["L-20"] if sta & 8 else [])
            self.check_mps_caps()
        elif off == DEVCAP:
            self.devcap = v
            self.check_mps_caps()
        elif off == LINKCAP:
            self.linkcap = v
        elif off == LINKCTL:
            self.linkctl = v
            if not write:
                sp = (v >> 16) & 0xF
                w = (v >> 20) & 0x3F
                self.linksta = (S.RATE_OF_SPEED_CODE.get(sp, 0.0), w)
        elif off == DEVCTL2:
            self.cto_disabled = bool(v >> 4 & 1)
            self.cto_value = v & 0xF
            self.tag10 = bool(v >> 12 & 1)
            if self.cto_disabled and not getattr(self, "_cto_flag", False):
                self._cto_flag = True
                self.emit("T-34", "CTO-DISABLED", "warning", "timeout",
                          "Device Control 2 'Completion Timeout Disable' is set: a lost completion will hang the requester instead of "
                          "timing out.", side="H")
        elif off == LINKCAP2:
            self.linkcap2 = v
        # AER registers
        ac = self.aer_cap
        if ac is not None and not write:
            if reg == ac + 0x04 and v:
                self.aer_bits(v, AER_UNCOR, "uncorrectable")
            elif reg == ac + 0x10 and v:
                self.aer_bits(v, AER_COR, "correctable")

    def aer_bits(self, v, table, kind):
        for bit, name, card in table:
            if v >> bit & 1:
                key = ("aer", kind, bit)
                if key in self.__dict__.setdefault("_aer_seen", set()):
                    continue
                self._aer_seen.add(key)
                self.emit(card, "AER-STATUS", "error" if kind == "uncorrectable" else "warning", "link-layer",
                          f"AER {kind} error status read from the SSD has '{name}' (bit {bit}) set (status register value 0x{v:08X}).",
                          metrics={"aer_status": f"0x{v:08X}", "bit": bit})

    def check_mps_caps(self):
        dc = getattr(self, "devcap", None)
        if dc is None or not self.mps or self.mps_src == "cli":
            return
        sup = 128 << (dc & 7)
        if self.mps > sup and not getattr(self, "_l32", False):
            self._l32 = True
            self.emit("L-32", "MPS-CAP", "error", "protocol-state",
                      f"Max_Payload_Size programmed to {self.mps} B but the SSD's Device Capabilities supports only {sup} B.", side="H")
        if sup >= 256 and self.mps == 128 and not getattr(self, "_p12", False):
            self._p12 = True
            self.emit("P-12", "MPS-SMALL", "info", "performance",
                      f"Max_Payload_Size is 128 B although the SSD supports {sup} B: every 4 KiB read needs 32 TLPs instead of "
                      f"{4096 // sup}; check the root port's capability and the OS MPS policy.", side="H")

    def link_cap_l1_exit_us(self):
        lc = getattr(self, "linkcap", None)
        if lc is None:
            return None, ""
        code = (lc >> 15) & 7
        if code == 7:
            return None, "code 7 (more than 64 us)"
        return float(1 << code), f"code {code} (< {1 << code} us)"

    def dev_l1_acceptable_us(self):
        dc = getattr(self, "devcap", None)
        if dc is None:
            return None, ""
        code = (dc >> 9) & 7
        if code == 7:
            return None, "no limit"
        return float(1 << code), f"code {code} ({1 << code} us)"

    # ================================================================================================ periodic
    def periodic(self):
        now = self.now
        lim = self.cfg.cpl_timeout_ms * 1e-3
        if self.np:
            old = [r for r in self.np.values() if now - r.ts > lim]
            for r in old:
                self.report_cpl_timeout(r)
        while self.seq_gap_pending and self.seq_gap_pending[0][0] <= now:
            dl, d, exp, psn, ahead, ev0 = self.seq_gap_pending.popleft()
            self.emit("X-30", "PSN-GAP", "info", "link-layer",
                      f"{self.dname(d)} TLP sequence jumped from expected PSN {exp} to {psn} ({ahead} missing) with no Nak from the "
                      f"receiver: the analyzer did not store those TLPs (capture gap, filter or lost lock), so missing-completion "
                      f"findings nearby may be artefacts.", at=ev0, related=["X-17", "X-28"], side="A")
        if self.relock_pending:
            self.check_relock()
        if self.pm_req is not None:
            self.check_pm_req()
        if self.fc_pending:
            self.fc_pending = [p for p in self.fc_pending if now - p[2] < 1e-3]
        for d in ("U", "D"):
            blk = self.dll_block[d]
            if blk is not None and now - blk[1] > 1e-4:
                self._dll_unblock(d)
        self.nvme_periodic()

    def finish(self):
        self.t_last = self.now
        self.periodic()
        for d in ("U", "D"):
            ep = self.replay_ep[d]
            if ep is not None:
                self.add_interval(ep[0], ep[1], "replay burst", ep[5]["row"], ep[5]["pkt"], f"{ep[2]} replayed TLPs")
        if self.np and self.t_last is not None:
            for r in list(self.np.values()):
                if self.t_last - r.ts > self.cfg.cpl_timeout_ms * 1e-3:
                    self.report_cpl_timeout(r, at_end=True)
        while self.relock_pending:
            self.now += 1.0
            self.check_relock()
        self.now = self.t_last
        self.nvme_finish()
        self.link_finish()

    def link_finish(self):
        # trained below capability (L-25 / P-11)
        cur_rate = self.speed or (getattr(self, "linksta", (0, 0))[0])
        cur_w = self.width or (getattr(self, "linksta", (0, 0))[1])
        cap_rate, cap_w, src = 0.0, 0, []
        lc = getattr(self, "linkcap", None)
        if lc is not None:
            cap_rate = S.RATE_OF_SPEED_CODE.get(lc & 0xF, 0.0)
            cap_w = (lc >> 4) & 0x3F
            src.append("SSD Link Capabilities")
        if self.max_rate_seen > cap_rate:
            cap_rate = self.max_rate_seen
            src.append("rates seen in TS ordered sets / link events")
        if self.max_width_seen > cap_w:
            cap_w = self.max_width_seen
            src.append("lanes seen in TS ordered sets")
        if cur_rate and cap_rate and cur_rate < cap_rate or (cur_w and cap_w and cur_w < cap_w):
            self.emit("L-25", "LINK-BELOW-CAP", "warning", "performance",
                      f"Link ends the capture at {cur_rate or '?'} GT/s x{cur_w or '?'} although {cap_rate or '?'} GT/s x{cap_w or '?'} "
                      f"was seen as capable ({', '.join(src)}). Check the root port's LnkCap too: the trace shows only this link.",
                      related=["P-11", "X-09", "X-13"], metrics={"final_gts": cur_rate, "final_width": cur_w,
                                                                 "capable_gts": cap_rate, "capable_width": cap_w})
        if not self.l0_seen and self.training_t0 is not None and self.t_last is not None:
            dur = (self.t_last - self.training_t0) * 1e3
            self.emit("L-39", "NO-L0", "error", "link-layer",
                      f"Link training started but no TLP/DLLP traffic (L0) was ever seen in {dur:.2f} ms of capture.",
                      at=self.training_ev, related=["T-40", "X-12"])
