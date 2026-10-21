"""SYNTHETIC Teledyne LeCroy "pex -> CSV" packet export generator (PCIe Protocol Analysis 13.28.b.15 112-column layout).

Everything this module writes is synthetic.  It models a Gen4 x4 NVMe SSD link from training through enumeration,
controller enable, admin queue setup (Identify, Set Features, Create I/O CQ/SQ, MSI-X table programming) and I/O with
real SQE / CQE / PRP-list bytes, Ack DLLPs, flow-control credit updates and SKP ordered sets.  In fault mode it injects
at least one fault per rule family and records ground truth (expected atlas card + packet number) in
injected_faults.json.  In clean mode nothing is injected.

Usage:
  python -m pex_checker.synth --out DIR --rows 150000            # fault mode
  python -m pex_checker.synth --out DIR --rows 150000 --clean    # clean mode
  python -m pex_checker.synth --out DIR --size-gb 1.0            # big file for throughput measurement
"""
from __future__ import annotations

import argparse
import json
import os
import random
import struct
import time

COLUMNS = [
    "Marker", "Packet", "Link", "Direction", "Ordered Set Type", "DLLP Type", "TLP Type", "Link Event",
    "PSN", "ACK/NAK Sequence Number", "VC ID", "HdrScale", "HdrFC", "DataScale", "DataFC", "Feature Ack", "Feature Support",
    "TC", "LN", "TH", "NW", "TD", "EP", "Attributes", "AT", "Length", "Requester ID", "Tag", "Completer ID", "Address",
    "Device ID", "Register", "First BE", "Last BE", "Cpl Status", "Byte Cnt", "BCM", "Lwr Addr", "Msg Routing", "Message Code",
    "TS Link", "TS Lane", "N_FTS", "Training_Control", "TS_Data Rate", "TS Gen3 EQ Control", "TS Gen3 Pre-cursor",
    "TS Gen3 Cursor", "TS Gen3 Post-cursor", "PMUX Channel ID", "PMUX Metadata",
    "Snoop Requirement", "Snoop Scale", "Snoop Value", "No Snoop Requirement", "No Snoop Scale", "No Snoop Value",
    "PTM Master Time[63:32]", "PTM Master Time[31:0]", "PTM Propagation Delay",
    "ARP Command", "Slave Address", "Target Address", "Device Slave Address", "Assign Address", "Read/Write", "Byte Count",
    "Address Type", "PEC Support", "UDID Version", "Silicon Revision", "Vendor ID", "Device ID", "Zone", "IPMI", "ASF", "OEM",
    "SMBus Version", "Sub Vendor ID", "Sub Device ID", "Vendor Specific ID", "PEC",
    "LCRC", "ECRC", "CRC16", "Data", "Time Delta", "Time Stamp",
    "Jammer Phase", "Action", "Jammer Target Packet", "Jammer Result Packet",
    "CXL Packet", "CXL Packet Subtype", "Opcode", "CQID", "UQID", "Address[51:6]", "NT", "RSP_PRE", "MESI", "ChunkValid",
    "Bogus", "Poison", "Go-Err", "Meta Field", "Meta Value", "Snp Type", "LLCTRL", "Packet Subtype", "ALMP VLSM State",
    "ALMP VLSM Target",
]
COL = {}
for _i, _c in enumerate(COLUMNS):
    COL.setdefault(_c, _i)       # first "Device ID" (TLP Cfg target) wins
NCOL = len(COLUMNS)

HOST, SSD = "00:00.0", "01:00.0"
BAR0 = 0xF000_0000
DB = BAR0 + 0x1000
ASQ, ACQ = 0x1_0000_0000, 0x1_0001_0000
IOSQ, IOCQ = 0x1_1000_0000, 0x1_2000_0000
ADMIN_BUF = 0x1_3000_0000
PRPL_BASE = 0x1_4000_0000
DATA_BASE = 0x2_0000_0000
MSI_ADDR = 0xFEE0_0000
RATE = {1: "2.5 GT/s", 2: "5.0 GT/s", 3: "8.0 GT/s", 4: "16.0 GT/s"}
MPS, MRRS = 256, 512
QSIZE = 256
NQ = 8
MDTS = 5                    # 2^5 x 4 KiB = 128 KiB
LBA = 512


def hx(b: bytes) -> str:
    return b.hex(" ").upper()


def sqe(opc, cid, nsid=0, prp1=0, prp2=0, cdw10=0, cdw11=0, cdw12=0, cdw13=0, cdw14=0, cdw15=0, fuse=0, psdt=0, mptr=0,
        sgl=None) -> bytes:
    cdw0 = opc | (fuse << 8) | (psdt << 14) | (cid << 16)
    b = bytearray(struct.pack("<IIIIQQQIIIIII", cdw0, nsid, 0, 0, mptr, prp1, prp2, cdw10, cdw11, cdw12, cdw13, cdw14, cdw15))
    if sgl is not None:
        b[24:40] = sgl
    return bytes(b)


def cqe(dw0, sqhd, sqid, cid, phase, status=0) -> bytes:
    return struct.pack("<IIHHHH", dw0, 0, sqhd, sqid, cid, (status << 1) | phase)


class Writer:
    def __init__(self, path, rnd):
        self.f = open(path, "w", newline="", encoding="utf-8", buffering=4 << 20)
        self.f.write(",".join(COLUMNS) + "\n")
        self.rnd = rnd
        self.n = 0
        self.t = 0                  # ns
        self.last = 0
        self.blank = [""] * NCOL

    def size(self):
        return self.f.tell()

    def close(self):
        self.f.close()

    def row(self, d, fields):
        self.n += 1
        r = self.blank[:]
        r[1] = str(self.n)
        r[2] = "0"
        r[3] = d
        for k, v in fields.items():
            r[COL[k]] = str(v)
        r[COL["Time Delta"]] = str(self.t - self.last)
        s, ns = divmod(self.t, 1_000_000_000)
        r[COL["Time Stamp"]] = f"{s}.{ns:09d}"
        self.last = self.t
        line = ",".join(r)
        if '"' in line:
            line = ",".join('"' + x.replace('"', '""') + '"' if ('"' in x or "," in x) else x for x in r)
        self.f.write(line + "\n")
        return self.n


