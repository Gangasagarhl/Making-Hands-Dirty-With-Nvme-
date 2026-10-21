"""NVMe decoder built from raw TLPs (atlas chapter I-B.3): controller registers via MMIO, queue map from AQA/ASQ/ACQ
and Create I/O CQ/SQ SQEs, doorbells, SQE fetches (split completions reassembled by the engine), PRP/SGL data
attribution by address, CQEs, MSI-X, and the per-command lifecycle with stage latencies.

Memory is bounded: queues (by number of queues), outstanding commands and their address regions (by queue depth),
a fixed-size table of recently completed commands, fixed-size histograms."""
from __future__ import annotations

import hashlib
import math
import struct
from collections import OrderedDict, deque

from . import nvme_defs as nd
from . import schema as S
from .stats import LogHist, size_bucket

MSI_LO, MSI_HI = 0xFEE00000, 0xFEF00000
PAGE = 4096


class Region:
    __slots__ = ("start", "end", "kind", "obj", "boff", "live")

    def __init__(self, start, end, kind, obj, boff=0):
        self.start = start
        self.end = end
        self.kind = kind          # 'sq' 'cq' 'data' 'prpl' 'sgl'
        self.obj = obj
        self.boff = boff
        self.live = True


class SQ:
    __slots__ = ("qid", "base", "size", "cqid", "tail", "fetch_head", "last_sqhd", "slots", "created_row", "in_trace",
                 "regions", "fetches", "sqhd_stall", "fetch_since_sqhd", "first_ts", "outstanding", "prio", "pc",
                 "last_fuse1", "doorbells")

    def __init__(self, qid, base, size, cqid, in_trace, row):
        self.qid = qid
        self.base = base
        self.size = size
        self.cqid = cqid
        self.tail = None
        self.fetch_head = None
        self.last_sqhd = None
        self.slots = {}            # slot -> [doorbell_ts, row, pkt, qd_sq, qd_all, link]
        self.created_row = row
        self.in_trace = in_trace
        self.regions = []
        self.fetches = 0
        self.sqhd_stall = 0
        self.fetch_since_sqhd = 0
        self.first_ts = None
        self.outstanding = 0
        self.prio = 0
        self.pc = 1
        self.last_fuse1 = None
        self.doorbells = 0


class CQ:
    __slots__ = ("qid", "base", "size", "head", "tail", "phase", "vector", "ien", "regions", "posted", "in_trace",
                 "await_irq", "head_ts", "stall_flag", "cqes", "created_row", "resync")

    def __init__(self, qid, base, size, vector, ien, in_trace, row):
        self.qid = qid
        self.base = base
        self.size = size
        self.head = 0
        self.tail = 0
        self.phase = 1
        self.vector = vector
        self.ien = ien
        self.regions = []
        self.posted = deque()      # commands whose CQE is posted but not yet released by the host
        self.in_trace = in_trace
        self.await_irq = deque()   # (ts, cmd) CQEs not yet followed by an MSI-X
        self.head_ts = None
        self.stall_flag = False
        self.cqes = 0
        self.created_row = row
        self.resync = 0


class Cmd:
    __slots__ = ("sqid", "cid", "opc", "name", "nsid", "sqe", "slba", "nlb", "nbytes", "dirn", "slot",
                 "db_ts", "db_row", "db_pkt", "fetch_ts", "fetch_row", "fetch_pkt", "sqe_ts", "sqe_row", "sqe_pkt",
                 "fd_ts", "fd_row", "fd_pkt", "ld_ts", "ld_row", "ld_pkt", "bytes_done", "cqe_ts", "cqe_row", "cqe_pkt",
                 "status", "dw0", "msix_ts", "msix_row", "msix_pkt", "cqdb_ts", "cqdb_row", "cqdb_pkt", "qd_sq", "qd_all",
                 "prev", "link", "regions", "np_open", "ranges_known", "prp_needed", "prp_entries", "prp_list_base",
                 "pcie_error", "poisoned", "flags", "capture", "early_cqe", "done", "vbuf", "cq", "sqhd", "timeout_flag",
                 "sgl_total", "irq_flag", "max_read_wait", "outlier_checked")

    def __init__(self, sqid, slot):
        self.sqid = sqid
        self.slot = slot
        self.cid = -1
        self.opc = -1
        self.name = "?"
        self.nsid = 0
        self.sqe = None
        self.slba = None
        self.nlb = None
        self.nbytes = 0
        self.dirn = 0
        self.db_ts = self.fetch_ts = self.sqe_ts = self.fd_ts = self.ld_ts = self.cqe_ts = self.msix_ts = self.cqdb_ts = None
        self.db_row = self.fetch_row = self.sqe_row = self.fd_row = self.ld_row = self.cqe_row = self.msix_row = self.cqdb_row = None
        self.db_pkt = self.fetch_pkt = self.sqe_pkt = self.fd_pkt = self.ld_pkt = self.cqe_pkt = self.msix_pkt = self.cqdb_pkt = None
        self.bytes_done = 0
        self.status = None
        self.dw0 = 0
        self.qd_sq = 0
        self.qd_all = 0
        self.prev = []
        self.link = None
        self.regions = []
        self.np_open = 0
        self.ranges_known = True
        self.prp_needed = 0
        self.prp_entries = None
        self.prp_list_base = None
        self.pcie_error = None
        self.poisoned = None
        self.flags = []
        self.capture = None
        self.early_cqe = None
        self.done = False
        self.vbuf = None
        self.cq = None
        self.sqhd = None
        self.timeout_flag = False
        self.sgl_total = 0
        self.irq_flag = False
        self.max_read_wait = 0.0
        self.outlier_checked = False

    # ---------------------------------------------------------------------------------------------------------
    @property
    def link_at_submit(self):
        if not self.link:
            return None
        return {"ltssm": self.link[0], "speed_gts": self.link[1] or None, "width": self.link[2] or None}

    def stages(self) -> dict:
        def d(a, b):
            return round((b - a) * 1e6, 3) if (a is not None and b is not None) else None
        last_dev = self.ld_ts if self.ld_ts is not None else self.sqe_ts
        return {"doorbell_to_fetch": d(self.db_ts, self.fetch_ts),
                "fetch_to_sqe_cpl (host completer)": d(self.fetch_ts, self.sqe_ts),
                "sqe_to_first_data (device processing)": d(self.sqe_ts, self.fd_ts),
                "data_transfer": d(self.fd_ts, self.ld_ts),
                "last_data_to_cqe (device)": d(last_dev, self.cqe_ts),
                "cqe_to_msix": d(self.cqe_ts, self.msix_ts),
                "msix_to_cq_doorbell (host)": d(self.msix_ts if self.msix_ts is not None else self.cqe_ts, self.cqdb_ts),
                "total_doorbell_to_cqe": d(self.db_ts if self.db_ts is not None else self.fetch_ts, self.cqe_ts)}

    def packets(self) -> dict:
        return {"doorbell": [self.db_row, self.db_pkt], "sqe_fetch": [self.fetch_row, self.fetch_pkt],
                "sqe_completion": [self.sqe_row, self.sqe_pkt], "first_data": [self.fd_row, self.fd_pkt],
                "last_data": [self.ld_row, self.ld_pkt], "cqe": [self.cqe_row, self.cqe_pkt],
                "msix": [self.msix_row, self.msix_pkt], "cq_head_doorbell": [self.cqdb_row, self.cqdb_pkt]}

    def summary(self):
        return {"sqid": self.sqid, "cid": self.cid, "opcode": self.name, "slba": self.slba, "nlb": self.nlb,
                "bytes": self.nbytes, "doorbell_row": self.db_row, "status": self.status}


CMD_CSV_COLS = ["sqid", "cid", "opcode", "opcode_name", "nsid", "slba", "nlb", "bytes", "fua", "status", "sct", "sc",
                "status_name", "dnr", "doorbell_row", "doorbell_pkt", "doorbell_ts", "fetch_row", "fetch_pkt", "fetch_ts",
                "sqe_row", "first_data_row", "first_data_ts", "last_data_ts", "bytes_seen", "cqe_row", "cqe_pkt", "cqe_ts",
                "msix_row", "msix_ts", "cq_db_row", "cq_db_ts", "us_doorbell_to_fetch", "us_fetch_to_sqe",
                "us_sqe_to_first_data", "us_data", "us_last_data_to_cqe", "us_cqe_to_msix", "us_msix_to_cq_db",
                "us_total", "qd_sq", "qd_all", "link_speed", "link_width", "flags"]


def _f(x, nd_=9):
    return "" if x is None else f"{x:.{nd_}f}"


class NvmeMixin:
    def _init_nvme(self):
        cfg = self.cfg
        self.bar0 = cfg.bar0
        self.bar0_src = "cli" if cfg.bar0 is not None else ""
        self.dstrd = cfg.dstrd if cfg.dstrd is not None else 0
        self.dstrd_src = "cli" if cfg.dstrd is not None else "default 0 (CAP not captured)"
        self.page_size = cfg.page_size or 4096
        self.page_src = "cli" if cfg.page_size else "default 4096 (CC.MPS not captured)"
        self.mmio_wr_pages = {}
        self.regs = {}
        self.cap = None
        self.mqes = None
        self.cqr = None
        self.cap_to_ms = None
        self.mpsmin = 4096
        self.cc_en = None
        self.cc_en_ts = None
        self.cc_en_ev = None
        self.cc_dis_ts = None
        self.cc_dis_ev = None
        self.shn_ts = None
        self.shn_ev = None
        self.rdy_flag = False
        self.resets = deque()
        self.aqa = None
        self.asq = None
        self.acq = None
        self.sqs: dict[int, SQ] = {}
        self.cqs: dict[int, CQ] = {}
        self.deleted_sq = {}
        self.deleted_cq = {}
        self.pages: dict[int, list] = {}
        self.cmds: dict[tuple, Cmd] = {}
        self.recent_done = OrderedDict()   # (sqid, cid) -> Cmd
        self.recent_cmds = deque(maxlen=6)
        self.lba_size = cfg.lba_size or 512
        self.lba_src = "cli" if cfg.lba_size else "default 512 (Identify Namespace not captured)"
        self.ns_info = {}
        self.ctrl_info = {}
        self.mdts_bytes = cfg.mdts_bytes
        self.mdts_src = "cli" if cfg.mdts_bytes else ""
        self.granted_q = None
        self.coalesce = None
        self.acre = None
        self.msix_table_size = None
        self.msix_table = None
        self.msix_pba = None
        self.msix_enabled = None
        self.msix_fmask = None
        self.msix_entries = {}      # vector -> [addr, data, masked]
        self.msix_by_key = {}       # (addr, data) -> vector
        self.msix_learn = {}        # data -> cqid (no table captured)
        self.vec_pending = {}       # vector -> count of CQEs not yet announced
        self.msix_wait = deque()    # MSI-X with no CQE yet: (deadline, vector, ev)
        self.nvme_active = False
        self.cmd_hist = {}
        self.stage_hist = {}
        self.lat_all = {}
        self.counts_nvme = {"doorbells": 0, "sqe": 0, "cqe": 0, "msix": 0, "data_tlps": 0, "data_unattributed": 0,
                            "ghost_fetches": 0, "completed": 0, "error_status": 0, "cq_slot_resync": 0}
        self.retry_tab = OrderedDict()
        self.crd_wait = OrderedDict()
        self.nnr = None
        self.aborts = []
        self.poison_pending = None
        self.dsm_ranges = 0
        self.writes_since_flush = 0
        self.verify = {} if cfg.verify_data else None
        self.dealloc = {} if cfg.verify_data else None
        self.zeroed = {} if cfg.verify_data else None
        self.ns_ids = {}
        self.csv_out = None
        self.csv_rows = 0
        self.ghost_slots = 0
        self.hmb_enabled = False
        self.queue_overrides_applied = False

    # =============================================================================================== output
    def open_commands_csv(self, path):
        import csv
        self._csv_fh = open(path, "w", newline="", encoding="utf-8", buffering=1 << 20)
        self.csv_out = csv.writer(self._csv_fh)
        self.csv_out.writerow(CMD_CSV_COLS)

    def close_commands_csv(self):
        if self.csv_out is not None:
            self._csv_fh.close()
            self.csv_out = None

    def write_cmd(self, c: Cmd):
        if self.csv_out is None:
            return
        st = c.stages()
        sct = sc = dnr = ""
        sname = ""
        if c.status is not None:
            sc_, sct_, crd, more, dnr_ = nd.split_status(c.status)
            sct, sc, dnr = sct_, f"0x{sc_:02X}", dnr_
            sname = "Success" if c.status == 0 else nd.status_info(sct_, sc_)[0]
        self.csv_rows += 1
        self.csv_out.writerow([
            c.sqid, f"0x{c.cid:X}" if c.cid >= 0 else "", f"0x{c.opc:02X}" if c.opc >= 0 else "", c.name, c.nsid,
            "" if c.slba is None else c.slba, "" if c.nlb is None else c.nlb, c.nbytes,
            int(nd.fua(c.sqid, c.sqe)) if c.sqe is not None else "",
            "" if c.status is None else f"0x{c.status:04X}", sct, sc, sname, dnr,
            c.db_row or "", c.db_pkt if c.db_pkt is not None else "", _f(c.db_ts), c.fetch_row or "",
            c.fetch_pkt if c.fetch_pkt is not None else "", _f(c.fetch_ts), c.sqe_row or "", c.fd_row or "", _f(c.fd_ts),
            _f(c.ld_ts), c.bytes_done, c.cqe_row or "", c.cqe_pkt if c.cqe_pkt is not None else "", _f(c.cqe_ts),
            c.msix_row or "", _f(c.msix_ts), c.cqdb_row or "", _f(c.cqdb_ts),
            *("" if v is None else v for v in st.values()),
            c.qd_sq, c.qd_all, c.link[1] if c.link else "", c.link[2] if c.link else "", ";".join(c.flags)])

    # =============================================================================================== address index
    def add_region(self, start, end, kind, obj, boff=0) -> Region:
        r = Region(start, end, kind, obj, boff)
        pg = start >> 12
        last = (end - 1) >> 12
        pages = self.pages
        while pg <= last:
            lst = pages.get(pg)
            if lst is None:
                pages[pg] = [r]
            else:
                lst.append(r)
            pg += 1
        return r

    def remove_region(self, r: Region):
        pg = r.start >> 12
        last = (r.end - 1) >> 12
        pages = self.pages
        while pg <= last:
            lst = pages.get(pg)
            if lst is not None:
                try:
                    lst.remove(r)
                except ValueError:
                    pass
                if not lst:
                    del pages[pg]
            pg += 1

    def lookup(self, a: int):
        lst = self.pages.get(a >> 12)
        if not lst:
            return None
        dead = None
        for r in lst:
            if r.start <= a < r.end:
                if r.live:
                    return r
                dead = r
        return dead

    # =============================================================================================== BAR / registers
    def set_bar0(self, base, src):
        if self.bar0 == base:
            return
        self.bar0 = base
        self.bar0_src = src

    def doorbell_stride(self):
        return 4 << self.dstrd

    def on_mem(self, f, d, k, length, data):
        ix = self.ix
        a = S.hexint(f[ix["addr"]])
        if a < 0:
            if k == S.MRD:
                self.open_np(f, d, k, -1, length * 4, length)
            return
        fbe = S.hexint(f[ix["first_be"]]) if f[ix["first_be"]] else -1
        lbe = S.hexint(f[ix["last_be"]]) if f[ix["last_be"]] else -1
        nbytes = S.be_bytes(fbe, lbe, length) if (fbe >= 0 and fbe != 0xF) or (lbe >= 0 and lbe not in (0xF, 0)) else length * 4
        a_eff = a + (S.be_offset(fbe) if fbe > 0 else 0)
        if length and ((a & 0xFFF) + length * 4) > 4096:
            self.emit("V-28", "4K-CROSS", "error", "protocol-state",
                      f"{S.TLP_NAMES[k]} {self.dname(d)} at 0x{a:X} with {length * 4} bytes crosses a 4 KiB address boundary.",
                      metrics={"address": f"0x{a:X}", "bytes": length * 4}, side="D" if d == "U" else "H")
        if d == "D":
            if k == S.MWR:
                self.host_mwr(a, length, data, fbe, lbe)
            else:
                rec = self.open_np(f, d, k, a_eff, nbytes, length)
                b0 = self.bar0
                if b0 is not None and b0 <= a < b0 + 0x1000:
                    rec.purpose = "mmio"
                    rec.extra = a - b0
            return
        # ---------------- upstream (device is the requester)
        if k == S.MRD:
            mrrs = self.mrrs
            if mrrs and length * 4 > mrrs:
                self.emit("V-27", "MRRS", "warning", "protocol-state",
                          f"Device MRd of {length * 4} bytes at 0x{a:X} exceeds Max_Read_Request_Size {mrrs} B ({self.mrrs_src}).",
                          related=["L-32"], metrics={"bytes": length * 4, "mrrs": mrrs})
            rec = self.open_np(f, d, k, a_eff, nbytes, length)
            self.device_read(rec, a_eff, nbytes)
            return
        # upstream MWr
        attr = f[ix["attr"]]
        if MSI_LO <= a < MSI_HI or (a, 0) in self.msix_addr_set():
            self.on_msix(a, data, attr)
            return
        self.device_write(a_eff, nbytes, data, attr, length)

    def msix_addr_set(self):
        return ()

    # ---------------------------------------------------------------------------------------------- host MWr (MMIO)
    def host_mwr(self, a, length, data, fbe, lbe):
        b0 = self.bar0
        if b0 is None:
            if length == 1:
                pg = a & ~0xFFF
                self.mmio_wr_pages[pg] = self.mmio_wr_pages.get(pg, 0) + 1
                if self.mmio_wr_pages[pg] >= 64 and len(self.mmio_wr_pages) < 64:
                    self.set_bar0(pg - 0x1000, f"inferred from {self.mmio_wr_pages[pg]} downstream 4-byte writes to page 0x{pg:X} "
                                               f"(assumed doorbell page BAR0+0x1000)")
                    self.emit("X-30", "BAR0-INFERRED", "info", "protocol-state",
                              f"BAR0 was not programmed inside the capture; it was inferred as 0x{self.bar0:X} from doorbell-like "
                              f"writes. Queue creation must still be in the capture (or given with --sq/--cq) for NVMe decode.", side="A")
            if b0 is None and self.bar0 is None:
                self.check_msix_table_write(a, length, data)
                return
            b0 = self.bar0
        off = a - b0
        if 0 <= off < 0x1000:
            self.mmio_write(off, length, data, fbe)
            return
        if 0x1000 <= off < 0x1000 + 65536 * 2 * self.doorbell_stride():
            self.on_doorbell(off - 0x1000, length, data, fbe, lbe, a)
            return
        self.check_msix_table_write(a, length, data)

    def check_msix_table_write(self, a, length, data):
        t = self.msix_table
        if t is None:
            # MSI-X table often sits at BAR0 + 0x2000/0x3000 region; without the capability read we cannot know
            return
        bir, off = t
        base = self.bars.get(bir)
        if base is None:
            return
        rel = a - (base + off)
        size = (self.msix_table_size or 2048) * 16
        if not (0 <= rel < size):
            return
        b = self.payload(data)
        for i in range(0, len(b) - 3, 4):
            r = rel + i
            v = struct.unpack_from("<I", b, i)[0]
            vec, dw = r // 16, (r % 16) // 4
            e = self.msix_entries.setdefault(vec, [0, 0, 1])
            if dw == 0:
                e[0] = (e[0] & ~0xFFFFFFFF) | v
            elif dw == 1:
                e[0] = (e[0] & 0xFFFFFFFF) | (v << 32)
            elif dw == 2:
                e[1] = v
            else:
                e[2] = v & 1
            self.msix_by_key = {(x[0], x[1]): k for k, x in self.msix_entries.items()}

    def mmio_write(self, off, length, data, fbe):
        b = self.payload(data)
        if len(b) < 4:
            return
        for i in range(0, min(len(b), length * 4) - 3, 4):
            self.reg_write(off + i, struct.unpack_from("<I", b, i)[0])

    def reg_write(self, off, v):
        regs = self.regs
        old = regs.get(off)
        regs[off] = v
        ts = self.now
        if off == 0x14:          # CC
            en = v & 1
            shn = (v >> 14) & 3
            mps = (v >> 7) & 0xF
            if not self.cfg.page_size:
                self.page_size = 4096 << mps
                self.page_src = f"CC.MPS write at row {self.row}"
            prev_en = self.cc_en
            self.cc_en = en
            if en and not prev_en:
                self.cc_en_ts = ts
                self.cc_en_ev = self.ev_here("CC.EN = 1")
                self.rdy_flag = False
                self.setup_admin_queues("CC.EN 0->1")
            elif not en and prev_en:
                self.cc_dis_ts = ts
                self.cc_dis_ev = self.ev_here("CC.EN = 0 (controller reset)")
                self.controller_reset("CC.EN 1->0")
            if shn and (old is None or ((old >> 14) & 3) != shn):
                self.shn_ts = ts
                self.shn_ev = self.ev_here(f"CC.SHN = {shn} (shutdown notification)")
        elif off == 0x24:
            self.aqa = v
        elif off in (0x28, 0x2C):
            lo = regs.get(0x28, 0)
            hi = regs.get(0x2C, 0)
            self.asq = lo | (hi << 32)
        elif off in (0x30, 0x34):
            lo = regs.get(0x30, 0)
            hi = regs.get(0x34, 0)
            self.acq = lo | (hi << 32)
        elif off == 0x20 and v == 0x4E564D65:
            self.emit("C-05", "NSSR", "warning", "protocol-state",
                      "Host wrote NSSR = 'NVMe' (NVM Subsystem Reset): the link will drop and all controller state is lost.",
                      related=["S-91"], side="H")

    def setup_admin_queues(self, why):
        if self.aqa is None or self.asq is None or self.acq is None:
            return
        asqs = (self.aqa & 0xFFF) + 1
        acqs = ((self.aqa >> 16) & 0xFFF) + 1
        self.create_cq(0, self.acq, acqs, 0, 1, True)
        self.create_sq(0, self.asq, asqs, 0, True)

    def controller_reset(self, why):
        ts = self.now
        self.resets.append((ts, self.row))
        while self.resets and ts - self.resets[0][0] > 300:
            self.resets.popleft()
        if len(self.resets) >= 3 and not getattr(self, "_c06", False):
            self._c06 = True
            self.emit("C-06", "RESET-LOOP", "error", "protocol-state",
                      f"{len(self.resets)} controller resets (CC.EN 1->0) within 5 minutes: the host keeps resetting the controller.",
                      evidence=[self.ev("earlier reset", r, None, t) for t, r in list(self.resets)[:-1]], related=["C-17", "T-22"])
        out = [c for c in self.cmds.values() if not c.done]
        for c in out:
            c.flags.append("outstanding at controller reset")
            self.finish_cmd(c, reason="controller reset")
        for q in list(self.sqs.values()):
            self.delete_sq(q.qid, reset=True)
        for q in list(self.cqs.values()):
            self.delete_cq(q.qid, reset=True)
        self.vec_pending.clear()
        self.msix_wait.clear()

    def mmio_read_done(self, rec):
        if not rec.buf or len(rec.buf) < 4:
            return
        off = rec.extra
        b = bytes(rec.buf)
        for i in range(0, len(b) - 3, 4):
            self.reg_read(off + i, struct.unpack_from("<I", b, i)[0], rec)

    def reg_read(self, off, v, rec):
        ts = self.now
        if off == 0x00:
            self.mqes = (v & 0xFFFF) + 1
            self.cqr = bool(v >> 16 & 1)
            self.cap_to_ms = ((v >> 24) & 0xFF) * 500
            self.regs[0x00] = v
        elif off == 0x04:
            if self.cfg.dstrd is None:
                self.dstrd = v & 0xF
                self.dstrd_src = f"CAP.DSTRD read at row {self.row}"
            self.mpsmin = 4096 << ((v >> 16) & 0xF)
            self.regs[0x04] = v
        elif off == 0x1C:      # CSTS
            if v == 0xFFFFFFFF:
                if not getattr(self, "_c16", False):
                    self._c16 = True
                    self.emit("C-16", "CSTS-ALL-ONES", "error", "protocol-state",
                              "CSTS read returned 0xFFFFFFFF: the device is gone (link down, or the function no longer decodes "
                              "its BAR).", at=self.ev("CSTS read", rec.row, rec.pkt, rec.ts), evidence=[self.ev_here("completion")],
                              related=["T-44", "C-17"])
                return
            rdy = v & 1
            cfs = v >> 1 & 1
            shst = (v >> 2) & 3
            nssro = v >> 4 & 1
            pp = v >> 5 & 1
            if cfs and not getattr(self, "_cfs", False):
                self._cfs = True
                self.emit("C-01", "CSTS-CFS", "error", "protocol-state",
                          f"CSTS read shows CFS=1 (Controller Fatal Status, CSTS=0x{v:08X}).", related=["S-90"],
                          at=self.ev("CSTS read", rec.row, rec.pkt, rec.ts), evidence=[self.ev_here("completion")])
            elif not cfs:
                self._cfs = False
            if nssro and not getattr(self, "_nssro", False):
                self._nssro = True
                self.emit("S-91", "CSTS-NSSRO", "info", "status-code", f"CSTS.NSSRO = 1: an NVM Subsystem Reset occurred (CSTS=0x{v:08X}).",
                          related=["C-05"])
            to_ms = self.cfg.rdy_timeout_s * 1e3 if self.cfg.rdy_timeout_s else self.cap_to_ms
            if self.cc_en and self.cc_en_ts is not None and not rdy and to_ms and not self.rdy_flag:
                dt = (ts - self.cc_en_ts) * 1e3
                if dt > to_ms:
                    self.rdy_flag = True
                    self.emit("C-02", "RDY-TIMEOUT", "error", "timeout",
                              f"CSTS.RDY is still 0 {dt:.1f} ms after CC.EN=1 (CAP.TO = {to_ms:.0f} ms).",
                              at=self.cc_en_ev, evidence=[self.ev_here("CSTS read with RDY=0")], related=["T-02", "T-03"])
            if self.cc_en and rdy and self.cc_en_ts is not None:
                if to_ms and (ts - self.cc_en_ts) * 1e3 > to_ms and not self.rdy_flag:
                    self.rdy_flag = True
                    self.emit("T-02", "RDY-LATE", "error", "timeout",
                              f"CSTS.RDY became 1 only {(ts - self.cc_en_ts) * 1e3:.1f} ms after CC.EN=1 (CAP.TO = {to_ms:.0f} ms).",
                              at=self.cc_en_ev, evidence=[self.ev_here("CSTS read with RDY=1")], related=["C-02"])
                self.cc_en_ts = None
            if self.cc_en == 0 and self.cc_dis_ts is not None and rdy and to_ms:
                dt = (ts - self.cc_dis_ts) * 1e3
                if dt > to_ms and not getattr(self, "_c03", False):
                    self._c03 = True
                    self.emit("C-03", "RDY-STUCK", "error", "timeout",
                              f"CSTS.RDY is still 1 {dt:.1f} ms after CC.EN=0 (CAP.TO = {to_ms:.0f} ms): controller reset hangs.",
                              at=self.cc_dis_ev, evidence=[self.ev_here("CSTS read with RDY=1")], related=["T-04"])
            if self.cc_en == 0 and not rdy:
                self.cc_dis_ts = None
            if self.shn_ts is not None:
                if shst == 2:
                    self.shn_ts = None
                else:
                    lim = self.cfg.shutdown_timeout_s
                    rtd3e = self.ctrl_info.get("rtd3e")
                    if rtd3e:
                        lim = rtd3e / 1e6
                    if ts - self.shn_ts > lim and not getattr(self, "_c04", False):
                        self._c04 = True
                        self.emit("C-04", "SHUTDOWN-SLOW", "error", "timeout",
                                  f"CSTS.SHST = {shst} {ts - self.shn_ts:.3f} s after the shutdown notification (limit {lim:g} s"
                                  + (" from RTD3E" if rtd3e else ", configurable") + ").", at=self.shn_ev,
                                  evidence=[self.ev_here("CSTS read")], related=["T-05"])
            if pp:
                if getattr(self, "_pp_ts", None) is None:
                    self._pp_ts = (ts, self.ev_here("CSTS.PP = 1"))
                else:
                    mtfa = self.ctrl_info.get("mtfa")
                    lim = mtfa * 0.1 if mtfa else 10.0
                    if ts - self._pp_ts[0] > lim and not getattr(self, "_c12", False):
                        self._c12 = True
                        self.emit("C-12", "PP-STUCK", "error", "timeout",
                                  f"CSTS.PP (processing paused for firmware activation) has been 1 for {ts - self._pp_ts[0]:.2f} s "
                                  f"(limit {lim:g} s" + (" from MTFA" if mtfa else ", configurable") + ").", at=self._pp_ts[1],
                                  evidence=[self.ev_here("CSTS read")], related=["T-10", "S-81"])
            else:
                self._pp_ts = None
        elif off == 0x68:      # CRTO
            if self.cap_to_ms is not None:
                crwmt = (v & 0xFFFF) * 500
                if crwmt and crwmt < self.cap_to_ms and not getattr(self, "_c13", False):
                    self._c13 = True
                    self.emit("C-13", "CRTO", "warning", "protocol-state",
                              f"CRTO.CRWMT = {crwmt} ms is smaller than CAP.TO = {self.cap_to_ms} ms: inconsistent ready-timeout "
                              f"advertisement.", related=["T-03"])

    # ---------------------------------------------------------------------------------------------- queues
    def create_sq(self, qid, base, size, cqid, in_trace, pc=1, prio=0):
        old = self.sqs.get(qid)
        if old is not None:
            self.delete_sq(qid, reset=True)
        q = SQ(qid, base, size, cqid, in_trace, self.row)
        q.pc = pc
        q.prio = prio
        if pc:
            q.regions.append(self.add_region(base, base + size * 64, "sq", q))
        self.sqs[qid] = q
        self.deleted_sq.pop(qid, None)
        return q

    def create_cq(self, qid, base, size, vector, ien, in_trace, pc=1):
        old = self.cqs.get(qid)
        if old is not None:
            self.delete_cq(qid, reset=True)
        q = CQ(qid, base, size, vector, ien, in_trace, self.row)
        if pc:
            q.regions.append(self.add_region(base, base + size * 16, "cq", q))
        self.cqs[qid] = q
        self.deleted_cq.pop(qid, None)
        return q

    def delete_sq(self, qid, reset=False):
        q = self.sqs.pop(qid, None)
        if q is None:
            return
        for r in q.regions:
            self.remove_region(r)
        self.deleted_sq[qid] = (self.now, self.row)

    def delete_cq(self, qid, reset=False):
        q = self.cqs.pop(qid, None)
        if q is None:
            return
        for r in q.regions:
            self.remove_region(r)
        for c in list(q.posted):
            self.finish_cmd(c, reason="CQ deleted")
        self.deleted_cq[qid] = (self.now, self.row)

    def apply_queue_overrides(self, sq_specs, cq_specs):
        for qid, base, size, vec in cq_specs:
            self.create_cq(qid, base, size, vec, 1, False)
        for qid, base, size, cqid in sq_specs:
            self.create_sq(qid, base, size, cqid, False)

    def qdepth(self, sqid):
        q = self.sqs.get(sqid)
        n_sq = (q.outstanding + len(q.slots)) if q else 0
        return n_sq, len(self.cmds) + sum(len(x.slots) for x in self.sqs.values())

    # ---------------------------------------------------------------------------------------------- doorbells
    def on_doorbell(self, off, length, data, fbe, lbe, a):
        stride = self.doorbell_stride()
        self.counts_nvme["doorbells"] += 1
        b = self.payload(data)
        if length != 1 or (fbe >= 0 and fbe != 0xF) or off % 4:
            self.emit("V-04", "DB-PARTIAL", "error", "protocol-state",
                      f"Doorbell-region write at BAR0+0x{off + 0x1000:X} with Length {length} DW, First BE 0x{max(fbe, 0):X}: doorbells "
                      f"are 32-bit registers; the controller may ignore or mis-latch this write.",
                      metrics={"length_dw": length, "first_be": fbe})
            if length != 1 or len(b) < 4:
                return
        if off % stride:
            self.emit("V-03", "DB-STRIDE", "error", "protocol-state",
                      f"Doorbell write at BAR0+0x{off + 0x1000:X} is not on a {stride}-byte doorbell boundary (CAP.DSTRD={self.dstrd}, "
                      f"{self.dstrd_src}): wrong stride used by the host.", related=["C-09", "S-78"])
            return
        if len(b) < 4:
            return
        v = struct.unpack_from("<I", b, 0)[0]
        idx = off // stride
        qid, is_cq = idx >> 1, idx & 1
        if is_cq:
            self.cq_doorbell(qid, v)
        else:
            self.sq_doorbell(qid, v)

    def _db_no_queue(self, qid, v, kind):
        known = self.sqs if kind == "SQ" else self.cqs
        deleted = (self.deleted_sq if kind == "SQ" else self.deleted_cq).get(qid)
        init_seen = bool(known) or self.cc_en is not None
        if not init_seen and not self.deleted_sq:
            # queue map unknown (capture started after initialisation): no finding, count only
            self.counts_nvme["doorbell_unknown_queue"] = self.counts_nvme.get("doorbell_unknown_queue", 0) + 1
            return
        why = f"deleted at row {deleted[1]}" if deleted else "never created in this capture"
        self.emit("V-03", "DB-NOQ", "error", "protocol-state",
                  f"{kind} {'tail' if kind == 'SQ' else 'head'} doorbell for queue {qid} (value {v}) but that queue is {why}.",
                  related=["C-09", "S-78"], metrics={"qid": qid, "value": v})

    def sq_doorbell(self, qid, v):
        q = self.sqs.get(qid)
        if q is None:
            self._db_no_queue(qid, v, "SQ")
            return
        q.doorbells += 1
        self.nvme_active = True
        N = q.size
        if v >= N:
            self.emit("V-01", "DB-RANGE", "error", "protocol-state",
                      f"SQ{qid} tail doorbell = {v} but SQ{qid} has only {N} entries (valid 0..{N - 1}); the controller ignores the write "
                      f"and the commands will never be fetched.", metrics={"qid": qid, "value": v, "qsize": N}, related=["S-78", "C-09"])
            return
        if q.tail is None:
            q.tail = q.fetch_head = q.last_sqhd = v if q.in_trace is False else 0
            if q.in_trace is False:
                q.tail = v
                return
        old = q.tail
        new = (v - old) % N
        if new == 0:
            return
        unfetched = len(q.slots)
        if new > (N - 1) - (q.outstanding + unfetched) and q.in_trace:
            behind = (old - v) % N
            self.emit("V-05", "SQ-OVERRUN", "error", "protocol-state",
                      f"SQ{qid} tail doorbell {old} -> {v}: implies {new} new entries but only {(N - 1) - (q.outstanding + unfetched)} "
                      f"slots were free ({q.outstanding} commands outstanding, {unfetched} not yet fetched, size {N})"
                      + (f"; read as a backwards move by {behind} it means the host withdrew entries" if behind < N // 2 else "")
                      + ": the host overwrote SQEs the controller had not consumed.",
                      metrics={"qid": qid, "old_tail": old, "new_tail": v, "outstanding": q.outstanding, "unfetched": unfetched})
        ts = self.now
        n_sq, n_all = self.qdepth(qid)
        link = (self.ltssm, self.speed, self.width)
        pk = self.pkt()
        wake = self.wake
        s = old
        for i in range(new):
            q.slots[s] = [ts, self.row, pk, n_sq + i, n_all + i, link, wake]
            s = (s + 1) % N
            if len(q.slots) > N:
                break
        self.wake = None
        q.tail = v

    def cq_doorbell(self, qid, v):
        q = self.cqs.get(qid)
        if q is None:
            self._db_no_queue(qid, v, "CQ")
            return
        N = q.size
        if v >= N:
            self.emit("V-02", "DB-RANGE", "error", "protocol-state",
                      f"CQ{qid} head doorbell = {v} but CQ{qid} has only {N} entries: the controller ignores it and the CQ will fill up.",
                      metrics={"qid": qid, "value": v, "qsize": N}, related=["S-78", "C-09"])
            return
        rel = (v - q.head) % N
        avail = (q.tail - q.head) % N
        if rel > avail and q.in_trace:
            self.emit("V-06", "CQDB-PAST-TAIL", "error", "protocol-state",
                      f"CQ{qid} head doorbell {q.head} -> {v} releases {rel} entries but the controller has posted only {avail} "
                      f"unreleased CQE(s): the host released slots it never consumed.",
                      metrics={"qid": qid, "old_head": q.head, "new_head": v, "posted_unreleased": avail})
        ts = self.now
        for _ in range(min(rel, len(q.posted))):
            c = q.posted.popleft()
            c.cqdb_ts, c.cqdb_row, c.cqdb_pkt = ts, self.row, self.pkt()
            if c.msix_ts is None and q.ien and c.cqe_ts is not None:
                c.flags.append("consumed without MSI-X")
                lat = (ts - c.cqe_ts) * 1e3
                if lat > 1.0 and q.vector is not None and self.msix_tracking_ok():
                    self.emit("T-21", "POLLED", "warning", "timeout",
                              f"CQE of {c.name} SQ{c.sqid} CID 0x{c.cid:X} was consumed by the host {lat:.2f} ms after it was posted, "
                              f"with no MSI-X in between: the completion was found by polling / the timeout path.",
                              cmd=c, at=self.ev("CQE", c.cqe_row, c.cqe_pkt, c.cqe_ts), evidence=[self.ev_here("CQ head doorbell")],
                              related=["V-15", "C-15", "D-30"], metrics={"cqe_to_consume_ms": round(lat, 3)})
            if c.msix_ts is not None and (ts - c.msix_ts) * 1e3 > self.cfg.host_stall_ms:
                self.emit("T-32", "HOST-SLOW", "warning", "timing",
                          f"Host rang the CQ{qid} head doorbell {(ts - c.msix_ts) * 1e3:.1f} ms after the MSI-X for {c.name} CID "
                          f"0x{c.cid:X} (threshold {self.cfg.host_stall_ms} ms, configurable): the host side was stalled, not the device.",
                          cmd=c, at=self.ev("MSI-X", c.msix_row, c.msix_pkt, c.msix_ts), evidence=[self.ev_here("CQ head doorbell")],
                          related=["V-07"], side="H", metrics={"msix_to_doorbell_ms": round((ts - c.msix_ts) * 1e3, 3)})
            self.finish_cmd(c)
        q.head = v
        q.head_ts = ts
        q.stall_flag = False

    # ---------------------------------------------------------------------------------------------- device reads
    def device_read(self, rec, a, nbytes):
        r = self.lookup(a)
        if r is None:
            self.counts_nvme["data_unattributed"] += 1
            return
        kind = r.kind
        if kind == "sq":
            q = r.obj
            slot = (a - q.base) // 64
            cnt = max(1, nbytes // 64)
            rec.purpose = "sqe"
            rec.obj = (q.qid, slot, cnt)
            ts = self.now
            q.fetches += cnt
            if q.first_ts is None:
                q.first_ts = ts
            if q.tail is None:
                q.tail = (slot + cnt) % q.size
                q.fetch_head = q.last_sqhd = slot
            for i in range(cnt):
                s = (slot + i) % q.size
                p = q.slots.get(s)
                if p is not None:
                    p.append((ts, self.row, self.pkt()))
            return
        if kind == "prpl" or kind == "sgl":
            c = r.obj
            rec.purpose = kind
            rec.obj = c
            rec.extra = r
            if kind == "prpl" and (a & ~(self.page_size - 1)) != ((a + nbytes - 1) & ~(self.page_size - 1)):
                self.emit("V-19", "PRPL-CROSS", "error", "protocol-state",
                          f"PRP list read of {nbytes} bytes at 0x{a:X} for {c.name} CID 0x{c.cid:X} runs past the end of the "
                          f"list's memory page without chaining.", cmd=c)
            return
        if kind == "data" or kind == "dsm":
            c = r.obj
            if not r.live:
                self.dma_after_completion(c, a, nbytes, "MRd")
                return
            end = a + nbytes
            if end > r.end and self.lookup(r.end) is None or (end > r.end and self.lookup(r.end).obj is not c):
                card = "D-32" if c.opc == 0x09 and c.sqid != 0 else "D-31"
                self.emit(card, "DMA-OVERREAD", "error", "data-integrity",
                          f"Device read of {nbytes} bytes at 0x{a:X} for {c.name} SQ{c.sqid} CID 0x{c.cid:X} extends "
                          f"{end - r.end} bytes past the command's buffer (buffer ends at 0x{r.end:X})"
                          + (f"; the range list holds only {((c.sqe.cdw10 & 0xFF) + 1)} range(s) = {c.nbytes} bytes" if card == "D-32" else "")
                          + ".", cmd=c)
            rec.purpose = "data"
            rec.obj = c
            rec.extra = r
            c.np_open += 1
            self.counts_nvme["data_tlps"] += 1
            if c.fd_ts is None:
                c.fd_ts, c.fd_row, c.fd_pkt = self.now, self.row, self.pkt()
            if c.cqe_ts is not None:
                self.dma_after_completion(c, a, nbytes, "MRd")
            return
        if kind == "cq":
            self.emit("D-31", "READ-CQ", "warning", "data-integrity",
                      f"Device read from CQ{r.obj.qid} memory at 0x{a:X}: a controller never needs to read its completion queue.")

    def data_arrived(self, c: Cmd, start_addr, nbytes, data, rec, read):
        """Write-data CplD payload for command c (device read of host memory)."""
        if nbytes <= 0:
            return
        c.bytes_done += nbytes
        c.ld_ts, c.ld_row, c.ld_pkt = self.now, self.row, self.pkt()
        w = (self.now - rec.ts) * 1e6
        if w > c.max_read_wait:
            c.max_read_wait = w
        self.capture_payload(c, rec.extra, start_addr, data, nbytes)
        if self.poison_pending is not None and self.poison_pending[0] == self.row:
            self.poisoned_data(c)

    def data_read_done(self, rec):
        c = rec.obj
        c.np_open -= 1

    def capture_payload(self, c: Cmd, region, addr, data, nbytes):
        if c.capture is None and c.vbuf is None:
            return
        if region is None or addr is None or addr < 0:
            return
        b = self.payload(data)
        if not b:
            return
        off = region.boff + (addr - region.start)
        if c.capture is not None:
            cap = c.capture
            if off < len(cap):
                n = min(len(b), len(cap) - off)
                cap[off:off + n] = b[:n]
        if c.vbuf is not None and off < len(c.vbuf):
            n = min(len(b), len(c.vbuf) - off, nbytes)
            c.vbuf[off:off + n] = b[:n]

    # ---------------------------------------------------------------------------------------------- device writes
    def device_write(self, a, nbytes, data, attr, length):
        r = self.lookup(a)
        if r is None:
            self.counts_nvme["data_unattributed"] += 1
            if self.poison_pending is not None and self.poison_pending[0] == self.row:
                self.poisoned_unowned()
            return
        kind = r.kind
        if kind == "cq":
            self.on_cqe(r.obj, a, data, nbytes, attr)
            return
        if kind == "data" or kind == "dsm":
            c = r.obj
            self.counts_nvme["data_tlps"] += 1
            if not r.live:
                self.dma_after_completion(c, a, nbytes, "MWr")
                return
            end = a + nbytes
            if end > r.end:
                nxt = self.lookup(r.end)
                if nxt is None or nxt.obj is not c:
                    self.emit("D-31", "DMA-OVERRUN", "error", "data-integrity",
                              f"Device wrote {nbytes} bytes at 0x{a:X} for {c.name} SQ{c.sqid} CID 0x{c.cid:X}; the write runs "
                              f"{end - r.end} bytes past the end of the command's buffer (0x{r.end:X}).", cmd=c)
            if c.dirn == 1 and c.sqid != 0:
                c.flags.append("device wrote into a host->device buffer")
            c.bytes_done += nbytes
            ts = self.now
            if c.fd_ts is None:
                c.fd_ts, c.fd_row, c.fd_pkt = ts, self.row, self.pkt()
            c.ld_ts, c.ld_row, c.ld_pkt = ts, self.row, self.pkt()
            self.capture_payload(c, r, a, data, nbytes)
            if self.poison_pending is not None and self.poison_pending[0] == self.row:
                self.poisoned_data(c)
            if c.cqe_ts is not None:
                self.dma_after_completion(c, a, nbytes, "MWr")
            return
        if kind in ("sq", "prpl", "sgl"):
            self.emit("D-31", "WRITE-INTO-QUEUE", "error", "data-integrity",
                      f"Device wrote {nbytes} bytes at 0x{a:X}, inside {'SQ' + str(r.obj.qid) if kind == 'sq' else 'a ' + kind + ' of ' + r.obj.name} "
                      f"memory: a controller must never write there (corrupts host structures).",
                      cmd=r.obj if kind != "sq" else None)

    def dma_after_completion(self, c: Cmd, a, nbytes, how):
        if "dma_after_cqe" in c.flags:
            return
        c.flags.append("dma_after_cqe")
        if c.early_cqe is not None:
            fnd = c.early_cqe
            self.early_cqe_confirmed(c, how)
        self.emit("V-16", "DMA-NOCMD", "error", "reordering",
                  f"Device {how} of {nbytes} bytes at 0x{a:X} into the buffer of {c.name} SQ{c.sqid} CID 0x{c.cid:X} "
                  f"{(self.now - c.cqe_ts) * 1e6:.2f} us AFTER that command's CQE: the host may already have reused the buffer.",
                  cmd=c, evidence=[self.ev("CQE of the command", c.cqe_row, c.cqe_pkt, c.cqe_ts)], related=["V-13"])

    def early_cqe_confirmed(self, c: Cmd, how):
        ev0, done, need = c.early_cqe
        c.early_cqe = None
        self.emit("V-13", "CQE-BEFORE-DATA", "error", "reordering",
                  f"CQE for {c.name} SQ{c.sqid} CID 0x{c.cid:X} was posted when only {done} of {need} data bytes had been "
                  f"transferred; the rest ({how}) followed the CQE. The host can consume stale data.",
                  cmd=c, at=ev0, evidence=[self.ev_here(f"data {how} after the CQE")], related=["V-16", "V-33"])

    # ---------------------------------------------------------------------------------------------- poison
    def poisoned_data(self, c: Cmd):
        row, pkt, ts, d, tname = self.poison_pending
        self.poison_pending = None
        c.poisoned = (row, pkt, ts, d)
        self.emit("L-12", "POISON", "error", "protocol-state",
                  f"Poisoned {tname} (EP=1) {self.dname(d)} carrying {'read' if d == 'U' else 'write'} data of {c.name} SQ{c.sqid} "
                  f"CID 0x{c.cid:X}: the payload is known to be bad.", cmd=c, related=["V-37", "L-38"],
                  side="D" if d == "U" else "H")

    def poisoned_unowned(self):
        row, pkt, ts, d, tname = self.poison_pending
        self.poison_pending = None
        self.emit("L-12", "POISON", "error", "protocol-state",
                  f"Poisoned {tname} (EP=1) {self.dname(d)}; the TLP could not be tied to an NVMe command.",
                  related=["V-37"], side="D" if d == "U" else "H")

    # ---------------------------------------------------------------------------------------------- SQEs
    def sqe_fetched(self, rec):
        sqid, slot, cnt = rec.obj
        q = self.sqs.get(sqid)
        buf = rec.buf
        if q is None:
            return
        if buf is None or len(buf) < 64:
            # no payload in the export: command identity unknown; keep lifecycle from doorbell anyway
            for i in range(cnt):
                s = (slot + i) % q.size
                p = q.slots.pop(s, None)
                if p is not None:
                    c = Cmd(sqid, s)
                    self._fill_from_slot(c, p, rec)
                    c.flags.append("SQE payload not in export")
                    key = (sqid, -1 - (self.counts_nvme["sqe"] % 1000000))
                    c.cid = key[1]
                    self.counts_nvme["sqe"] += 1
                    self.cmds[key] = c
                    q.outstanding += 1
            return
        ts = self.now
        for i in range(min(cnt, len(buf) // 64)):
            s = (slot + i) % q.size
            p = q.slots.pop(s, None)
            if p is None and q.doorbells:
                self.counts_nvme["ghost_fetches"] += 1     # prefetch of an entry the host has not submitted
                continue
            self.counts_nvme["sqe"] += 1
            sqe = nd.SQE(bytes(buf[i * 64:(i + 1) * 64]))
            self.new_command(q, s, sqe, p, rec)
        if q.fetch_head is not None:
            q.fetch_head = (slot + cnt) % q.size
            q.fetch_since_sqhd += cnt

    def _fill_from_slot(self, c: Cmd, p, rec):
        if p is not None:
            c.db_ts, c.db_row, c.db_pkt, c.qd_sq, c.qd_all, c.link = p[0], p[1], p[2], p[3], p[4], p[5]
            if p[6] is not None:
                c.flags.append("first command after link wake")
                c.__setattr__("cq", None)
                c._wake = p[6] if hasattr(c, "_wake") else None
            c.prev_wake = None if False else None if True else None
        c.fetch_ts, c.fetch_row, c.fetch_pkt = rec.ts, rec.row, rec.pkt
        c.sqe_ts, c.sqe_row, c.sqe_pkt = self.now, self.row, self.pkt()

    def new_command(self, q: SQ, slot, sqe: nd.SQE, p, rec):
        self.nvme_active = True
        sqid = q.qid
        c = Cmd(sqid, slot)
        if p is not None:
            c.db_ts, c.db_row, c.db_pkt, c.qd_sq, c.qd_all, c.link = p[0], p[1], p[2], p[3], p[4], p[5]
            wake = p[6]
        else:
            wake = None
            n_sq, n_all = self.qdepth(sqid)
            c.qd_sq, c.qd_all, c.link = n_sq, n_all, (self.ltssm, self.speed, self.width)
        c.fetch_ts, c.fetch_row, c.fetch_pkt = rec.ts, rec.row, rec.pkt
        c.sqe_ts, c.sqe_row, c.sqe_pkt = self.now, self.row, self.pkt()
        c.sqe = sqe
        c.cid = sqe.cid
        c.opc = sqe.opc
        c.name = nd.opcode_name(sqid, sqe.opc)
        c.nsid = sqe.nsid
        c.slba, c.nlb = nd.lba_range(sqid, sqe)
        c.dirn = nd.data_direction(sqe.opc) if sqid != 0 else self.admin_dirn(sqe)
        lba = self.ns_info.get(sqe.nsid, {}).get("lba_size", self.lba_size)
        c.nbytes = nd.data_length(sqid, sqe, lba)
        c.prev = [x.summary() for x in self.recent_cmds]
        if wake is not None:
            c.flags.append(f"wake:{wake['exit_us']:.1f}us@{wake['row']}")
            c.wake = wake if False else None
            self._wake_of[(sqid, sqe.cid)] = wake
        key = (sqid, sqe.cid)
        prev = self.cmds.get(key)
        if prev is not None and not prev.done:
            self.emit("V-08", "CID-REUSE", "error", "protocol-state",
                      f"SQ{sqid} CID 0x{sqe.cid:X} fetched again ({c.name}) while the earlier command with the same CID "
                      f"({prev.name}, doorbell row {prev.db_row}) is still outstanding: completions become ambiguous.",
                      cmd=c, evidence=[self.ev("earlier SQE with the same CID", prev.sqe_row, prev.sqe_pkt, prev.sqe_ts)],
                      related=["S-03"])
            prev.flags.append("CID reused while outstanding")
            self.finish_cmd(prev, reason="CID reused")
        self.cmds[key] = c
        self.recent_done.pop(key, None)
        q.outstanding += 1
        self.recent_cmds.append(c)
        self.validate_sqe(q, c, sqe)
        self.setup_data(q, c, sqe)
        self.admin_on_submit(c, sqe)
        self.io_on_submit(c, sqe)

    def admin_dirn(self, s):
        o = s.opc
        if o in (0x06, 0x02, 0x82, 0x1A, 0x86, 0x0A, 0x1E):
            return 2
        if o in (0x11, 0x81, 0x19, 0x0D, 0x15, 0x09, 0x1D, 0x7C):
            return 1
        return 0

    _wake_of: dict = {}

    def validate_sqe(self, q: SQ, c: Cmd, s: nd.SQE):
        sqid = q.qid
        # FUSE (V-22)
        if s.fuse:
            if sqid == 0:
                self.emit("V-22", "FUSE-ADMIN", "error", "protocol-state",
                          f"Admin command {c.name} CID 0x{c.cid:X} has FUSE={s.fuse}: fused operations are defined for I/O commands only.", cmd=c)
            elif s.fuse == 3:
                self.emit("V-22", "FUSE-RSVD", "error", "protocol-state", f"{c.name} CID 0x{c.cid:X} uses the reserved FUSE value 11b.", cmd=c)
            elif s.fuse == 1:
                if s.opc != 0x05:
                    self.emit("V-22", "FUSE-OPC", "error", "protocol-state",
                              f"First fused command is {c.name}; only Compare (05h) followed by Write (01h) is a defined fused pair.", cmd=c)
                q.last_fuse1 = (c.slot, c, c.db_row)
            elif s.fuse == 2:
                f1 = q.last_fuse1
                ok = f1 is not None and (f1[0] + 1) % q.size == c.slot and f1[2] == c.db_row
                if not ok:
                    self.emit("V-22", "FUSE-MISSING", "error", "protocol-state",
                              f"Second fused command {c.name} CID 0x{c.cid:X} at SQ{sqid} slot {c.slot} is not directly preceded by a "
                              f"FUSE=01b command made visible by the same doorbell.", cmd=c, related=["S-09"])
                elif s.opc != 0x01:
                    self.emit("V-22", "FUSE-OPC", "error", "protocol-state",
                              f"Second fused command is {c.name}; the defined pair is Compare then Write.", cmd=c)
                q.last_fuse1 = None
        if q.last_fuse1 is not None and s.fuse != 1 and q.last_fuse1[1] is not c:
            f1 = q.last_fuse1
            if (f1[0] + 1) % q.size == c.slot and s.fuse != 2:
                self.emit("V-22", "FUSE-ALONE", "error", "protocol-state",
                          f"FUSE=01b command {f1[1].name} CID 0x{f1[1].cid:X} is followed by {c.name} with FUSE={s.fuse}, not by its "
                          f"second half.", cmd=f1[1], related=["S-09"])
                q.last_fuse1 = None
        # V-23 admin-looking opcode on I/O queue
        if sqid != 0 and (s.opc in nd.ADMIN_ONLY_LOOKING and s.opc not in nd.NVM_OPC or
                          (s.opc == 0x09 and s.nsid in (0, 0xFFFFFFFF))):
            self.emit("V-23", "ADMIN-ON-IO", "warning", "protocol-state",
                      f"SQ{sqid} (an I/O queue) carries opcode 0x{s.opc:02X} ({nd.ADMIN_OPC.get(s.opc, '?')} on the admin queue; "
                      f"'{nd.NVM_OPC.get(s.opc, 'reserved/unsupported')}' on an I/O queue) with NSID 0x{s.nsid:X}: it looks like an admin "
                      f"command submitted to the wrong queue.", cmd=c)
        # V-21 reserved fields
        rv = nd.reserved_violations(sqid, s)
        if rv:
            self.emit("V-21", "RSVD", "warning", "protocol-state",
                      f"{c.name} CID 0x{c.cid:X} has reserved bits set: {'; '.join(rv)} (checked against a conservative subset; verify "
                      f"for the spec revision in VS).", cmd=c)
        # PSDT / SGL (V-24)
        if s.psdt:
            if sqid == 0:
                self.emit("V-24", "SGL-ADMIN", "error", "protocol-state",
                          f"Admin command {c.name} CID 0x{c.cid:X} uses PSDT={s.psdt} (SGL); admin commands over PCIe use PRPs.", cmd=c)
            elif s.psdt == 3:
                self.emit("V-24", "PSDT-RSVD", "error", "protocol-state", f"{c.name} CID 0x{c.cid:X} uses the reserved PSDT value 11b.", cmd=c)
            elif self.ctrl_info.get("sgls") == 0:
                self.emit("V-24", "SGL-UNSUPPORTED", "error", "protocol-state",
                          f"{c.name} CID 0x{c.cid:X} uses SGLs but Identify Controller SGLS = 0 (no SGL support).", cmd=c)
        # MDTS (V-20)
        if self.mdts_bytes and c.nbytes > self.mdts_bytes and (sqid != 0 or s.opc in (0x02, 0x06)):
            if not (sqid != 0 and s.opc in (0x08, 0x09)):
                self.emit("V-20", "MDTS", "error", "protocol-state",
                          f"{c.name} CID 0x{c.cid:X} transfers {c.nbytes} bytes; MDTS allows {self.mdts_bytes} bytes ({self.mdts_src}).",
                          cmd=c, metrics={"bytes": c.nbytes, "mdts_bytes": self.mdts_bytes})
        # I/O on a namespace whose LBA range is known (S-25 precheck is the device's job; record only)

    # ---------------------------------------------------------------------------------------------- data pointers
    def setup_data(self, q: SQ, c: Cmd, s: nd.SQE):
        n = c.nbytes
        if n <= 0:
            return
        if self.verify is not None and c.sqid != 0 and c.opc in (0x01, 0x02) and n <= 1 << 20:
            c.vbuf = bytearray(n)
        if c.sqid == 0 and c.opc in (0x06, 0x02, 0x0A):
            c.capture = bytearray(min(n, self.cfg.capture_limit))
        elif c.sqid != 0 and c.opc == 0x09:
            c.capture = bytearray(min(n, 4096))
        if s.psdt in (1, 2) and c.sqid != 0:
            self.setup_sgl(c, s)
            return
        P = self.page_size
        p1 = s.prp1
        kind = "dsm" if (c.sqid != 0 and c.opc == 0x09) else "data"
        if p1 & 3:
            self.emit("V-17", "PRP1-ALIGN", "error", "protocol-state",
                      f"{c.name} CID 0x{c.cid:X}: PRP1 = 0x{p1:X} is not dword aligned (bits 1:0 = {p1 & 3}).", cmd=c, related=["S-12"])
        first = P - (p1 % P)
        if p1 == 0:
            c.ranges_known = False
            return
        if n <= first:
            c.regions.append(self.add_region(p1, p1 + n, kind, c, 0))
            self.check_trunc(c, p1)
            return
        c.regions.append(self.add_region(p1, p1 + first, kind, c, 0))
        self.check_trunc(c, p1)
        rem = n - first
        p2 = s.prp2
        if rem <= P:
            if p2 % P:
                self.emit("V-18", "PRP2-OFFSET", "error", "protocol-state",
                          f"{c.name} CID 0x{c.cid:X} moves {n} bytes: after PRP1's page ({first} B) the remaining {rem} B fit in one "
                          f"page, so PRP2 = 0x{p2:X} is a data pointer and must be page aligned ({P} B pages, {self.page_src}).",
                          cmd=c, related=["S-12"], metrics={"prp2": f"0x{p2:X}", "page_size": P})
            if p2:
                c.regions.append(self.add_region(p2, p2 + rem, kind, c, first))
            else:
                c.ranges_known = False
            return
        # PRP2 is a PRP list pointer
        need = -(-rem // P)
        c.prp_needed = need
        c.prp_entries = []
        c.ranges_known = False
        self.add_prp_list_region(c, p2, need)

    def add_prp_list_region(self, c: Cmd, ptr, remaining_entries):
        P = self.page_size
        if ptr & 7:
            self.emit("V-17", "PRPL-PTR-ALIGN", "error", "protocol-state",
                      f"{c.name} CID 0x{c.cid:X}: PRP list pointer 0x{ptr:X} is not 8-byte aligned.", cmd=c)
        fit = (P - (ptr % P)) // 8
        n = remaining_entries if remaining_entries <= fit else fit
        c.prp_list_base = ptr
        c.regions.append(self.add_region(ptr, ptr + n * 8, "prpl", c, len(c.prp_entries) * 8))

    def prp_list_fetched(self, rec):
        c = rec.obj
        if c.done or rec.buf is None:
            return
        r = rec.extra
        b = bytes(rec.buf)
        P = self.page_size
        base_idx = (r.boff + (rec.addr - r.start)) // 8
        ents = c.prp_entries
        for i in range(len(b) // 8):
            v = struct.unpack_from("<Q", b, i * 8)[0]
            idx = base_idx + i
            while len(ents) <= idx:
                ents.append(None)
            ents[idx] = v
        # walk what we have
        if any(e is None for e in ents) or len(ents) == 0:
            return
        need = c.prp_needed
        first = P - (c.sqe.prp1 % P)
        boff = first
        rem = c.nbytes - first
        # entries used so far (chain-aware: if more needed than fit in a list page, last entry of the page is a pointer)
        page_cap = (P - (c.prp_list_base % P)) // 8 if c.prp_list_base is not None else need
        data_entries = []
        i = 0
        ptr_next = None
        for i, e in enumerate(ents):
            if len(data_entries) >= need:
                break
            if i == page_cap - 1 and need - len(data_entries) > 1 and len(ents) == page_cap:
                ptr_next = e
                break
            data_entries.append(e)
        for e in data_entries[len(getattr(c, "_prp_done", [])):] if False else data_entries:
            pass
        done_n = getattr(self, "_dummy", 0)
        # register data regions for entries not registered yet
        reg_count = sum(1 for x in c.regions if x.kind == c.regions[0].kind) - 1
        start_i = max(0, reg_count - (1 if c.sqe.prp1 else 0) + 1) if False else len([x for x in c.regions if x.boff >= first])
        for j in range(start_i, len(data_entries)):
            e = data_entries[j]
            if e % P:
                self.emit("V-17", "PRPL-ENTRY-OFFSET", "error", "protocol-state",
                          f"{c.name} CID 0x{c.cid:X}: PRP list entry {j} = 0x{e:X} has a page offset; list entries must be page "
                          f"aligned ({P} B pages, {self.page_src}).", cmd=c, related=["S-12", "V-19"])
            o = first + j * P
            ln = min(P, c.nbytes - o)
            if ln > 0:
                c.regions.append(self.add_region(e, e + ln, "dsm" if c.opc == 0x09 and c.sqid else "data", c, o))
        if ptr_next is not None:
            c.prp_entries = ents[:page_cap - 1]
            c.prp_needed = need
            # continue the list on the next page
            remaining = need - (page_cap - 1)
            self._prp_chain(c, ptr_next, remaining, page_cap - 1)
            return
        if len(data_entries) >= need:
            c.ranges_known = True

    def _prp_chain(self, c, ptr, remaining, done_entries):
        P = self.page_size
        if ptr % P:
            self.emit("V-19", "PRPL-CHAIN-ALIGN", "error", "protocol-state",
                      f"{c.name} CID 0x{c.cid:X}: chained PRP list pointer 0x{ptr:X} is not page aligned.", cmd=c)
        fit = P // 8
        n = min(remaining, fit)
        c.prp_list_base = ptr
        # extend the entries array so indexes continue
        c.regions.append(self.add_region(ptr, ptr + n * 8, "prpl", c, done_entries * 8))

    def check_trunc(self, c, addr):
        if addr >= 1 << 48:
            for mask in ((1 << 48) - 1, (1 << 32) - 1):
                t = addr & mask
                c.regions.append(self.add_region(t, t + 8, "trunc", c, 0))

    def setup_sgl(self, c: Cmd, s: nd.SQE):
        t = s.sgl_type
        addr = s.prp1
        ln = s.sgl_len
        if t == 0:
            if ln < c.nbytes:
                self.emit("V-24", "SGL-SHORT", "error", "protocol-state",
                          f"{c.name} CID 0x{c.cid:X}: SGL Data Block describes {ln} bytes but the command moves {c.nbytes}.", cmd=c,
                          related=["S-12"])
            c.regions.append(self.add_region(addr, addr + min(ln, c.nbytes) if ln else addr + c.nbytes, "data", c, 0))
        elif t in (2, 3):
            if ln == 0 or ln % 16:
                self.emit("V-24", "SGL-SEG-LEN", "error", "protocol-state",
                          f"{c.name} CID 0x{c.cid:X}: SGL segment descriptor length {ln} is zero or not a multiple of 16.", cmd=c,
                          related=["S-12"])
                c.ranges_known = False
                return
            c.ranges_known = False
            c.sgl_total = 0
            c.regions.append(self.add_region(addr, addr + ln, "sgl", c, 0))
            c.flags.append("sgl")
        elif t in (4, 5):
            self.emit("V-24", "SGL-TYPE-PCIE", "error", "protocol-state",
                      f"{c.name} CID 0x{c.cid:X}: SGL descriptor type {t} ({'Keyed Data Block' if t == 4 else 'Transport'}) is not "
                      f"used by NVMe over PCIe.", cmd=c, related=["S-12"])
        elif t == 1:
            pass
        else:
            self.emit("V-24", "SGL-TYPE-RSVD", "error", "protocol-state",
                      f"{c.name} CID 0x{c.cid:X}: reserved SGL descriptor type {t}.", cmd=c, related=["S-12"])

    def sgl_fetched(self, rec):
        c = rec.obj
        if c.done or not rec.buf:
            return
        b = bytes(rec.buf)
        r = rec.extra
        nd_ = len(b) // 16
        last_seg = True
        for i in range(nd_):
            addr, ln = struct.unpack_from("<QI", b, i * 16)
            t = b[i * 16 + 15] >> 4
            is_last = i == nd_ - 1
            if t == 0:
                if ln:
                    c.regions.append(self.add_region(addr, addr + ln, "data", c, c.sgl_total))
                c.sgl_total += ln
            elif t == 1:
                c.sgl_total += ln
            elif t in (2, 3):
                if not is_last:
                    self.emit("V-24", "SGL-SEG-POS", "error", "protocol-state",
                              f"{c.name} CID 0x{c.cid:X}: SGL segment descriptor at position {i} is not the last descriptor of its segment.",
                              cmd=c, related=["S-12"])
                else:
                    c.regions.append(self.add_region(addr, addr + ln, "sgl", c, 0))
                    last_seg = False
            else:
                self.emit("V-24", "SGL-TYPE", "error", "protocol-state",
                          f"{c.name} CID 0x{c.cid:X}: SGL descriptor {i} has type {t}, not valid here.", cmd=c, related=["S-12"])
        if last_seg:
            c.ranges_known = True
            if c.sgl_total < c.nbytes:
                self.emit("V-24", "SGL-SHORT", "error", "protocol-state",
                          f"{c.name} CID 0x{c.cid:X}: SGL describes {c.sgl_total} bytes but the command moves {c.nbytes}.", cmd=c,
                          related=["S-12"])

    # ---------------------------------------------------------------------------------------------- admin specifics
    def admin_on_submit(self, c: Cmd, s: nd.SQE):
        if c.sqid != 0:
            return
        o = s.opc
        if o in (0x01, 0x05):
            self.validate_create(c, s)
        elif o == 0x08:
            tsq = s.cdw10 & 0xFFFF
            tcid = s.cdw10 >> 16
            tgt = self.cmds.get((tsq, tcid))
            if tgt is not None and not tgt.done:
                age = self.now - (tgt.db_ts or tgt.fetch_ts or self.now)
                self.emit("C-14", "ABORT", "warning", "timeout",
                          f"Host submitted Abort for {tgt.name} SQ{tsq} CID 0x{tcid:X}, outstanding for {age * 1e3:.1f} ms: the host's "
                          f"I/O timer expired (or a tool aborted it).", cmd=tgt, related=["T-22", "T-09"],
                          evidence=[self.ev_here("Abort SQE")], metrics={"outstanding_ms": round(age * 1e3, 3)})
            self.aborts.append([c, tsq, tcid, None])

    def validate_create(self, c: Cmd, s: nd.SQE):
        is_sq = s.opc == 0x01
        qid = s.cdw10 & 0xFFFF
        qsize = (s.cdw10 >> 16) + 1
        pc = s.cdw11 & 1
        probs = []
        if qid == 0:
            probs.append("QID 0 is the admin queue")
        if self.granted_q is not None:
            lim = self.granted_q[0] if is_sq else self.granted_q[1]
            if qid > lim:
                probs.append(f"QID {qid} exceeds the {lim} queues granted by Set Features Number of Queues")
        if (is_sq and qid in self.sqs) or (not is_sq and qid in self.cqs):
            probs.append(f"QID {qid} already exists")
        if qsize < 2:
            probs.append(f"queue size {qsize} (QSIZE {qsize - 1}) is below the minimum of 2 entries")
        if self.mqes is not None and qsize > self.mqes:
            probs.append(f"queue size {qsize} exceeds CAP.MQES+1 = {self.mqes}")
        if pc and s.prp1 % self.page_size:
            probs.append(f"PRP1 0x{s.prp1:X} not page aligned")
        if not pc and self.cqr:
            probs.append("PC=0 (non-contiguous) but CAP.CQR=1 requires contiguous queues")
        if is_sq:
            cqid = s.cdw11 >> 16
            if cqid not in self.cqs:
                probs.append(f"CQID {cqid} does not exist")
            if cqid == 0:
                probs.append("I/O SQ mapped to the admin CQ")
        else:
            iv = s.cdw11 >> 16
            if self.msix_table_size is not None and (s.cdw11 >> 1 & 1) and iv >= self.msix_table_size:
                probs.append(f"interrupt vector {iv} beyond the MSI-X table size {self.msix_table_size}")
        if probs:
            self.emit("V-25", "CREATE-PARAMS", "error", "protocol-state",
                      f"{c.name} for QID {qid}: " + "; ".join(probs) + ".", cmd=c, related=["S-30", "S-31", "S-32", "S-37"])

    def io_on_submit(self, c: Cmd, s: nd.SQE):
        if c.sqid == 0:
            return
        o = s.opc
        if o == 0x01:
            self.writes_since_flush += c.nbytes
        elif o == 0x09 and (s.cdw11 >> 2) & 1:
            self.dsm_ranges += (s.cdw10 & 0xFF) + 1
            c.flags.append("deallocate")
        elif o == 0x08:
            c.flags.append("write zeroes" + (" (deallocate)" if s.cdw12 >> 25 & 1 else ""))
        key = (s.opc, s.nsid, s.cdw10, s.cdw11, s.cdw12)
        w = self.crd_wait.pop(key, None)
        if w is not None:
            gap = self.now - w[0]
            crdt = self.ctrl_info.get("crdt")
            need = None
            if crdt and 1 <= w[1] <= 3:
                need = crdt[w[1] - 1] * 0.1
            if need and self.acre and gap < need:
                self.emit("T-17", "CRD-IGNORED", "warning", "timing",
                          f"{c.name} resubmitted {gap * 1e3:.1f} ms after a failure with CRD={w[1]}; the controller asked for at least "
                          f"{need * 1e3:.0f} ms (CRDT{w[1]} from Identify Controller, ACRE enabled).", cmd=c,
                          evidence=[w[2]], side="H", metrics={"gap_ms": round(gap * 1e3, 3), "required_ms": need * 1e3})

    # ---------------------------------------------------------------------------------------------- CQEs
    def on_cqe(self, q: CQ, a, data, nbytes, attr):
        b = self.payload(data)
        if len(b) < 16:
            self.counts_nvme["cqe_no_payload"] = self.counts_nvme.get("cqe_no_payload", 0) + 1
            return
        if attr and self._ro_bit(attr):
            self.emit("V-33", "RO-CQE", "warning", "reordering",
                      f"CQE write to CQ{q.qid} at 0x{a:X} has the Relaxed Ordering attribute set ('{attr.strip()}'): it may pass the "
                      f"command's earlier data writes.", related=["V-13"], side="D")
        n = len(b) // 16
        slot0 = (a - q.base) // 16
        for i in range(n):
            self.one_cqe(q, (slot0 + i) % q.size, b, i * 16)

    @staticmethod
    def _ro_bit(attr: str) -> bool:
        t = attr.strip().upper()
        if "RO" in t and "NO RO" not in t:
            return True
        v = S.hexint(t)
        return v >= 0 and bool(v & 2)

    def one_cqe(self, q: CQ, slot, b, off):
        self.counts_nvme["cqe"] += 1
        q.cqes += 1
        dw0, dw1, sqhd, sqid, cid, phase, status = nd.decode_cqe(b, off)
        ts = self.now
        N = q.size
        here = self.ev_here(f"CQE SQ{sqid} CID 0x{cid:X}")
        if slot != q.tail:
            if (q.tail + 1) % N == q.head and slot == (q.head - 1) % N:
                pass
            q.resync += 1
            self.counts_nvme["cq_slot_resync"] += 1
            if q.in_trace and q.cqes > 1:
                self.emit("V-07", "CQ-SLOT", "warning", "protocol-state",
                          f"CQE written to CQ{q.qid} slot {slot} while the controller's next slot should be {q.tail} (head {q.head}).",
                          related=["V-11", "D-29"], metrics={"slot": slot, "expected": q.tail})
            if slot < q.tail and q.tail - slot > N // 2:
                q.phase ^= 1
            q.tail = slot
        if (q.tail + 1) % N == q.head and q.in_trace:
            self.emit("V-07", "CQ-OVERFLOW", "error", "protocol-state",
                      f"Controller posted a CQE into CQ{q.qid} slot {slot} while the CQ was full (head {q.head}, size {N}): the entry "
                      f"overwrites one the host has not consumed.", related=["V-11"], metrics={"head": q.head, "slot": slot})
        if phase != q.phase and q.in_trace:
            self.emit("V-11", "PHASE", "error", "protocol-state",
                      f"CQE in CQ{q.qid} slot {slot} has phase {phase} but phase {q.phase} was expected "
                      f"({'after a wrap the tag must be inverted' if slot == 0 else 'same pass as the previous entries'}): the host "
                      f"will not see this completion.", metrics={"slot": slot, "phase": phase, "expected": q.phase})
        q.tail = (slot + 1) % N
        if q.tail == 0:
            q.phase ^= 1
        # ---- match the command
        key = (sqid, cid)
        c = self.cmds.get(key)
        if c is None or c.done:
            alt = None
            for sq in self.sqs.values():
                if sq.cqid == q.qid and sq.qid != sqid:
                    x = self.cmds.get((sq.qid, cid))
                    if x is not None and not x.done:
                        alt = x
                        break
            if alt is not None:
                self.emit("V-10", "CQE-SQID", "error", "protocol-state",
                          f"CQE in CQ{q.qid} names SQID {sqid} for CID 0x{cid:X}, but that CID is outstanding on SQ{alt.sqid}: wrong SQID.",
                          cmd=alt, related=["D-28"])
                c = alt
            else:
                dup = self.recent_done.get(key)
                if dup is not None and dup.cqe_ts is not None:
                    self.emit("D-29", "CQE-DUP", "error", "protocol-state",
                              f"Second CQE for SQ{sqid} CID 0x{cid:X} ({dup.name}) with no new SQE fetch of that CID in between: "
                              f"duplicate completion.", cmd=dup, evidence=[self.ev("first CQE", dup.cqe_row, dup.cqe_pkt, dup.cqe_ts)],
                              related=["V-09", "C-19"])
                else:
                    sq = self.sqs.get(sqid)
                    synced = sq is not None and sq.in_trace and (sq.first_ts is not None)
                    if sq is None and self.sqs:
                        self.emit("V-10", "CQE-SQID-UNKNOWN", "error", "protocol-state",
                                  f"CQE in CQ{q.qid} names SQID {sqid}, which does not exist (CID 0x{cid:X}).", related=["V-09"])
                    elif synced:
                        self.emit("V-09", "CQE-UNKNOWN", "error", "protocol-state",
                                  f"CQE in CQ{q.qid} for SQ{sqid} CID 0x{cid:X} matches no command fetched from SQ{sqid}.",
                                  related=["D-28", "C-19"], metrics={"status": f"0x{status:04X}"})
                self.vec_cqe(q, None)
                return
        sq = self.sqs.get(c.sqid)
        if sq is not None and sq.cqid != q.qid and sq.in_trace:
            self.emit("V-10", "CQE-WRONG-CQ", "error", "protocol-state",
                      f"CQE for SQ{c.sqid} CID 0x{cid:X} was posted to CQ{q.qid}, but SQ{c.sqid} is bound to CQ{sq.cqid}.", cmd=c)
        # SQHD
        if sq is not None and sq.fetch_head is not None and sq.in_trace:
            Ns = sq.size
            if sq.last_sqhd is None:
                sq.last_sqhd = sqhd
            d1 = (sqhd - sq.last_sqhd) % Ns
            d2 = (sq.fetch_head - sq.last_sqhd) % Ns
            if d1 > d2:
                self.emit("V-10", "SQHD-RANGE", "error", "protocol-state",
                          f"CQE for SQ{c.sqid} CID 0x{cid:X} reports SQHD={sqhd}, beyond the entries the controller has fetched "
                          f"(fetch head {sq.fetch_head}, previous SQHD {sq.last_sqhd}).", cmd=c,
                          metrics={"sqhd": sqhd, "fetch_head": sq.fetch_head})
            if sqhd == sq.last_sqhd and sq.fetch_since_sqhd > Ns:
                if not sq.sqhd_stall:
                    sq.sqhd_stall = 1
                    self.emit("V-10", "SQHD-STALL", "error", "protocol-state",
                              f"SQHD of SQ{c.sqid} stays at {sqhd} although {sq.fetch_since_sqhd} entries were fetched since it last "
                              f"moved: a host that uses SQHD for flow control will think the SQ is full.", cmd=c)
            elif sqhd != sq.last_sqhd:
                sq.fetch_since_sqhd = 0
                sq.sqhd_stall = 0
            sq.last_sqhd = sqhd
        self.complete(c, q, dw0, status, here, sqhd)

    def complete(self, c: Cmd, q: CQ, dw0, status, here, sqhd):
        ts = self.now
        c.cqe_ts, c.cqe_row, c.cqe_pkt = ts, self.row, self.pkt()
        c.status = status
        c.dw0 = dw0
        c.cq = q.qid
        c.sqhd = sqhd
        self.counts_nvme["completed"] += 1
        sq = self.sqs.get(c.sqid)
        if sq is not None and sq.outstanding > 0:
            sq.outstanding -= 1
        q.posted.append(c)
        if len(q.posted) > q.size:
            self.finish_cmd(q.posted.popleft())
        self.vec_cqe(q, c)
        sc, sct, crd, more, dnr = nd.split_status(status)
        # ---- data completeness / ordering
        need = c.nbytes if c.dirn in (1, 2) else 0
        if need and status == 0 and c.ranges_known and not (c.sqid == 0 and c.opc == 0x0A):
            if c.dirn == 2 and c.bytes_done < need:
                c.early_cqe = (here, c.bytes_done, need)
                c.flags.append("short data at CQE")
            elif c.dirn == 1 and (c.np_open > 0 or c.bytes_done < need):
                c.early_cqe = (here, c.bytes_done, need)
                c.flags.append("short data at CQE")
        # ---- poisoned data then success
        if c.poisoned is not None and status == 0:
            card = "L-38" if c.dirn == 1 else "V-37"
            self.emit(card, "POISON-SUCCESS", "error", "data-integrity",
                      f"{c.name} SQ{c.sqid} CID 0x{c.cid:X} completed with Success although its data carried EP=1 (poisoned) at row "
                      f"{c.poisoned[0]}: poisoned data was accepted as good" + (" and may now be on the media." if c.dirn == 1 else "."),
                      cmd=c, evidence=[self.ev("poisoned TLP", c.poisoned[0], c.poisoned[1], c.poisoned[2])],
                      related=["V-37", "L-12", "L-38"])
        if c.pcie_error is not None and status == 0:
            c.flags.append(f"success after PCIe {c.pcie_error[0]}")
            self.emit("S-04", "DMA-ERROR-SUCCESS", "error", "data-integrity",
                      f"{c.name} SQ{c.sqid} CID 0x{c.cid:X} completed with Success although one of its DMA reads got a "
                      f"{c.pcie_error[0]} completion (row {c.pcie_error[1]}): the command should have failed with Data Transfer Error.",
                      cmd=c, related=["L-20", "L-15"], side="D")
        # ---- status codes
        if status:
            self.counts_nvme["error_status"] += 1
            name, card = nd.status_info(sct, sc)
            if (sct, sc) in nd.SUCCESS_LIKE:
                sev = "info"
            elif card in ("S-07", "S-08", "S-21", "S-62", "S-64", "S-17", "S-19", "S-29", "S-27"):
                sev = "warning"
            else:
                sev = "error"
            extra = ""
            if (sct, sc) in nd.NOT_ON_PCIE:
                extra = " This code is Fabrics- or host-generated and should never be posted by a PCIe controller."
            if card is None:
                card = "S-70"
            self.emit(card, "CQE-STATUS", sev, "status-code",
                      f"{c.name} SQ{c.sqid} CID 0x{c.cid:X} completed with status 0x{status & 0x7FF:03X} = SCT {sct} / SC 0x{sc:02X} "
                      f"'{name}' (DNR={dnr}, More={more}, CRD={crd})" + (f", NSID {c.nsid}" if c.nsid else "")
                      + (f", SLBA {c.slba} NLB {c.nlb}" if c.slba is not None else "") + "." + extra,
                      cmd=c, at=here, metrics={"sct": sct, "sc": f"0x{sc:02X}", "dnr": dnr, "more": more, "crd": crd})
            self.retry_track(c, status, dnr, crd, here)
            if card == "S-27":
                self.nnr_track(c, here)
        if more:
            self.emit("S-71", "CQE-MORE", "info", "status-code",
                      f"CQE for {c.name} CID 0x{c.cid:X} has More=1: an Error Information log entry exists; read it with "
                      f"'nvme error-log' (partly visible: the log content is only in the trace if the host reads it).", cmd=c)
        if c.sqid == 0:
            self.admin_on_complete(c, dw0, status)
        else:
            self.io_on_complete(c, status)
        # ---- timeouts measured at completion
        lat = ts - (c.db_ts if c.db_ts is not None else c.fetch_ts)
        lim = self.cfg.admin_timeout_s if c.sqid == 0 else self.cfg.io_timeout_s
        if lat > lim and not c.timeout_flag and not (c.sqid == 0 and c.opc in nd.LONG_LIVED_ADMIN):
            c.timeout_flag = True
            self.emit("T-23" if c.sqid == 0 else "T-22", "LATE-COMPLETION", "error", "timeout",
                      f"{c.name} SQ{c.sqid} CID 0x{c.cid:X} completed {lat:.3f} s after its doorbell, beyond the "
                      f"{'admin' if c.sqid == 0 else 'I/O'} timeout of {lim:g} s (configurable; Linux default): the host has most likely "
                      f"already aborted or reset.", cmd=c, related=["T-01", "C-14"] + (["T-11"] if c.opc == 0x80 and c.sqid == 0 else []),
                      at=self.ev("doorbell", c.db_row, c.db_pkt, c.db_ts) if c.db_ts is not None else None,
                      evidence=[here], metrics={"latency_s": round(lat, 6)})

    def retry_track(self, c, status, dnr, crd, here):
        if dnr:
            return
        key = (c.sqid != 0, c.opc, c.nsid, c.slba, c.nlb)
        e = self.retry_tab.get(key)
        ts = self.now
        if e is None or ts - e[0] > self.cfg.retry_window_s:
            e = [ts, 0, here, False]
            self.retry_tab[key] = e
            if len(self.retry_tab) > 4096:
                self.retry_tab.popitem(last=False)
        e[1] += 1
        if e[1] > self.cfg.max_retries and not e[3]:
            e[3] = True
            self.emit("T-24", "RETRY-BUDGET", "error", "timeout",
                      f"{c.name} NSID {c.nsid} SLBA {c.slba} NLB {c.nlb} failed {e[1]} times without DNR within "
                      f"{ts - e[0]:.2f} s (max_retries {self.cfg.max_retries}, configurable): the host retry budget is exhausted.",
                      cmd=c, at=e[2], evidence=[here], related=["S-21", "T-17"])
        if crd and c.sqe is not None:
            s = c.sqe
            self.crd_wait[(s.opc, s.nsid, s.cdw10, s.cdw11, s.cdw12)] = (ts, crd, here)
            if len(self.crd_wait) > 4096:
                self.crd_wait.popitem(last=False)

    def nnr_track(self, c, here):
        ts = self.now
        if self.nnr is None or ts - self.nnr[1] > 5.0:
            self.nnr = [ts, ts, here, False]
        self.nnr[1] = ts
        if ts - self.nnr[0] > self.cfg.ns_not_ready_s and not self.nnr[3]:
            self.nnr[3] = True
            self.emit("T-18", "NS-NOT-READY", "error", "timeout",
                      f"Namespace Not Ready has been returned for {ts - self.nnr[0]:.2f} s (threshold {self.cfg.ns_not_ready_s} s, "
                      f"configurable).", cmd=c, at=self.nnr[2], evidence=[here], related=["S-27"])

    def admin_on_complete(self, c: Cmd, dw0, status):
        s = c.sqe
        if s is None:
            return
        o = s.opc
        ok = status == 0
        if o == 0x05 and ok:
            qid = s.cdw10 & 0xFFFF
            size = (s.cdw10 >> 16) + 1
            pc = s.cdw11 & 1
            ien = s.cdw11 >> 1 & 1
            iv = s.cdw11 >> 16
            self.create_cq(qid, s.prp1, size, iv, ien, True, pc)
        elif o == 0x01 and ok:
            qid = s.cdw10 & 0xFFFF
            size = (s.cdw10 >> 16) + 1
            self.create_sq(qid, s.prp1, size, s.cdw11 >> 16, True, s.cdw11 & 1, (s.cdw11 >> 1) & 3)
        elif o == 0x00 and ok:
            qid = s.cdw10 & 0xFFFF
            for k, x in list(self.cmds.items()):
                if k[0] == qid and not x.done:
                    x.flags.append("SQ deleted while outstanding")
                    self.finish_cmd(x, reason="SQ deleted")
            self.delete_sq(qid)
        elif o == 0x04 and ok:
            self.delete_cq(s.cdw10 & 0xFFFF)
        elif o == 0x09 and ok:
            fid = s.cdw10 & 0xFF
            if fid == 0x07:
                self.granted_q = ((dw0 & 0xFFFF) + 1, (dw0 >> 16) + 1)
            elif fid == 0x08:
                self.coalesce = (s.cdw11 & 0xFF, (s.cdw11 >> 8) & 0xFF)
            elif fid == 0x16:
                self.acre = bool(s.cdw11 & 1)       # Host Behavior Support: data buffer; ACRE is byte 0 of the buffer
            elif fid == 0x0D:
                self.hmb_enabled = bool(s.cdw11 & 1)
        elif o == 0x08 and ok:
            for a in self.aborts:
                if a[0] is c:
                    a[3] = (self.now, dw0 & 1, self.ev_here("Abort CQE"))
        elif o == 0x0C and ok:
            et = dw0 & 7
            info = (dw0 >> 8) & 0xFF
            lid = (dw0 >> 16) & 0xFF
            name, card = nd.AEN_CARD.get((et, info), (f"{nd.AEN_TYPE.get(et, 'type ' + str(et))} event info 0x{info:02X}", None))
            if card is None:
                card = "S-79" if et == 0 else ("S-84" if et == 2 else "S-70")
            sev = "warning" if et in (0, 1) else "info"
            rel = ["C-09"] if card == "S-78" else (["C-11"] if (et, info) == (0, 3) else [])
            self.emit(card, "AEN", sev, "status-code",
                      f"Asynchronous Event: {name} (type {et}, info 0x{info:02X}, log page 0x{lid:02X}). The host should read log "
                      f"page 0x{lid:02X}.", cmd=c, related=rel, metrics={"event_type": et, "event_info": info, "log_page": lid})
        if ok and c.capture is not None:
            self.parse_admin_data(c)

    def parse_admin_data(self, c: Cmd):
        s = c.sqe
        b = bytes(c.capture)
        if not any(b):
            return
        if s.opc == 0x06:
            cns = s.cdw10 & 0xFF
            if cns == 0x01:
                info = nd.parse_identify_ctrl(b)
                if len(b) >= 272:
                    info["mtfa"] = struct.unpack_from("<H", b, 270)[0]
                if len(b) >= 92:
                    info["rtd3e"] = struct.unpack_from("<I", b, 88)[0]
                self.ctrl_info.update(info)
                if not self.cfg.mdts_bytes and "mdts" in info:
                    m = info["mdts"]
                    self.mdts_bytes = (self.mpsmin << m) if m else None
                    self.mdts_src = f"Identify Controller MDTS={m} x MPSMIN {self.mpsmin} B (row {c.cqe_row})"
            elif cns == 0x00 and c.nsid:
                info = nd.parse_identify_ns(b)
                old = self.ns_info.get(c.nsid)
                self.ns_info[c.nsid] = info
                if "lba_size" in info and not self.cfg.lba_size:
                    self.lba_size = info["lba_size"]
                    self.lba_src = f"Identify Namespace {c.nsid} (row {c.cqe_row})"
                for idk in ("nguid", "eui64"):
                    v = info.get(idk)
                    if v and int(v, 16):
                        other = self.ns_ids.get((idk, v))
                        if other is not None and other != c.nsid:
                            self.emit("D-22", "NSID-DUP", "error", "protocol-state",
                                      f"Identify Namespace: namespaces {other} and {c.nsid} report the same {idk.upper()} {v}.",
                                      cmd=c, related=["C-24"])
                        self.ns_ids[(idk, v)] = c.nsid
                if old and "nuse" in old and "nuse" in info and self.dsm_ranges and info["nuse"] == old["nuse"]:
                    self.emit("D-07", "NUSE-FROZEN", "info", "data-integrity",
                              f"Namespace {c.nsid} NUSE stayed {info['nuse']} across {self.dsm_ranges} deallocated range(s): "
                              f"allocation accounting may be wrong (partly visible: NUSE semantics depend on thin provisioning).",
                              cmd=c, related=["D-01"])
        elif s.opc == 0x02:
            lid = s.cdw10 & 0xFF
            if lid == 0x02 and len(b) >= 1:
                cw = b[0]
                for bit, nm, card in nd.SMART_WARN_CARDS:
                    if cw >> bit & 1:
                        self.emit(card, "SMART-WARN", "warning", "status-code",
                                  f"SMART / Health log read by the host shows critical warning bit {bit}: {nm} (critical_warning=0x{cw:02X}).",
                                  cmd=c)
            elif lid == 0x03 and len(b) >= 1:
                afi = b[0]
                if afi >> 4 & 7:
                    self.emit("S-92", "FW-PENDING", "info", "status-code",
                              f"Firmware Slot log: AFI=0x{afi:02X}, a firmware image is pending activation at the next reset.", cmd=c,
                              related=["D-26"])

    def io_on_complete(self, c: Cmd, status):
        s = c.sqe
        if s is None:
            return
        o = s.opc
        if status != 0:
            return
        if o == 0x00:
            if self.ctrl_info.get("vwc", 1) & 1 and self.writes_since_flush >= 1 << 20 and c.db_ts is not None:
                lat = (self.now - c.db_ts) * 1e6
                if lat < self.cfg.flush_min_us:
                    self.emit("D-08", "FLUSH-FAST", "info", "data-integrity",
                              f"Flush completed {lat:.2f} us after its doorbell although {self.writes_since_flush >> 20} MiB were written "
                              f"since the previous flush: the cache may not really be flushed (partly visible; prove with a power-loss test).",
                              cmd=c)
            self.writes_since_flush = 0
        elif o == 0x09 and (s.cdw11 >> 2) & 1:
            ranges = []
            if c.capture is not None:
                b = bytes(c.capture)
                for i in range(min((s.cdw10 & 0xFF) + 1, len(b) // 16)):
                    _, nlb, slba = struct.unpack_from("<IIQ", b, i * 16)
                    ranges.append((slba, nlb))
            tot = sum(r[1] for r in ranges)
            self.emit("D-01", "DEALLOCATE", "info", "data-integrity",
                      f"Dataset Management Deallocate of {(s.cdw10 & 0xFF) + 1} range(s)" + (f", {tot} LBAs: " + ", ".join(
                          f"{a}+{n}" for a, n in ranges[:8]) + ("..." if len(ranges) > 8 else "") if ranges else "")
                      + " completed with Success. The trace proves the command and its range list, not that the LBAs were "
                        "unmapped (partly visible).", cmd=c, related=["D-02", "D-03", "D-06"],
                      metrics={"ranges": [list(r) for r in ranges[:64]], "lbas": tot})
            if self.dealloc is not None:
                for slba, nlb in ranges:
                    for l in range(slba, slba + min(nlb, 4096)):
                        self.dealloc[l] = True
                        self.verify.pop(l, None)
        elif o == 0x08:
            self.emit("D-05", "WRITE-ZEROES", "info", "data-integrity",
                      f"Write Zeroes SLBA {c.slba} NLB {c.nlb} completed with Success; whether the blocks read back as zero needs a "
                      f"read-back (partly visible).", cmd=c, related=["C-23"])
            if self.zeroed is not None and c.slba is not None:
                for l in range(c.slba, c.slba + min(c.nlb, 4096)):
                    self.zeroed[l] = True
                    self.verify.pop(l, None)
        if self.verify is not None and c.vbuf is not None and c.slba is not None:
            self.verify_cmd(c)

    def verify_cmd(self, c: Cmd):
        lba = self.ns_info.get(c.nsid, {}).get("lba_size", self.lba_size)
        b = c.vbuf
        if c.bytes_done < c.nbytes:
            return
        for i in range(c.nlb):
            blk = bytes(b[i * lba:(i + 1) * lba])
            l = c.slba + i
            h = hashlib.blake2b(blk, digest_size=8).digest()
            if c.opc == 0x01:
                if len(self.verify) < self.cfg.verify_lbas or l in self.verify:
                    self.verify[l] = (h, c.db_row)
                self.dealloc.pop(l, None)
                self.zeroed.pop(l, None)
            else:
                if l in self.zeroed and any(blk):
                    self.emit("D-05", "ZEROES-NOT-ZERO", "error", "data-integrity",
                              f"Read of LBA {l} after Write Zeroes returned non-zero data.", cmd=c, related=["C-23"])
                    return
                if l in self.dealloc:
                    dl = self.ns_info.get(c.nsid, {}).get("dlfeat", 0)
                    if (dl & 7) == 1 and any(blk):
                        self.emit("D-03", "DEALLOC-OLD-DATA", "error", "data-integrity",
                                  f"Read of deallocated LBA {l} returned non-zero data although DLFEAT promises zeroes.", cmd=c,
                                  related=["D-01"])
                        return
                    continue
                w = self.verify.get(l)
                if w is not None and w[0] != h:
                    self.emit("D-15", "READ-MISMATCH", "error", "data-integrity",
                              f"Read of LBA {l} ({c.name} CID 0x{c.cid:X}) returned data that differs from the last write captured to it "
                              f"(write doorbell row {w[1]}): stale, lost, misdirected or corrupted data (D-13/D-14/D-16).",
                              cmd=c, related=["D-13", "D-14", "D-16"])
                    return

    # ---------------------------------------------------------------------------------------------- MSI-X
    def msix_tracking_ok(self):
        return self.counts_nvme["msix"] > 0

    def vec_cqe(self, q: CQ, c):
        if not q.ien:
            return
        v = q.vector
        self.vec_pending[v] = self.vec_pending.get(v, 0) + 1
        q.await_irq.append((self.now, c))
        if self.msix_wait:
            keep = deque()
            for w in self.msix_wait:
                if w[1] == v or w[1] is None:
                    dt_us = (self.now - w[3]) * 1e6
                    self.emit("V-14", "MSIX-BEFORE-CQE", "error", "reordering",
                              f"MSI-X (vector {v}) was sent {dt_us:.2f} us BEFORE the CQE it announces (CQ{q.qid}): the interrupt "
                              f"handler finds nothing and the completion waits for the next interrupt.",
                              at=w[2], evidence=[self.ev_here("CQE that followed")], cmd=c, related=["V-33", "P-08"])
                    self.vec_pending[v] -= 1
                    self._msix_mark(q)
                else:
                    keep.append(w)
            self.msix_wait = keep

    def _msix_mark(self, q: CQ):
        ts, row, pkt = self.now, self.row, self.pkt()
        while q.await_irq:
            t0, c = q.await_irq.popleft()
            if c is not None and c.msix_ts is None:
                c.msix_ts, c.msix_row, c.msix_pkt = ts, row, pkt

    def on_msix(self, a, data, attr):
        self.counts_nvme["msix"] += 1
        b = self.payload(data)
        dv = struct.unpack_from("<I", b, 0)[0] if len(b) >= 4 else None
        if attr and self._ro_bit(attr):
            self.emit("V-33", "RO-MSIX", "warning", "reordering",
                      f"MSI-X write to 0x{a:X} has the Relaxed Ordering attribute set ('{attr.strip()}'): it may pass the CQE.",
                      related=["V-14"])
        vec = None
        if self.msix_by_key:
            vec = self.msix_by_key.get((a, dv))
            if vec is None:
                self.emit("L-35", "MSIX-UNKNOWN", "error", "protocol-state",
                          f"MSI-X write to 0x{a:X} data 0x{(dv or 0):X} matches no entry of the MSI-X table programmed in this capture.",
                          related=["V-14"])
                return
            ent = self.msix_entries.get(vec)
            if ent and ent[2]:
                self.emit("L-35", "MSIX-MASKED", "error", "protocol-state",
                          f"MSI-X for vector {vec} was sent although the vector is masked in the MSI-X table.", related=["V-15"])
        cqs = [q for q in self.cqs.values() if q.ien and (vec is None or q.vector == vec)]
        if vec is None:
            # no table captured: tie the message data to the CQ that has the oldest un-announced CQE
            if dv is not None and dv in self.msix_learn:
                cq_id = self.msix_learn[dv]
                cqs = [q for q in cqs if q.qid == cq_id]
            else:
                waiting = [q for q in cqs if q.await_irq]
                if waiting:
                    q0 = min(waiting, key=lambda q: q.await_irq[0][0])
                    if dv is not None:
                        self.msix_learn[dv] = q0.qid
                    cqs = [q0]
                else:
                    cqs = []
        pend = [q for q in cqs if q.await_irq]
        if pend:
            for q in pend:
                self.vec_pending[q.vector] = 0
                self._msix_mark(q)
            return
        if not self.cqs:
            return
        v = vec if vec is not None else (cqs[0].vector if cqs else None)
        self.msix_wait.append((self.now + self.cfg.msix_before_cqe_us * 1e-6, v, self.ev_here(f"MSI-X vector {v}"), self.now))

    def msix_periodic(self):
        now = self.now
        while self.msix_wait and self.msix_wait[0][0] <= now:
            dl, v, ev0, t0 = self.msix_wait.popleft()
            if self.counts_nvme["cqe"] == 0:
                continue
            self.emit("V-14", "MSIX-NO-CQE", "warning", "reordering",
                      f"MSI-X (vector {v}) with no new CQE on any completion queue mapped to that vector since the previous interrupt: "
                      f"a spurious interrupt (the handler returns IRQ_NONE).", at=ev0, related=["P-08"])
        if not self.counts_nvme["msix"]:
            return
        win = self.cfg.msix_window_us * 1e-6
        if self.coalesce:
            win += self.coalesce[1] * 100e-6
        for q in self.cqs.values():
            if not q.ien or not q.await_irq:
                continue
            t0, c = q.await_irq[0]
            if now - t0 > win:
                q.await_irq.popleft()
                masked = ""
                if self.msix_fmask:
                    masked = " The MSI-X Function Mask bit is set (host side)."
                ent = self.msix_entries.get(q.vector)
                if ent and ent[2]:
                    masked = f" Vector {q.vector} is masked in the MSI-X table (host side)."
                if c is not None:
                    c.flags.append("no MSI-X")
                self.emit("V-15", "CQE-NO-MSIX", "error", "timeout",
                          f"CQE posted to CQ{q.qid} (interrupts enabled, vector {q.vector}) with no MSI-X within "
                          f"{win * 1e6:.0f} us (configurable" + (", includes the interrupt-coalescing time" if self.coalesce else "")
                          + f").{masked}", cmd=c,
                          at=self.ev("CQE", c.cqe_row, c.cqe_pkt, c.cqe_ts) if c is not None else None,
                          related=["T-21", "C-15", "D-30", "L-35"], side="H" if masked else None)
                while q.await_irq and now - q.await_irq[0][0] > win:
                    t1, c1 = q.await_irq.popleft()
                    if c1 is not None:
                        c1.flags.append("no MSI-X")

    # ---------------------------------------------------------------------------------------------- lifecycle end
    def finish_cmd(self, c: Cmd, reason=None):
        if c.done:
            return
        c.done = True
        key = (c.sqid, c.cid)
        if self.cmds.get(key) is c:
            del self.cmds[key]
        if c.cqe_ts is None:
            sq = self.sqs.get(c.sqid)
            if sq is not None and sq.outstanding > 0:
                sq.outstanding -= 1
            if reason:
                c.flags.append(reason)
        else:
            if c.early_cqe is not None:
                ev0, done, need = c.early_cqe
                c.early_cqe = None
                if c.bytes_done < need:
                    self.emit("V-13", "CQE-SHORT-DATA", "warning", "reordering",
                              f"CQE (Success) for {c.name} SQ{c.sqid} CID 0x{c.cid:X} was posted after only {done} of {need} data bytes "
                              f"were seen on the link, and the rest never appeared: either the device completed early / short, or the "
                              f"capture filtered data TLPs.", cmd=c, at=ev0, related=["V-16", "X-30"])
            self.check_outlier(c)
        for r in c.regions:
            r.live = False
        self.recent_done[key] = c
        while len(self.recent_done) > self.cfg.recent_completed:
            k, old = self.recent_done.popitem(last=False)
            for r in old.regions:
                self.remove_region(r)
            old.regions = []
        c.vbuf = None
        c.capture = None
        self.write_cmd(c)
        self._wake_of.pop(key, None)

    def check_outlier(self, c: Cmd):
        if c.cqe_ts is None or c.outlier_checked:
            return
        c.outlier_checked = True
        t0 = c.db_ts if c.db_ts is not None else c.fetch_ts
        if t0 is None:
            return
        lat_us = (c.cqe_ts - t0) * 1e6
        key = (c.sqid == 0, c.name, size_bucket(c.nbytes))
        h = self.cmd_hist.get(key)
        if h is None:
            h = self.cmd_hist[key] = LogHist()
            self.stage_hist[key] = {}
        st = c.stages()
        sh = self.stage_hist[key]
        base = {}
        for k2, v in st.items():
            if v is None or k2.startswith("msix_to") or k2.startswith("total"):
                continue
            hh = sh.get(k2)
            if hh is None:
                hh = sh[k2] = LogHist()
            base[k2] = hh.quantile(0.5) if hh.n >= self.cfg.outlier_min_samples else None
        flagged = False
        if h.n >= self.cfg.outlier_min_samples and c.status == 0:
            p99 = h.quantile(0.99)
            thr = max(self.cfg.outlier_k * p99, self.cfg.outlier_floor_us)
            if lat_us > thr:
                flagged = True
                self.report_outlier(c, lat_us, h, p99, thr, st, base)
        h.add(lat_us)
        la = self.lat_all.get(c.name)
        if la is None:
            la = self.lat_all[c.name] = LogHist()
        la.add(lat_us)
        for k2, v in st.items():
            if v is None or k2 not in sh:
                continue
            if not flagged:
                sh[k2].add(max(v, 0.0))

    def report_outlier(self, c: Cmd, lat_us, h, p99, thr, st, base):
        excess = {}
        for k2, v in st.items():
            if v is None or k2.startswith("msix_to") or k2.startswith("total"):
                continue
            b = base.get(k2)
            excess[k2] = v - (b if b is not None else 0.0)
        dom = max(excess, key=lambda k: excess[k]) if excess else None
        t0 = c.db_ts if c.db_ts is not None else c.fetch_ts
        ivs = self.intervals_overlapping(t0, c.cqe_ts)
        wake = self._wake_of.get((c.sqid, c.cid))
        causes = []
        cards = []
        side = None
        for iv in ivs[:6]:
            causes.append(f"{iv[2]} at row {iv[3]} ({iv[5]}) overlapped the command")
        if wake is not None:
            causes.append(f"the doorbell was the first traffic after an L1 exit of {wake['exit_us']:.1f} us (row {wake['row']}): "
                          f"the host waited for the link to wake before it could ring the doorbell")
            cards.append("P-07")
        kinds = {iv[2].split(" ")[0] for iv in ivs}
        if "recovery" in kinds or "speed" in kinds:
            cards.append("X-11")
            side = "L"
        if "replay" in kinds:
            cards.append("X-18")
            side = "L"
        if "credit" in kinds:
            cards.append("T-38")
        if dom and dom.startswith("fetch_to_sqe") or (dom == "data_transfer" and c.dirn == 1 and c.max_read_wait > 0.5 * excess.get(dom, 0)):
            causes.append("the host was slow to return read completions (SQE / write data): host memory or root-complex latency")
            side = side or "H"
        if not causes:
            if dom in ("sqe_to_first_data (device processing)", "last_data_to_cqe (device)", "doorbell_to_fetch"):
                causes.append("no link event, replay or credit stall overlapped the command: the delay is inside the device "
                              "(firmware, media read retry, garbage collection or thermal throttling; prove with SMART / telemetry / "
                              "latency histograms: T-48, P-05, T-49)")
                cards += ["T-48", "P-05", "T-49"]
                side = "D"
            else:
                causes.append("no overlapping link event; see the dominant stage")
        card = "T-51" if (c.sqid != 0 and (c.opc == 0x00 or (c.sqe is not None and nd.fua(c.sqid, c.sqe)))) else "T-47"
        if wake is not None and card == "T-47" and dom == "doorbell_to_fetch":
            card = "P-07"
        ev = []
        if c.db_row:
            ev.append(self.ev("doorbell", c.db_row, c.db_pkt, c.db_ts))
        if c.fetch_row:
            ev.append(self.ev("SQE fetch", c.fetch_row, c.fetch_pkt, c.fetch_ts))
        if c.fd_row:
            ev.append(self.ev("first data", c.fd_row, c.fd_pkt, c.fd_ts))
        if c.ld_row:
            ev.append(self.ev("last data", c.ld_row, c.ld_pkt, c.ld_ts))
        self.emit(card, "LATENCY-OUTLIER", "warning", "performance",
                  f"{c.name} SQ{c.sqid} CID 0x{c.cid:X} ({c.nbytes} B) took {lat_us:.1f} us doorbell->CQE; the running p99 for this "
                  f"opcode/size is {p99:.1f} us over {h.n} commands (outlier threshold max({self.cfg.outlier_k:g} x p99, "
                  f"{self.cfg.outlier_floor_us:g} us) = {thr:.1f} us, configurable). Dominant stage: {dom} "
                  f"({excess.get(dom, 0):.1f} us above its median). Why: " + "; ".join(causes) + ".",
                  cmd=c, at=self.ev("CQE", c.cqe_row, c.cqe_pkt, c.cqe_ts), evidence=ev, stage=st,
                  related=sorted(set(cards) | {"P-03"}), side=side,
                  metrics={"latency_us": round(lat_us, 3), "p99_us": round(p99, 3), "threshold_us": round(thr, 3),
                           "dominant_stage": dom, "stage_excess_us": {k: round(v, 3) for k, v in excess.items()},
                           "overlapping_link_intervals": [{"kind": iv[2], "row": iv[3], "pkt": iv[4], "detail": iv[5],
                                                           "t0": iv[0], "t1": iv[1]} for iv in ivs[:10]]})

    # ---------------------------------------------------------------------------------------------- periodic / end
    def nvme_periodic(self):
        now = self.now
        self.msix_periodic()
        # CQ head not advancing (V-07 a)
        for q in self.cqs.values():
            if q.posted and not q.stall_flag:
                ref = q.head_ts if q.head_ts is not None else q.posted[0].cqe_ts
                oldest = q.posted[0].cqe_ts
                if ref is not None and oldest is not None and now - max(ref, oldest) > self.cfg.cq_stall_ms * 1e-3:
                    q.stall_flag = True
                    full = len(q.posted) >= q.size - 1
                    self.emit("V-07", "CQ-STALL", "error" if full else "warning", "timeout",
                              f"CQ{q.qid} holds {len(q.posted)} posted CQE(s) and the host has not advanced its head doorbell for "
                              f"{(now - max(ref, oldest)) * 1e3:.0f} ms (threshold {self.cfg.cq_stall_ms} ms, configurable)"
                              + (": the CQ is full and the controller must stop posting." if full else "."),
                              cmd=q.posted[0], related=["V-15", "T-21"], side="H")
        # Abort that did not abort (T-09)
        if self.aborts:
            keep = []
            for a in self.aborts:
                c, tsq, tcid, res = a
                if res is None:
                    keep.append(a)
                    continue
                t_done, not_aborted, ev0 = res
                tgt = self.cmds.get((tsq, tcid))
                if tgt is None or tgt.done:
                    continue
                if now - t_done > self.cfg.abort_check_s:
                    self.emit("T-09", "ABORT-NOT-DONE", "error", "timeout",
                              f"Abort for SQ{tsq} CID 0x{tcid:X} completed (DW0 bit0 = {not_aborted}: "
                              f"{'controller says not aborted' if not_aborted else 'controller says aborted'}) but the command is still "
                              f"outstanding {now - t_done:.2f} s later.", cmd=tgt, at=ev0, related=["C-14", "C-21"])
                    continue
                keep.append(a)
            self.aborts = keep[-256:]
        # commands outstanding beyond the host timeout (reported once, while the trace continues)
        if self.cmds and int(now * 4) != getattr(self, "_last_to_scan", -1):
            self._last_to_scan = int(now * 4)
            for c in list(self.cmds.values()):
                if c.done or c.timeout_flag or c.cqe_ts is not None:
                    continue
                t0 = c.db_ts if c.db_ts is not None else c.fetch_ts
                if t0 is None:
                    continue
                lim = self.cfg.admin_timeout_s if c.sqid == 0 else self.cfg.io_timeout_s
                if c.sqid == 0 and c.opc in nd.LONG_LIVED_ADMIN:
                    continue
                if now - t0 > lim:
                    c.timeout_flag = True
                    self.timeout_finding(c, now - t0, lim, still=True)

    def timeout_finding(self, c: Cmd, age, lim, still=False, at_end=False):
        stage = self.stuck_stage(c)
        admin = c.sqid == 0
        if at_end and age < lim:
            card = "C-08" if admin else "T-01"
            self.emit(card, "NEVER-COMPLETED", "error" if age > 1.0 else "warning", "timeout",
                      f"{c.name} SQ{c.sqid} CID 0x{c.cid:X} has no CQE at the end of the trace, {age * 1e3:.1f} ms after its doorbell "
                      f"(below the {lim:g} s host timeout, so the host had not given up yet). Stuck in stage: {stage}.",
                      cmd=c, at=self.ev("doorbell", c.db_row, c.db_pkt, c.db_ts) if c.db_ts is not None else
                      self.ev("SQE fetch", c.fetch_row, c.fetch_pkt, c.fetch_ts), related=["T-01", "T-22", "C-08", "X-30"],
                      metrics={"age_ms": round(age * 1e3, 3), "stuck_stage": stage})
            return
        card = "T-23" if admin else "T-22"
        self.emit(card, "TIMEOUT", "error", "timeout",
                  f"{c.name} SQ{c.sqid} CID 0x{c.cid:X} has been outstanding {age:.3f} s, beyond the {'admin' if admin else 'I/O'} "
                  f"timeout of {lim:g} s (configurable; Linux default): the host will abort / reset. Stuck in stage: {stage}.",
                  cmd=c, at=self.ev("doorbell", c.db_row, c.db_pkt, c.db_ts) if c.db_ts is not None else
                  self.ev("SQE fetch", c.fetch_row, c.fetch_pkt, c.fetch_ts), related=["T-01", "C-08" if admin else "C-14"],
                  metrics={"age_s": round(age, 6), "stuck_stage": stage})

    def stuck_stage(self, c: Cmd) -> str:
        if c.fetch_ts is None:
            return "doorbell rung, SQE never fetched by the device"
        if c.sqe_ts is None:
            return "SQE fetch never completed by the host"
        if c.nbytes and c.bytes_done < c.nbytes:
            return f"data transfer incomplete ({c.bytes_done} of {c.nbytes} bytes)"
        if c.nbytes:
            return "all data transferred, CQE never posted (device)"
        return "SQE fetched, CQE never posted (device)"

    def nvme_finish(self):
        end = self.t_last if self.t_last is not None else self.now
        self.msix_periodic()
        # slots rung but never fetched
        for q in self.sqs.values():
            for s, p in list(q.slots.items()):
                age = end - p[0]
                if age * 1e3 > self.cfg.pending_min_ms:
                    c = Cmd(q.qid, s)
                    c.db_ts, c.db_row, c.db_pkt, c.qd_sq, c.qd_all, c.link = p[0], p[1], p[2], p[3], p[4], p[5]
                    c.name = f"(unfetched SQ{q.qid} slot {s})"
                    lim = self.cfg.admin_timeout_s if q.qid == 0 else self.cfg.io_timeout_s
                    card = ("T-23" if q.qid == 0 else "T-22") if age > lim else ("C-08" if q.qid == 0 else "T-01")
                    self.emit(card, "NEVER-FETCHED", "error", "timeout",
                              f"SQ{q.qid} slot {s} was made visible by the tail doorbell at row {p[1]} but the device never fetched it "
                              f"({age * 1e3:.1f} ms until the end of the trace).", at=self.ev("doorbell", p[1], p[2], p[0]),
                              cmd=c, related=["T-01", "V-01"], metrics={"age_ms": round(age * 1e3, 3)})
                    break
        for c in list(self.cmds.values()):
            if c.done:
                continue
            if c.cqe_ts is not None:
                continue
            t0 = c.db_ts if c.db_ts is not None else c.fetch_ts
            if t0 is None:
                continue
            age = end - t0
            if c.sqid == 0 and c.opc in nd.LONG_LIVED_ADMIN:
                continue
            if c.timeout_flag:
                continue
            lim = self.cfg.admin_timeout_s if c.sqid == 0 else self.cfg.io_timeout_s
            if age * 1e3 > self.cfg.pending_min_ms:
                self.timeout_finding(c, age, lim, at_end=True)
        # early CQEs still open
        for q in self.cqs.values():
            for c in list(q.posted):
                self.finish_cmd(c)
        for c in list(self.cmds.values()):
            self.finish_cmd(c, reason="no CQE in trace")
        for c in list(self.recent_done.values()):
            pass
        # interrupt storm (P-08)
        m, cq = self.counts_nvme["msix"], self.counts_nvme["cqe"]
        if cq > 1000 and m > 2 * cq:
            self.emit("P-08", "IRQ-STORM", "warning", "performance",
                      f"{m} MSI-X messages for {cq} CQEs ({m / cq:.1f} per completion): interrupt storm or coalescing misconfiguration.",
                      at=self.ev("end of trace", self.row, None, end))
        if self.ctrl_info.get("hmpre") and not self.hmb_enabled and self.counts_nvme["completed"] > 100:
            self.emit("T-16", "HMB-MISSING", "info", "performance",
                      f"Identify Controller reports HMPRE={self.ctrl_info['hmpre']} (the drive wants a Host Memory Buffer) but no Set "
                      f"Features HMB enable was seen in the capture (partly visible: it may have been set before the capture).",
                      at=self.ev("end of trace", self.row, None, end))