class Synth:
    UP, DN = "Upstream", "Downstream"

    def __init__(self, path, seed=7, clean=False):
        self.rnd = random.Random(seed)
        self.w = Writer(path, self.rnd)
        self.clean = clean
        self.psn = {self.UP: 0, self.DN: 0}
        self.accepted = {self.UP: set(), self.DN: set()}
        self.pool = [hx(bytes(self.rnd.randrange(256) for _ in range(256))) for _ in range(24)]
        self.tag_up = 0
        self.tag_dn = 0
        self.speed, self.width = 1, 1
        self.auto_ack = True
        self.fc_on = False
        self.fc_frozen = False
        self.faults = []
        self.sq_tail = {}
        self.cq_head = {}
        self.cq_tail = {}
        self.phase = {}
        self.cid = {}
        self.sq_fetch = {}
        self.page_ctr = 0
        self.prpl_ctr = 0
        self.ncmd = 0
        self.lat_cmds = 0

    # ------------------------------------------------------------------------------------------- primitives
    def adv(self, ns):
        self.w.t += int(ns)

    def lcrc(self, bad=False):
        v = f"0x{self.rnd.randrange(1 << 32):08X}"
        return v + " (Bad)" if bad else v

    def tlp(self, d, ttype, length=0, data=None, psn=None, bad=False, ack=None, ep=0, ecrc_bad=False, attr="0x0", **f):
        if psn is None:
            self.psn[d] = (self.psn[d] + 1) & 0xFFF
            psn = self.psn[d]
        fl = {"TLP Type": ttype, "PSN": psn, "TC": 0, "TD": 1 if ecrc_bad else 0, "EP": ep, "Attributes": attr, "AT": 0,
              "LN": 0, "TH": 0, "NW": 0, "Length": length, "LCRC": self.lcrc(bad)}
        if ecrc_bad:
            fl["ECRC"] = f"0x{self.rnd.randrange(1 << 32):08X} (Bad)"
        if data is not None:
            fl["Data"] = data
        fl.update(f)
        n = self.w.row(d, fl)
        acc = not bad and psn not in self.accepted[d]
        if acc:
            s = self.accepted[d]
            s.add(psn)
            s.discard((psn - 2048) & 0xFFF)
            self.fc_consume(d, ttype, length)
        if (ack if ack is not None else self.auto_ack) and acc:
            self.adv(self.rnd.randint(120, 300))
            self.dllp(self.DN if d == self.UP else self.UP, "Ack", **{"ACK/NAK Sequence Number": psn})
        return n, psn

    def dllp(self, d, t, **f):
        return self.w.row(d, {"DLLP Type": t, "CRC16": f"0x{self.rnd.randrange(1 << 16):04X}", **f})

    def ev(self, d, text):
        return self.w.row(d, {"Link Event": text})

    def os(self, d, t, **f):
        return self.w.row(d, {"Ordered Set Type": t, **f})

    def ts(self, d, kind, rate=None, lanes=None, sc=0, eq=None):
        rate = rate or self.speed
        lanes = lanes or self.width
        f = {"TS Link": 0, "TS Lane": f"0-{lanes - 1}" if lanes > 1 else "0", "N_FTS": 0x7F,
             "Training_Control": f"0x{(2 if sc else 0):02X}", "TS_Data Rate": RATE[rate] + (" (speed_change)" if sc else "")}
        if eq is not None:
            f.update({"TS Gen3 EQ Control": eq, "TS Gen3 Pre-cursor": 0, "TS Gen3 Cursor": 24, "TS Gen3 Post-cursor": 8})
        return self.os(d, kind, **f)

    def payload(self, n):
        out = []
        while n > 0:
            k = min(n, 256)
            out.append(self.rnd.choice(self.pool)[: k * 3 - 1])
            n -= k
        return " ".join(out)

    # ------------------------------------------------------------------------------------------- flow control
    def fc_init(self):
        # receiver host (credits for upstream TLPs) / receiver device (credits for downstream TLPs)
        self.fc = {
            (self.UP, "P"): [64, 1024, 0, 0, 64, 1024], (self.UP, "NP"): [32, 0, 0, 0, 32, 0], (self.UP, "CPL"): [0, 0, 0, 0, 0, 0],
            (self.DN, "P"): [32, 512, 0, 0, 32, 512], (self.DN, "NP"): [16, 0, 0, 0, 16, 0], (self.DN, "CPL"): [0, 0, 0, 0, 0, 0],
        }
        for d in (self.DN, self.UP):
            for typ in ("P", "NP", "CPL"):
                tx = self.UP if d == self.DN else self.DN       # d is the advertiser (receiver)
                v = self.fc[(tx, typ)]
                for stage in ("InitFC1", "InitFC2"):
                    self.adv(60)
                    self.dllp(d, f"{stage}-{typ}", **{"VC ID": 0, "HdrScale": 0, "HdrFC": v[0], "DataScale": 0, "DataFC": v[1]})
        self.fc_on = True

    def fc_consume(self, d, ttype, length):
        if not self.fc_on:
            return
        t = ttype[:3]
        typ = "P" if t in ("MWr", "Msg") else ("CPL" if t.startswith("Cpl") else "NP")
        v = self.fc[(d, typ)]
        hinf, dinf = v[4] == 0, v[5] == 0
        data_cr = (length * 4 + 15) // 16 if (typ != "NP" or ttype.startswith(("CfgWr", "IOWr"))) and not ttype.startswith("Msg ") else 0
        if not hinf:
            v[2] = (v[2] + 1) % 256
        if not dinf:
            v[3] = (v[3] + data_cr) % 4096
        if self.fc_frozen:
            return
        need = False
        if not hinf and (v[0] - v[2]) % 256 < v[4] // 4:
            need = True
        if not dinf and (v[1] - v[3]) % 4096 < v[5] // 4:
            need = True
        if need:
            self.fc_update(d, typ)

    def fc_update(self, tx, typ, data_limit=None):
        v = self.fc[(tx, typ)]
        if v[4]:
            v[0] = (v[2] + v[4]) % 256
        if v[5]:
            v[1] = (v[3] + v[5]) % 4096 if data_limit is None else data_limit
        rx = self.DN if tx == self.UP else self.UP
        self.adv(40)
        return self.dllp(rx, f"UpdateFC-{typ}", **{"VC ID": 0, "HdrScale": 0, "HdrFC": v[0], "DataScale": 0, "DataFC": v[1]})

    # ------------------------------------------------------------------------------------------- link bring-up
    def training(self):
        self.ev(self.DN, "Link Training Start (Detect)")
        for _ in range(24):
            self.adv(4)
            self.ts(self.DN, "TS1", 1, 1)
            self.adv(4)
            self.ts(self.UP, "TS1", 1, 1)
        self.ev(self.DN, "Polling.Configuration")
        self.width = 4
        for _ in range(12):
            self.adv(4)
            self.ts(self.DN, "TS2", 1, 4)
            self.adv(4)
            self.ts(self.UP, "TS2", 1, 4)
        self.ev(self.DN, "Link Width Change: x1 -> x4")
        self.ev(self.DN, "Link Up (L0)")
        self.fc_init()
        self.adv(200)
        self.dllp(self.DN, "Data_Link_Feature", **{"Feature Ack": 0, "Feature Support": "0x1"})
        self.dllp(self.UP, "Data_Link_Feature", **{"Feature Ack": 1, "Feature Support": "0x1"})
        for target in (3, 4):
            self.adv(2000)
            self.ev(self.DN, f"Recovery Entry (speed change to {RATE[target]})")
            for _ in range(8):
                self.adv(4)
                self.ts(self.DN, "TS1", target, sc=1)
                self.adv(4)
                self.ts(self.UP, "TS1", target, sc=1)
            self.adv(500)
            self.os(self.DN, "EIOS")
            self.ev(self.DN, "Electrical Idle Entry")
            self.adv(1500)
            self.ev(self.DN, "Electrical Idle Exit")
            prev = self.speed
            self.speed = target
            self.ev(self.DN, f"Speed Change: {RATE[prev]} -> {RATE[target]}")
            if target == 4:
                for ph in (1, 2, 3):
                    self.adv(400)
                    self.ev(self.DN, f"Recovery.Equalization Phase {ph}")
                    for _ in range(3):
                        self.adv(2)
                        self.ts(self.DN, "TS1", 4, eq=ph)
                        self.adv(2)
                        self.ts(self.UP, "TS1", 4, eq=ph)
            for _ in range(6):
                self.adv(2)
                self.ts(self.DN, "TS2", target)
                self.adv(2)
                self.ts(self.UP, "TS2", target)
            self.ev(self.DN, "Recovery Exit (L0)")

    # ------------------------------------------------------------------------------------------- host requests
    def next_tag(self, up=True):
        if up:
            self.tag_up = (self.tag_up + 1) % 128
            return self.tag_up
        self.tag_dn = (self.tag_dn + 1) % 32
        return self.tag_dn

    def cfg(self, rw, reg, wdata=None, rdata=None, status="SC"):
        tag = self.next_tag(False)
        self.adv(self.rnd.randint(400, 900))
        n, _ = self.tlp(self.DN, f"Cfg{rw}0", 1, hx(struct.pack("<I", wdata)) if wdata is not None else None,
                        **{"Requester ID": HOST, "Tag": tag, "Device ID": SSD, "Register": f"0x{reg:03X}", "First BE": "0xF", "Last BE": "0x0"})
        self.adv(self.rnd.randint(500, 1000))
        if rdata is not None and status == "SC":
            self.tlp(self.UP, "CplD", 1, hx(struct.pack("<I", rdata)), **{"Requester ID": HOST, "Tag": tag, "Completer ID": SSD,
                                                                          "Cpl Status": "SC", "Byte Cnt": 4, "BCM": 0, "Lwr Addr": "0x00"})
        else:
            self.tlp(self.UP, "Cpl", 0, **{"Requester ID": HOST, "Tag": tag, "Completer ID": SSD, "Cpl Status": status,
                                           "Byte Cnt": 4, "BCM": 0, "Lwr Addr": "0x00"})
        return n

    def mmio_wr(self, off, val_bytes):
        self.adv(self.rnd.randint(200, 600))
        ln = len(val_bytes) // 4
        n, _ = self.tlp(self.DN, "MWr32", ln, hx(val_bytes), **{"Requester ID": HOST, "Address": f"0x{BAR0 + off:016X}",
                                                               "First BE": "0xF", "Last BE": "0xF" if ln > 1 else "0x0"})
        return n

    def mmio_rd(self, off, val, status="SC"):
        tag = self.next_tag(False)
        self.adv(self.rnd.randint(200, 600))
        n, _ = self.tlp(self.DN, "MRd32", 1, **{"Requester ID": HOST, "Tag": tag, "Address": f"0x{BAR0 + off:016X}",
                                                "First BE": "0xF", "Last BE": "0x0"})
        self.adv(self.rnd.randint(500, 1000))
        if status == "SC":
            self.tlp(self.UP, "CplD", 1, hx(struct.pack("<I", val)), **{"Requester ID": HOST, "Tag": tag, "Completer ID": SSD,
                                                                        "Cpl Status": "SC", "Byte Cnt": 4, "BCM": 0,
                                                                        "Lwr Addr": f"0x{off & 0x7F:02X}"})
        else:
            self.tlp(self.UP, "Cpl", 0, **{"Requester ID": HOST, "Tag": tag, "Completer ID": SSD, "Cpl Status": status,
                                           "Byte Cnt": 4, "BCM": 0, "Lwr Addr": f"0x{off & 0x7F:02X}"})
        return n

    def doorbell(self, idx, val, fbe="0xF"):
        self.adv(self.rnd.randint(150, 500))
        n, _ = self.tlp(self.DN, "MWr32", 1, hx(struct.pack("<I", val)), **{"Requester ID": HOST, "Address": f"0x{DB + idx * 4:016X}",
                                                                           "First BE": fbe, "Last BE": "0x0"})
        return n

    def enumerate(self):
        self.cfg("Rd", 0x000, rdata=0xA80A144D)
        self.cfg("Wr", 0x010, wdata=0xFFFFFFFF)
        self.cfg("Rd", 0x010, rdata=0xFFFFC004)
        self.cfg("Wr", 0x010, wdata=BAR0 | 0x4)
        self.cfg("Wr", 0x014, wdata=0)
        self.cfg("Wr", 0x004, wdata=0x00000406)
        self.cfg("Rd", 0x034, rdata=0x40)
        self.cfg("Rd", 0x040, rdata=0x00028010)                       # PCIe cap, next 0x80
        self.cfg("Rd", 0x044, rdata=0x2 | (6 << 9) | (6 << 6))         # DevCap: MPS 512 supported, L1 acceptable 64 us
        self.cfg("Rd", 0x048, rdata=0x00002810)
        self.cfg("Wr", 0x048, wdata=0x00002130)                       # MPS 256, MRRS 512, ext tags, RO enable
        self.cfg("Rd", 0x04C, rdata=0x4 | (4 << 4) | (3 << 10) | (6 << 15))   # LinkCap: 16 GT/s x4, L1 exit < 64 us
        self.cfg("Rd", 0x050, rdata=0x00440042)
        self.cfg("Rd", 0x068, rdata=0x00000000)
        self.cfg("Rd", 0x080, rdata=0x001F0011)                       # MSI-X, 32 vectors
        self.cfg("Rd", 0x084, rdata=0x00002000)                       # table BIR0 + 0x2000
        self.cfg("Rd", 0x088, rdata=0x00003000)
        self.cfg("Wr", 0x080, wdata=0x801F0011)                       # MSI-X enable

    def controller_init(self):
        self.mmio_rd(0x00, 0x140103FF)       # CAP: MQES 1023, CQR, TO 10 s
        self.mmio_rd(0x04, 0x00000020)       # DSTRD 0, MPSMIN 4 KiB
        self.mmio_rd(0x08, 0x00010400)
        self.mmio_wr(0x14, struct.pack("<I", 0))
        self.mmio_rd(0x1C, 0)
        self.mmio_wr(0x24, struct.pack("<I", 31 | (31 << 16)))
        self.mmio_wr(0x28, struct.pack("<I", ASQ & 0xFFFFFFFF))
        self.mmio_wr(0x2C, struct.pack("<I", ASQ >> 32))
        self.mmio_wr(0x30, struct.pack("<I", ACQ & 0xFFFFFFFF))
        self.mmio_wr(0x34, struct.pack("<I", ACQ >> 32))
        for v in range(NQ + 1):
            self.mmio_wr(0x2000 + 16 * v, struct.pack("<IIII", MSI_ADDR | (v << 12), 0, 0x4020 + v, 0))
        self.mmio_wr(0x14, struct.pack("<I", 0x00460001))
        self.adv(20_000)
        self.mmio_rd(0x1C, 1)
        for q, size in ((0, 32),):
            self.sq_tail[q] = 0
            self.cq_head[q] = 0
            self.cq_tail[q] = 0
            self.phase[q] = 1
            self.cid[q] = 0
            self.sq_fetch[q] = 0
            self.qsize = {0: 32}

    # ------------------------------------------------------------------------------------------- NVMe engine
    def alloc(self, nbytes):
        pages = max(1, -(-nbytes // 4096))
        a = DATA_BASE + (self.page_ctr % 2_000_000) * 4096
        self.page_ctr += pages + 1
        return a

    def new_cid(self, q):
        self.cid[q] = (self.cid[q] + 1) & 0xFFFF
        return self.cid[q]

    def sq_base(self, q):
        return ASQ if q == 0 else IOSQ + q * 0x10000

    def cq_base(self, q):
        return ACQ if q == 0 else IOCQ + q * 0x10000

    def submit(self, q, entries, ring=True):
        """Host places SQEs (invisible) and rings the tail doorbell.  Returns (doorbell pkt, slots)."""
        slots = []
        for _ in entries:
            slots.append(self.sq_tail[q])
            self.sq_tail[q] = (self.sq_tail[q] + 1) % self.qsize[q]
        n = self.doorbell(2 * q, self.sq_tail[q]) if ring else None
        return n, slots

    def fetch(self, q, slots, entries, drop_cpl=False):
        """Device fetches consecutive SQEs (one MRd), host returns CplD(s).  Returns (mrd pkt, cpl pkt)."""
        self.adv(self.rnd.randint(250, 600))
        tag = self.next_tag()
        nb = 64 * len(entries)
        addr = self.sq_base(q) + slots[0] * 64
        n_rd, _ = self.tlp(self.UP, "MRd64", nb // 4, **{"Requester ID": SSD, "Tag": tag, "Address": f"0x{addr:016X}",
                                                        "First BE": "0xF", "Last BE": "0xF"})
        if drop_cpl:
            return n_rd, None
        data = b"".join(entries)
        n_cpl = self.read_cpl(tag, addr, data)
        self.sq_fetch[q] = (slots[-1] + 1) % self.qsize[q]
        return n_rd, n_cpl

    def read_cpl(self, tag, addr, data, split=256, bc_bug=False, ep=0, extra=0, first_split=None):
        rem = len(data)
        off = 0
        last = None
        k = 0
        while rem > 0:
            chunk = min(split, rem)
            if first_split and k == 0:
                chunk = first_split
            self.adv(self.rnd.randint(300, 700))
            bc = rem if not (bc_bug and k == 1) else rem + 64
            body = data[off:off + chunk]
            ln = chunk // 4
            payload = hx(body) + ((" " + hx(bytes(extra))) if extra and k == 0 else "")
            last, _ = self.tlp(self.DN, "CplD", ln, payload, ep=ep if k == 0 else 0,
                               **{"Requester ID": SSD, "Tag": tag, "Completer ID": HOST, "Cpl Status": "SC", "Byte Cnt": bc,
                                  "BCM": 0, "Lwr Addr": f"0x{(addr + off) & 0x7F:02X}"})
            if k == 0:
                self.first_cpl = last
                self.cpl_pkts = []
            self.cpl_pkts.append(last)
            off += chunk
            rem -= chunk
            k += 1
        return last

    def prp_pages(self, prp1, prp2, nbytes, list_entries):
        pages = [prp1]
        first = 4096 - prp1 % 4096
        if nbytes > first:
            if nbytes - first <= 4096:
                pages.append(prp2)
            else:
                pages += list_entries
        return pages

    def addr_at(self, pages, prp1, off):
        first = 4096 - prp1 % 4096
        if off < first:
            return prp1 + off
        off -= first
        return pages[1 + off // 4096] + off % 4096

    def dev_write_data(self, pages, prp1, nbytes, upto=None, start=0, chunk_plan=None, ep_at=None, mps_bug=False):
        """Device -> host data (read command)."""
        off = start
        end = nbytes if upto is None else upto
        n_first = None
        i = 0
        while off < end:
            ch = min(MPS * (2 if mps_bug and i == 0 else 1), end - off)
            if chunk_plan:
                ch = min(chunk_plan[i] if i < len(chunk_plan) else MPS, end - off)
            a = self.addr_at(pages, prp1, off)
            room = 4096 - a % 4096
            if ch > room and not chunk_plan:
                ch = room
            self.adv(self.rnd.randint(40, 110))
            n, _ = self.tlp(self.UP, "MWr64", ch // 4, self.payload(ch), ep=1 if ep_at == i else 0,
                            **{"Requester ID": SSD, "Address": f"0x{a:016X}", "First BE": "0xF", "Last BE": "0xF"})
            if n_first is None:
                n_first = n
                self.data_pkts = []
            self.data_pkts.append(n)
            off += ch
            i += 1
        return n_first, n

    def dev_read_data(self, pages, prp1, nbytes, mrrs=MRRS, bc_bug=False, rcb_bug=False, ep=False, malformed=False,
                      tag_reuse=False):
        """Host -> device data (write command): device MRd <= MRRS, host CplD split at 256."""
        reqs = []
        off = 0
        marks = {}
        while off < nbytes:
            ch = min(mrrs, nbytes - off)
            a = self.addr_at(pages, prp1, off)
            room = 4096 - a % 4096
            ch = min(ch, room)
            tag = self.next_tag() if not (tag_reuse and reqs) else reqs[0][0]
            self.adv(self.rnd.randint(50, 140))
            n, _ = self.tlp(self.UP, "MRd64", ch // 4, **{"Requester ID": SSD, "Tag": tag, "Address": f"0x{a:016X}",
                                                         "First BE": "0xF", "Last BE": "0xF"})
            if tag_reuse and reqs and "reuse" not in marks:
                marks["reuse"] = n
            reqs.append((tag, a, ch, n))
            off += ch
        for i, (tag, a, ch, n) in enumerate(reqs):
            data = bytes(self.rnd.randrange(256) for _ in range(min(ch, 64))) * (ch // 64 or 1)
            data = data[:ch] if len(data) >= ch else data + bytes(ch - len(data))
            if i == 0 and malformed:
                # Length says 16 DW (64 B) but 68 bytes of payload follow
                self.adv(400)
                p, _ = self.tlp(self.DN, "CplD", 16, hx(data[:64]) + " " + hx(bytes(4)),
                                **{"Requester ID": SSD, "Tag": tag, "Completer ID": HOST, "Cpl Status": "SC", "Byte Cnt": ch,
                                   "BCM": 0, "Lwr Addr": f"0x{a & 0x7F:02X}"})
                marks["malformed"] = p
                self.read_cpl(tag, a + 64, data[64:])
                continue
            last = self.read_cpl(tag, a, data, bc_bug=bc_bug and i == 0, ep=1 if (ep and i == 0) else 0,
                                 first_split=96 if (rcb_bug and i == 0) else None)
            if i == 0:
                marks["first_cpl"] = self.cpl_pkts[0]
                if len(self.cpl_pkts) > 1:
                    marks["second_cpl"] = self.cpl_pkts[1]
        return reqs, marks

    def post_cqe(self, q, cid, status=0, dw0=0, sqid=None, phase_bug=False, sqhd=None):
        self.adv(self.rnd.randint(200, 500))
        slot = self.cq_tail[q]
        ph = self.phase[q] ^ (1 if phase_bug else 0)
        a = self.cq_base(q) + slot * 16
        n, _ = self.tlp(self.UP, "MWr64", 4, hx(cqe(dw0, self.sq_fetch[q] if sqhd is None else sqhd, q if sqid is None else sqid,
                                                      cid, ph, status)),
                        **{"Requester ID": SSD, "Address": f"0x{a:016X}", "First BE": "0xF", "Last BE": "0xF"})
        self.cq_tail[q] = (slot + 1) % self.qsize[q]
        if self.cq_tail[q] == 0:
            self.phase[q] ^= 1
        return n

    def msix(self, q):
        self.adv(self.rnd.randint(80, 250))
        n, _ = self.tlp(self.UP, "MWr32", 1, hx(struct.pack("<I", 0x4020 + q)),
                        **{"Requester ID": SSD, "Address": f"0x{MSI_ADDR | (q << 12):016X}", "First BE": "0xF", "Last BE": "0x0"})
        return n

    def cq_db(self, q, val=None):
        self.adv(self.rnd.randint(1200, 3000))
        if val is None:
            self.cq_head[q] = self.cq_tail[q]
            val = self.cq_head[q]
        return self.doorbell(2 * q + 1, val)

    # ------------------------------------------------------------------------------------------- one command
    def run(self, q, opc, *, nsid=1, nbytes=0, dirn=0, slba=0, status=0, dw0=0, cdw10=None, cdw11=0, cdw12=None,
            cdw13=0, fuse=0, psdt=0, prp1=None, prp2=None, delay=None, admin_data=None, cid=None, opts=None):
        """Full lifecycle of one command; returns dict of packet numbers."""
        o = opts or {}
        r = {}
        cid = self.new_cid(q) if cid is None else cid
        if prp1 is None:
            prp1 = (ADMIN_BUF + (self.ncmd % 64) * 0x2000) if q == 0 and nbytes else (self.alloc(nbytes) if nbytes else 0)
        list_entries = []
        first = 4096 - prp1 % 4096 if prp1 else 4096
        if prp2 is None:
            prp2 = 0
            if nbytes > first:
                if nbytes - first <= 4096:
                    prp2 = prp1 - prp1 % 4096 + 4096
                else:
                    nent = -(-(nbytes - first) // 4096)
                    list_entries = [prp1 - prp1 % 4096 + 4096 * (i + 1) for i in range(nent)]
                    prp2 = PRPL_BASE + (self.prpl_ctr % 4096) * 4096 + 64
                    self.prpl_ctr += 1
        nlb = max(1, nbytes // LBA)
        if cdw10 is None:
            cdw10 = slba & 0xFFFFFFFF if q else 0
        if cdw12 is None:
            cdw12 = (nlb - 1) if q and opc in (1, 2) else 0
        if q and opc in (1, 2) and cdw11 == 0:
            cdw11 = slba >> 32
        e = sqe(opc, cid, nsid, prp1, prp2, cdw10, cdw11, cdw12, cdw13, fuse=fuse, psdt=psdt)
        self.ncmd += 1
        r["cid"] = cid
        r["doorbell"], slots = self.submit(q, [e])
        if o.get("no_fetch"):
            return r
        r["fetch"], r["sqe_cpl"] = self.fetch(q, slots, [e], drop_cpl=o.get("drop_sqe_cpl", False))
        if o.get("drop_sqe_cpl"):
            return r
        pages = self.prp_pages(prp1, prp2, nbytes, list_entries) if nbytes else []
        transfer = status == 0 and nbytes and not o.get("no_data")
        if list_entries and transfer:
            self.adv(self.rnd.randint(200, 500))
            tag = self.next_tag()
            lb = len(list_entries) * 8
            r["prpl_rd"], _ = self.tlp(self.UP, "MRd64", lb // 4, **{"Requester ID": SSD, "Tag": tag, "Address": f"0x{prp2:016X}",
                                                                     "First BE": "0xF", "Last BE": "0xF"})
            self.read_cpl(tag, prp2, b"".join(struct.pack("<Q", x) for x in list_entries))
        self.adv(self.rnd.randint(3000, 9000) if transfer else self.rnd.randint(1000, 3000))
        if transfer:
            if dirn == 2:
                if admin_data is not None:
                    off = 0
                    while off < nbytes:
                        a = self.addr_at(pages, prp1, off)
                        self.adv(60)
                        self.tlp(self.UP, "MWr64", 64, hx(admin_data[off:off + 256]),
                                 **{"Requester ID": SSD, "Address": f"0x{a:016X}", "First BE": "0xF", "Last BE": "0xF"})
                        off += 256
                elif o.get("early_cqe"):
                    upto = (nbytes * 3 // 4)
                    self.dev_write_data(pages, prp1, nbytes, upto=upto)
                    r["cqe"] = self.post_cqe(q, cid, status, dw0)
                    r["late_data"], _ = self.dev_write_data(pages, prp1, nbytes, start=upto)
                    r["msix"] = self.msix(q)
                    r["cq_db"] = self.cq_db(q)
                    return r
                else:
                    r["first_data"], r["last_data"] = self.dev_write_data(pages, prp1, nbytes, chunk_plan=o.get("chunk_plan"),
                                                                          mps_bug=o.get("mps_bug", False))
            elif dirn == 1:
                reqs, marks = self.dev_read_data(pages, prp1, nbytes, mrrs=o.get("mrrs", MRRS), bc_bug=o.get("bc_bug", False),
                                                 rcb_bug=o.get("rcb_bug", False), ep=o.get("ep", False),
                                                 malformed=o.get("malformed", False), tag_reuse=o.get("tag_reuse", False))
                r["data_reqs"] = [x[3] for x in reqs]
                r.update(marks)
        self.adv(delay if delay is not None else self.rnd.randint(50_000, 100_000))
        if o.get("no_cqe"):
            return r
        if o.get("msix_first"):
            r["msix"] = self.msix(q)
            self.adv(1500)
            r["cqe"] = self.post_cqe(q, cid, status, dw0)
            r["cq_db"] = self.cq_db(q)
            return r
        r["cqe"] = self.post_cqe(q, cid, status, dw0, phase_bug=o.get("phase_bug", False))
        if o.get("dup_cqe"):
            self.adv(800)
            r["dup_cqe"] = self.post_cqe(q, cid, status, dw0)
        if o.get("no_msix"):
            self.adv(2_500_000)
            r["cq_db"] = self.cq_db(q)
            return r
        r["msix"] = self.msix(q)
        r["cq_db"] = self.cq_db(q)
        return r

    def identify_ctrl_data(self):
        b = bytearray(4096)
        struct.pack_into("<H", b, 0, 0x144D)
        b[4:24] = b"SYNTH0000000000001  "
        b[24:64] = b"SYNTHETIC NVMe SSD (pex_checker synth)  "
        b[64:72] = b"SYN1.0  "
        b[77] = MDTS
        b[258] = 3
        b[259] = 3
        struct.pack_into("<I", b, 516, 1)
        struct.pack_into("<H", b, 520, 0x0004)
        b[525] = 1
        struct.pack_into("<I", b, 536, 1)
        return bytes(b)

    def identify_ns_data(self):
        b = bytearray(4096)
        struct.pack_into("<QQQ", b, 0, 1 << 30, 1 << 30, 1 << 28)
        b[26] = 0
        b[33] = 1
        b[104:120] = bytes(range(1, 17))
        struct.pack_into("<HBB", b, 128, 0, 9, 0)
        return bytes(b)

    def admin_setup(self):
        self.run(0, 0x06, nsid=0, nbytes=4096, dirn=2, cdw10=1, admin_data=self.identify_ctrl_data())
        self.run(0, 0x06, nsid=1, nbytes=4096, dirn=2, cdw10=0, admin_data=self.identify_ns_data())
        self.run(0, 0x09, nsid=0, cdw10=0x07, cdw11=(NQ - 1) | ((NQ - 1) << 16), dw0=(NQ - 1) | ((NQ - 1) << 16))
        for q in range(1, NQ + 1):
            self.run(0, 0x05, nsid=0, prp1=self.cq_base(q), cdw10=q | ((QSIZE - 1) << 16), cdw11=1 | 2 | (q << 16))
            self.run(0, 0x01, nsid=0, prp1=self.sq_base(q), cdw10=q | ((QSIZE - 1) << 16), cdw11=1 | (q << 16))
            self.qsize[q] = QSIZE
            self.sq_tail[q] = self.cq_head[q] = self.cq_tail[q] = self.sq_fetch[q] = 0
            self.phase[q] = 1
            self.cid[q] = q * 0x1000
        # an Asynchronous Event Request stays outstanding for the whole capture (normal behaviour)
        cid = self.new_cid(0)
        e = sqe(0x0C, cid)
        _, slots = self.submit(0, [e])
        self.fetch(0, slots, [e])

    # ------------------------------------------------------------------------------------------- traffic
    def normal_io(self, q=None):
        rnd = self.rnd
        q = q or rnd.randint(1, 4)
        kind = rnd.random()
        if kind < 0.08:
            # batch of 2-4 reads under one doorbell
            k = rnd.randint(2, 4)
            t = self.sq_tail[q]
            k = min(k, QSIZE - t, 64 - (t % 64))     # one MRd must not wrap the queue or cross a 4 KiB page
            if k < 2:
                self.run(q, 2, nbytes=4096, dirn=2, slba=rnd.randrange(1 << 27) * 8)
                return
            cmds = []
            for _ in range(k):
                cid = self.new_cid(q)
                nb = 4096
                p1 = self.alloc(nb)
                cmds.append((cid, p1, sqe(2, cid, 1, p1, 0, rnd.randrange(1 << 30), 0, 7)))
            _, slots = self.submit(q, [c[2] for c in cmds])
            self.fetch(q, slots, [c[2] for c in cmds])
            for cid, p1, _ in cmds:
                self.adv(rnd.randint(3000, 8000))
                self.dev_write_data([p1], p1, 4096)
                self.adv(rnd.randint(40_000, 80_000))
                self.post_cqe(q, cid)
                self.msix(q)
            self.cq_db(q)
            self.ncmd += k
            return
        if kind < 0.70:
            nb = rnd.choice([512, 4096, 4096, 4096, 8192, 16384, 65536, 131072])
            self.run(q, 2, nbytes=nb, dirn=2, slba=rnd.randrange(1 << 27) * 8)
        elif kind < 0.95:
            nb = rnd.choice([512, 4096, 4096, 8192, 16384, 32768])
            self.run(q, 1, nbytes=nb, dirn=1, slba=rnd.randrange(1 << 27) * 8)
        elif kind < 0.98:
            self.run(q, 0, nsid=1, delay=rnd.randint(20_000, 60_000))
        else:
            self.dsm(q)
        if rnd.random() < 0.08:
            self.adv(16)
            self.os(rnd.choice([self.UP, self.DN]), "SKP")

    def dsm(self, q, n=2):
        rnd = self.rnd
        ranges = b"".join(struct.pack("<IIQ", 0, 8 * (i + 1), rnd.randrange(1 << 30)) for i in range(n))
        cid = self.new_cid(q)
        p1 = self.alloc(4096)
        e = sqe(0x09, cid, 1, p1, 0, n - 1, 1 << 2)
        r = {"cid": cid}
        r["doorbell"], slots = self.submit(q, [e])
        self.fetch(q, slots, [e])
        self.adv(rnd.randint(1000, 3000))
        tag = self.next_tag()
        r["range_rd"], _ = self.tlp(self.UP, "MRd64", len(ranges) // 4, **{"Requester ID": SSD, "Tag": tag, "Address": f"0x{p1:016X}",
                                                                           "First BE": "0xF", "Last BE": "0xF"})
        self.read_cpl(tag, p1, ranges)
        self.adv(rnd.randint(20_000, 60_000))
        r["cqe"] = self.post_cqe(q, cid)
        self.msix(q)
        self.cq_db(q)
        self.ncmd += 1
        return r

    def l1_episode(self, exit_ns):
        r = {}
        self.adv(5000)
        self.dllp(self.UP, "PM_Active_State_Request_L1")
        self.adv(300)
        self.dllp(self.DN, "PM_Request_Ack")
        self.adv(200)
        self.os(self.UP, "EIOS")
        self.os(self.DN, "EIOS")
        self.ev(self.DN, "Electrical Idle Entry")
        r["entry"] = self.ev(self.DN, "L1 Entry")
        self.adv(2_000_000)
        r["exit"] = self.ev(self.DN, "L1 Exit (CLKREQ# asserted)")
        self.ev(self.DN, "Recovery Entry")
        for _ in range(4):
            self.adv(4)
            self.ts(self.DN, "TS1")
            self.adv(4)
            self.ts(self.UP, "TS1")
        self.adv(exit_ns)
        for _ in range(4):
            self.adv(4)
            self.ts(self.DN, "TS2")
            self.adv(4)
            self.ts(self.UP, "TS2")
        r["l0"] = self.ev(self.DN, "Recovery Exit (L0)")
        return r

    # ------------------------------------------------------------------------------------------- faults
    def F(self, fid, card, pkt, desc, **extra):
        self.faults.append({"id": fid, "card": card, "pkt": pkt, "desc": desc, **extra})

    def faults_link(self):
        U, D = self.UP, self.DN
        rnd = self.rnd
        # X-15 bad LCRC -> Nak -> replay
        a = DATA_BASE + 0x7_0000_0000
        psns, datas, pk = [], [], []
        self.auto_ack = False
        for i in range(3):
            self.adv(80)
            d = self.payload(256)
            n, p = self.tlp(U, "MWr64", 64, d, bad=(i == 1), **{"Requester ID": SSD, "Address": f"0x{a + i * 256:016X}",
                                                               "First BE": "0xF", "Last BE": "0xF"})
            psns.append(p)
            datas.append(d)
            pk.append(n)
        self.adv(300)
        self.dllp(D, "Nak", **{"ACK/NAK Sequence Number": psns[0]})
        for i in (1, 2):
            self.adv(80)
            self.tlp(U, "MWr64", 64, datas[i], psn=psns[i], **{"Requester ID": SSD, "Address": f"0x{a + i * 256:016X}",
                                                               "First BE": "0xF", "Last BE": "0xF"})
        self.adv(200)
        self.dllp(D, "Ack", **{"ACK/NAK Sequence Number": psns[2]})
        self.auto_ack = True
        self.F("F-LCRC", "X-15", pk[1], "upstream MWr with bad LCRC, Nak, replay of 2 TLPs")
        self.normal_io()
        # T-35 replay timer expiry (no Ack, no Nak)
        self.auto_ack = False
        psns, datas = [], []
        for i in range(4):
            self.adv(70)
            d = self.payload(256)
            n, p = self.tlp(U, "MWr64", 64, d, **{"Requester ID": SSD, "Address": f"0x{a + 0x10000 + i * 256:016X}",
                                                 "First BE": "0xF", "Last BE": "0xF"})
            psns.append(p)
            datas.append(d)
        self.adv(4200)
        first = None
        for i, p in enumerate(psns):
            self.adv(70)
            n, _ = self.tlp(U, "MWr64", 64, datas[i], psn=p, **{"Requester ID": SSD, "Address": f"0x{a + 0x10000 + i * 256:016X}",
                                                               "First BE": "0xF", "Last BE": "0xF"})
            first = first or n
        self.adv(200)
        self.dllp(D, "Ack", **{"ACK/NAK Sequence Number": psns[-1]})
        self.auto_ack = True
        self.F("F-REPLAY-TIMER", "T-35", first, "4 TLPs re-sent with original PSNs after 4.2 us without Ack or Nak")
        self.normal_io()
        # X-19 REPLAY_NUM rollover: one PSN sent 4 times, then Recovery
        self.auto_ack = False
        d = self.payload(256)
        n0, p = self.tlp(U, "MWr64", 64, d, **{"Requester ID": SSD, "Address": f"0x{a + 0x20000:016X}", "First BE": "0xF", "Last BE": "0xF"})
        last = None
        for k in range(3):
            self.adv(2500)
            last, _ = self.tlp(U, "MWr64", 64, d, psn=p, **{"Requester ID": SSD, "Address": f"0x{a + 0x20000:016X}",
                                                           "First BE": "0xF", "Last BE": "0xF"})
        self.adv(500)
        self.ev(D, "Recovery Entry (REPLAY_NUM rollover)")
        for _ in range(4):
            self.adv(4)
            self.ts(D, "TS1")
            self.adv(4)
            self.ts(U, "TS1")
        for _ in range(4):
            self.adv(4)
            self.ts(D, "TS2")
            self.adv(4)
            self.ts(U, "TS2")
        self.ev(D, "Recovery Exit (L0)")
        self.adv(200)
        self.dllp(D, "Ack", **{"ACK/NAK Sequence Number": p})
        self.auto_ack = True
        self.F("F-REPLAY-NUM", "X-19", last, "same PSN transmitted 4 times without Ack, then Recovery")
        self.normal_io()
        # X-20 Ack latency: host Ack for a device TLP 15 us late
        self.auto_ack = False
        n, p = self.tlp(U, "MWr64", 64, self.payload(256), **{"Requester ID": SSD, "Address": f"0x{a + 0x30000:016X}",
                                                             "First BE": "0xF", "Last BE": "0xF"})
        self.adv(15_000)
        ack = self.dllp(D, "Ack", **{"ACK/NAK Sequence Number": p})
        self.auto_ack = True
        self.F("F-ACK-LATE", "X-20", ack, "host Ack 15 us after the TLP it covers")
        self.normal_io()
        # T-38 flow-control starvation
        v = self.fc[(U, "P")]
        self.fc_frozen = True
        self.fc_update(U, "P", data_limit=(v[3] + 8) % 4096)
        self.adv(70)
        n_stall, _ = self.tlp(U, "MWr64", 32, self.payload(128), **{"Requester ID": SSD, "Address": f"0x{a + 0x40000:016X}",
                                                                   "First BE": "0xF", "Last BE": "0xF"})
        self.adv(48_000)
        self.fc_frozen = False
        self.fc_update(U, "P")
        for i in range(3):
            self.adv(70)
            self.tlp(U, "MWr64", 64, self.payload(256), **{"Requester ID": SSD, "Address": f"0x{a + 0x40100 + i * 256:016X}",
                                                          "First BE": "0xF", "Last BE": "0xF"})
        self.F("F-FC-STALL", "T-38", n_stall, "posted credits exhausted, UpdateFC-P 48 us later")
        self.normal_io()
        # X-30 PSN gap without Nak (capture gap)
        self.psn[U] = (self.psn[U] + 3) & 0xFFF
        n, _ = self.tlp(U, "MWr64", 64, self.payload(256), **{"Requester ID": SSD, "Address": f"0x{a + 0x50000:016X}",
                                                             "First BE": "0xF", "Last BE": "0xF"})
        self.F("F-PSN-GAP", "X-30", n, "3 upstream PSNs missing from the capture, no Nak")
        self.normal_io()
        # V-34 ECRC
        n, _ = self.tlp(U, "MWr64", 64, self.payload(256), ecrc_bad=True,
                        **{"Requester ID": SSD, "Address": f"0x{a + 0x60000:016X}", "First BE": "0xF", "Last BE": "0xF"})
        self.F("F-ECRC", "V-34", n, "TLP digest (ECRC) mismatch")
        self.normal_io()

    def fault_l1_slow(self):
        r = self.l1_episode(150_000)
        self.F("F-L1-EXIT", "T-45", r["exit"], "L1 exit took ~150 us; DevCap L1 acceptable latency 64 us")
        self.normal_io()

    def fault_downshift(self):
        U, D = self.UP, self.DN
        self.adv(900)
        n_phys = self.ev(U, "Receiver Error (lane 2)")
        self.ev(U, "Framing Error")
        self.adv(200)
        self.ev(D, "Recovery Entry (receiver error)")
        for _ in range(6):
            self.adv(4)
            self.ts(D, "TS1", 3, sc=1)
            self.adv(4)
            self.ts(U, "TS1", 3, sc=1)
        self.adv(1200)
        self.speed = 3
        n_sp = self.ev(D, "Speed Change: 16.0 GT/s -> 8.0 GT/s")
        for _ in range(4):
            self.adv(3)
            self.ts(D, "TS2", 3)
            self.adv(3)
            self.ts(U, "TS2", 3)
        self.ev(D, "Recovery Exit (L0)")
        self.F("F-PHY", "L-01", n_phys, "receiver error on the upstream lanes")
        self.F("F-DOWNSHIFT", "X-09", n_sp, "Recovery -> speed downshift 16 -> 8 GT/s during I/O")

    def faults_tl(self):
        U, D = self.UP, self.DN
        # T-33 completion timeout on an SQE fetch (+ T-01 command never fetched)
        r = self.run(4, 2, nbytes=4096, dirn=2, slba=1234, opts={"drop_sqe_cpl": True})
        self.F("F-CPL-TIMEOUT", "T-33", r["fetch"], "SQE fetch MRd never completed by the host")
        self.F("F-NEVER-DONE", "T-01", r["doorbell"], "command whose SQE fetch was never answered never completes")
        self.adv(60_000_000)
        self.sq_fetch[4] = self.sq_tail[4]
        # L-20 unsupported request on an MMIO read
        n = self.mmio_rd(0x3F00 - 0x0, 0, status="UR")
        self.F("F-UR", "L-20", n, "MMIO read of an unimplemented BAR offset answered with UR")
        self.adv(2000)
        n_msg, _ = self.tlp(U, "Msg", 0, **{"Requester ID": SSD, "Msg Routing": "Route to RC", "Message Code": "ERR_NONFATAL"})
        self.F("F-ERRMSG", "X-26", n_msg, "ERR_NONFATAL message from the SSD")
        self.normal_io()
        # L-15 completer abort on a register read
        n = self.mmio_rd(0x0E00, 0, status="CA")
        self.F("F-CA", "L-15", n, "MMIO read answered with Completer Abort")
        self.normal_io()
        # L-18 malformed CplD (Length vs payload)
        r = self.run(1, 1, nbytes=4096, dirn=1, slba=777, opts={"malformed": True})
        self.F("F-MALFORMED", "L-18", r["malformed"], "CplD Length 16 DW with 68 payload bytes")
        self.normal_io()
        # V-26 MPS violation
        r = self.run(2, 2, nbytes=4096, dirn=2, slba=4242, opts={"mps_bug": True})
        self.F("F-MPS", "V-26", r["first_data"], "device MWr of 512 B with MPS 256")
        self.normal_io()
        # V-27 MRRS violation
        r = self.run(3, 1, nbytes=4096, dirn=1, slba=4343, opts={"mrrs": 1024})
        self.F("F-MRRS", "V-27", r["data_reqs"][0], "device MRd of 1024 B with MRRS 512")
        self.normal_io()
        # V-28 4 KiB crossing
        r = self.run(2, 2, nbytes=8192, dirn=2, slba=5151, opts={"chunk_plan": [128] + [256] * 40})
        self.F("F-4K", "V-28", self.data_pkts[16], "device MWr at page offset 0xF80 with 256 B crosses 4 KiB")
        self.normal_io()
        # V-30 byte count
        r = self.run(1, 1, nbytes=512, dirn=1, slba=6161, opts={"bc_bug": True})
        self.F("F-BYTECOUNT", "V-30", r["second_cpl"], "second partial CplD has Byte Count 64 too large")
        self.normal_io()
        # V-29 RCB
        r = self.run(1, 1, nbytes=512, dirn=1, slba=6262, opts={"rcb_bug": True})
        self.F("F-RCB", "V-29", r["first_cpl"], "first partial CplD ends at offset 96 (not 64-byte aligned)")
        self.normal_io()
        # V-32 tag reuse
        r = self.run(3, 1, nbytes=1024, dirn=1, slba=6363, opts={"tag_reuse": True})
        self.F("F-TAG-REUSE", "V-32", r["reuse"], "second MRd reuses an outstanding tag")
        self.normal_io()
        # V-31 unexpected completion
        self.adv(500)
        n, _ = self.tlp(D, "CplD", 16, self.payload(64), **{"Requester ID": SSD, "Tag": 200, "Completer ID": HOST, "Cpl Status": "SC",
                                                           "Byte Cnt": 64, "BCM": 0, "Lwr Addr": "0x00"})
        self.F("F-UNEXPECTED-CPL", "V-31", n, "CplD for a tag nobody is waiting for")
        self.normal_io()
        # L-12 poisoned write data accepted (L-38 at the CQE)
        r = self.run(2, 1, nbytes=4096, dirn=1, slba=6464, opts={"ep": True})
        self.F("F-POISON", "L-12", r["first_cpl"], "host CplD with EP=1 carrying write data")
        self.F("F-POISON-ACCEPTED", "L-38", r["cqe"], "write with poisoned data completed with Success")
        self.normal_io()

    def faults_nvme(self):
        rnd = self.rnd
        # status codes
        r = self.run(1, 2, nbytes=4096, dirn=2, slba=(1 << 40), status=0x080 | (1 << 14))
        self.F("F-LBA-RANGE", "S-25", r["cqe"], "Read beyond the namespace -> LBA Out of Range")
        r = self.run(2, 2, nbytes=4096, dirn=2, slba=999, status=0x281 | (1 << 14))
        self.F("F-READ-ERROR", "S-60", r["cqe"], "Unrecovered Read Error")
        self.normal_io()
        # V-01 / V-02 / V-03 / V-04 doorbells
        n = self.doorbell(2 * 1, QSIZE + 5)
        self.F("F-DB-SQ-RANGE", "V-01", n, "SQ1 tail doorbell beyond queue size")
        n = self.doorbell(2 * 1 + 1, QSIZE + 9)
        self.F("F-DB-CQ-RANGE", "V-02", n, "CQ1 head doorbell beyond queue size")
        n = self.doorbell(2 * 12, 1)
        self.F("F-DB-NOQ", "V-03", n, "doorbell for queue 12, never created")
        n = self.doorbell(2 * 2, self.sq_tail[2], fbe="0x3")
        self.F("F-DB-PARTIAL", "V-04", n, "2-byte doorbell write (First BE 0x3)")
        self.normal_io()
        # V-05 SQ tail moved backwards
        q = 3
        e = sqe(2, self.new_cid(q), 1, self.alloc(4096), 0, 1, 0, 7)
        self.submit(q, [e])
        self.sq_tail[q] = (self.sq_tail[q] - 1) % QSIZE
        n = self.doorbell(2 * q, self.sq_tail[q])
        self.F("F-SQ-BACKWARDS", "V-05", n, "SQ3 tail doorbell moved backwards (host withdrew an entry)")
        self.normal_io(q=1)
        # V-08 CID reuse while outstanding (dedicated queue 7)
        q = 7
        cid = self.new_cid(q)
        p1 = self.alloc(4096)
        e1 = sqe(2, cid, 1, p1, 0, 11, 0, 7)
        _, s1 = self.submit(q, [e1])
        self.fetch(q, s1, [e1])
        p2 = self.alloc(4096)
        e2 = sqe(2, cid, 1, p2, 0, 22, 0, 7)
        _, s2 = self.submit(q, [e2])
        _, n_cpl = self.fetch(q, s2, [e2])
        self.F("F-CID-REUSE", "V-08", n_cpl, "second SQE on SQ7 with a CID that is still outstanding")
        self.adv(30_000)
        self.dev_write_data([p1], p1, 4096)
        self.post_cqe(q, cid)
        self.msix(q)
        self.cq_db(q)
        self.normal_io()
        # V-09 unknown CID / V-10 bad SQID
        n = self.post_cqe(1, 0xBEEF)
        self.msix(1)
        self.cq_db(1)
        self.F("F-CQE-UNKNOWN", "V-09", n, "CQE for a CID never fetched on SQ1")
        n = self.post_cqe(2, 0x0042, sqid=12)
        self.msix(2)
        self.cq_db(2)
        self.F("F-CQE-SQID", "V-10", n, "CQE names SQID 12 which does not exist")
        self.normal_io()
        # V-11 phase (dedicated queue 6)
        r = self.run(6, 2, nbytes=4096, dirn=2, slba=31, opts={"phase_bug": True})
        self.F("F-PHASE", "V-11", r["cqe"], "CQE with the wrong phase tag")
        self.normal_io()
        # V-13 CQE before data (+ V-16 DMA after completion)
        r = self.run(1, 2, nbytes=4096, dirn=2, slba=8080, opts={"early_cqe": True})
        self.F("F-CQE-BEFORE-DATA", "V-13", r["cqe"], "CQE posted after 3/4 of the read data")
        self.F("F-DMA-AFTER-CQE", "V-16", r["late_data"], "read data written after the CQE")
        self.normal_io()
        # V-14 MSI-X before CQE
        r = self.run(2, 2, nbytes=4096, dirn=2, slba=8181, opts={"msix_first": True})
        self.F("F-MSIX-FIRST", "V-14", r["msix"], "MSI-X sent 1.5 us before its CQE")
        self.normal_io()
        # V-15 CQE without MSI-X
        r = self.run(3, 2, nbytes=4096, dirn=2, slba=8282, opts={"no_msix": True})
        self.F("F-NO-MSIX", "V-15", r["cqe"], "CQE never followed by an MSI-X; host polled 2.5 ms later")
        self.normal_io()
        # command format
        p = self.alloc(4096) + 2
        r = self.run(1, 2, nbytes=4096, dirn=2, slba=1, prp1=p, status=0x13 | (1 << 14))
        self.F("F-PRP1", "V-17", r["sqe_cpl"], "PRP1 not dword aligned")
        p = self.alloc(8192)
        r = self.run(1, 2, nbytes=8192, dirn=2, slba=2, prp1=p, prp2=p + 4096 + 0x10, status=0x13 | (1 << 14))
        self.F("F-PRP2", "V-18", r["sqe_cpl"], "PRP2 data pointer with a page offset")
        r = self.run(2, 2, nbytes=256 * 1024, dirn=2, slba=3, status=0x02 | (1 << 14))
        self.F("F-MDTS", "V-20", r["sqe_cpl"], "256 KiB read with MDTS 128 KiB")
        r = self.run(2, 0, nsid=1, cdw10=0x5)
        self.F("F-RESERVED", "V-21", r["sqe_cpl"], "Flush with non-zero CDW10")
        r = self.run(3, 1, nbytes=4096, dirn=1, slba=4, fuse=2, status=0x0A)
        self.F("F-FUSE", "V-22", r["sqe_cpl"], "second-half fused Write without a first half")
        r = self.run(3, 0x06, nsid=0, cdw10=1, status=0x01 | (1 << 14))
        self.F("F-ADMIN-ON-IO", "V-23", r["sqe_cpl"], "Identify opcode submitted on I/O queue 3")
        r = self.run(0, 0x02, nsid=0xFFFFFFFF, nbytes=512, dirn=2, psdt=1, cdw10=0x02 | (127 << 16), status=0x02 | (1 << 14))
        self.F("F-SGL-ADMIN", "V-24", r["sqe_cpl"], "Get Log Page with PSDT=01b (SGL) on the admin queue")
        r = self.run(0, 0x01, nsid=0, prp1=IOSQ + 0x0F0000, cdw10=9 | (2047 << 16), cdw11=1 | (1 << 16), status=0x102 | (1 << 14))
        self.F("F-CREATE-QSIZE", "V-25", r["sqe_cpl"], "Create I/O SQ with 2048 entries > MQES+1 (1024)")
        self.normal_io()
        # D-29 duplicate CQE (dedicated queue 5)
        r = self.run(5, 2, nbytes=4096, dirn=2, slba=41, opts={"dup_cqe": True})
        self.F("F-DUP-CQE", "D-29", r["dup_cqe"], "same CQE posted twice")
        self.normal_io()
        # D-01 deallocate (partly visible, info)
        r = self.dsm(1, 3)
        self.F("F-DEALLOC", "D-01", r["cqe"], "Deallocate completed: needs read-back to prove", severity="info")
        # C-14 abort of a slow command
        q = 4
        cid = self.new_cid(q)
        p1 = self.alloc(4096)
        e = sqe(2, cid, 1, p1, 0, 51, 0, 7)
        _, s = self.submit(q, [e])
        self.fetch(q, s, [e])
        self.adv(2_000_000)
        r = self.run(0, 0x08, nsid=0, cdw10=q | (cid << 16), delay=20_000)
        self.F("F-ABORT", "C-14", r["sqe_cpl"], "host Abort for a command outstanding 2 ms")
        self.adv(5_000)
        n = self.post_cqe(q, cid, status=0x07)
        self.msix(q)
        self.cq_db(q)
        self.F("F-ABORTED", "S-07", n, "aborted command completes with Command Abort Requested")
        self.normal_io()

    def fault_slow(self):
        r = self.run(1, 2, nbytes=4096, dirn=2, slba=90909, delay=12_400_000)
        self.F("F-SLOW", "T-47", r["cqe"], "4 KiB read with 12.4 ms device time on a clean link")

    def fault_never_completed(self):
        r = self.run(8, 2, nbytes=4096, dirn=2, slba=70707, opts={"no_cqe": True})
        self.F("F-NO-CQE", "T-01", r["doorbell"], "read fetched and data transferred, CQE never posted")

    # ------------------------------------------------------------------------------------------- main
    def generate(self, rows=None, size_bytes=None, progress=False):
        self.training()
        self.enumerate()
        self.controller_init()
        self.admin_setup()
        sched = [] if self.clean else [(0.10, self.faults_link), (0.20, self.fault_l1_slow), (0.30, self.faults_tl),
                                       (0.42, self.faults_nvme), (0.28, self.fault_never_completed), (0.60, self.fault_slow),
                                       (0.80, self.fault_downshift)]
        clean_l1 = [(0.25, lambda: self.l1_episode(20_000)), (0.55, lambda: self.dsm(2))]
        fired = set()
        target_rows = rows or 150_000
        t0 = time.time()
        last = t0
        while True:
            frac = (self.w.size() / size_bytes) if size_bytes else (self.w.n / target_rows)
            if frac >= 1.0:
                break
            for at, fn in sched + (clean_l1 if self.clean else []):
                if frac >= at and fn not in fired:
                    fired.add(fn)
                    fn()
            self.normal_io()
            if progress and time.time() - last > 10:
                last = time.time()
                print(f"  {self.w.n:,} rows {self.w.size() / 1e9:.2f} GB {time.time() - t0:.0f}s", flush=True)
        self.w.close()
        return self.w.n


def generate(out_dir, rows=None, size_gb=None, seed=7, clean=False, progress=False, name=None):
    os.makedirs(out_dir, exist_ok=True)
    name = name or ("clean_trace.csv" if clean else "fault_trace.csv")
    path = os.path.join(out_dir, name)
    s = Synth(path, seed, clean)
    n = s.generate(rows, int(size_gb * 1e9) if size_gb else None, progress)
    meta = {"synthetic": True, "generator": "pex_checker/synth.py", "seed": seed, "mode": "clean" if clean else "faults",
            "rows": n, "bytes": os.path.getsize(path), "columns": len(COLUMNS), "faults": s.faults,
            "note": "SYNTHETIC trace. 'pkt' is the Packet column value of the packet the finding must point at."}
    with open(os.path.splitext(path)[0] + "_truth.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return path, meta


def main(argv=None):
    ap = argparse.ArgumentParser(description="SYNTHETIC pex->CSV export generator with injected faults")
    ap.add_argument("--out", required=True)
    ap.add_argument("--rows", type=int)
    ap.add_argument("--size-gb", type=float)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--name")
    a = ap.parse_args(argv)
    t = time.time()
    path, meta = generate(a.out, a.rows, a.size_gb, a.seed, a.clean, progress=True, name=a.name)
    print(f"wrote {path}: {meta['rows']:,} rows, {meta['bytes'] / 1e6:.1f} MB, {len(meta['faults'])} injected faults, "
          f"{time.time() - t:.0f}s (SYNTHETIC)")


if __name__ == "__main__":
    main()
