"""Header mapping and cell parsers for a Teledyne LeCroy "pex -> CSV" packet export.

The export of PCIe Protocol Analysis 13.28.b.15 has ~112-120 columns, one row per packet.  Header names are matched
case-insensitively against aliases, so small spelling differences in a real export are tolerated.  "Device ID"
appears twice in the real header (TLP Cfg target, then the SMBus UDID one); the first occurrence wins.

All parsers are written for speed (they run on tens of millions of cells) and never raise: they return -1 / None /
'' for cells they cannot interpret.
"""
from __future__ import annotations

import re

# canonical name -> aliases (normalised: lower case, single spaces)
COLUMN_ALIASES: dict[str, list[str]] = {
    "marker": ["marker"],
    "pkt": ["packet", "packet #", "packet number", "packet no", "packet no.", "pkt", "pkt #", "#", "index"],
    "link": ["link"],
    "dir": ["direction", "dir", "link direction"],
    "os_type": ["ordered set type", "ordered set", "os type", "ordered-set type"],
    "dllp_type": ["dllp type", "dll type", "dllp"],
    "tlp_type": ["tlp type", "tlp", "packet type"],
    "link_event": ["link event", "event", "link events"],
    "psn": ["psn", "sequence number", "seq num", "seq number", "tlp sequence number", "seq #", "sequence #"],
    "acknak": ["ack/nak sequence number", "ack/nak seq number", "acknak sequence number", "ack nak sequence number",
               "ack/nak seq", "ack/nak seq num", "acknak seq num", "ack_nak_seq_num"],
    "vcid": ["vc id", "vcid", "vc"],
    "hdrscale": ["hdrscale", "hdr scale", "header scale"],
    "hdrfc": ["hdrfc", "hdr fc", "header fc", "hdr credits"],
    "datascale": ["datascale", "data scale"],
    "datafc": ["datafc", "data fc", "data credits"],
    "tc": ["tc", "traffic class"],
    "td": ["td", "tlp digest"],
    "ep": ["ep", "poisoned", "error poisoned"],
    "attr": ["attributes", "attr", "attribute"],
    "at": ["at", "address type (at)"],
    "length": ["length", "len", "length (dw)", "length(dw)", "payload length"],
    "req": ["requester id", "requester", "req id", "requestor id"],
    "tag": ["tag"],
    "cpl": ["completer id", "completer"],
    "addr": ["address", "addr", "memory address"],
    "devid": ["device id", "bdf", "target id"],
    "register": ["register", "reg", "register number", "ext register"],
    "first_be": ["first be", "1st be", "first dw be", "first byte enable"],
    "last_be": ["last be", "last dw be", "last byte enable"],
    "status": ["cpl status", "completion status", "cpl stat", "status", "compl status"],
    "bcount": ["byte cnt", "byte count (tlp)", "bytecount", "byte cnt.", "byte_cnt", "byte count tlp"],
    "bcm": ["bcm"],
    "lwr_addr": ["lwr addr", "lower address", "lower addr", "lwr address"],
    "msg_routing": ["msg routing", "message routing", "routing"],
    "msg_code": ["message code", "msg code", "message"],
    "ts_link": ["ts link"],
    "ts_lane": ["ts lane", "lane"],
    "n_fts": ["n_fts", "n fts"],
    "train_ctl": ["training_control", "training control"],
    "ts_rate": ["ts_data rate", "ts data rate", "data rate", "ts_datarate", "ts rate"],
    "ts_eq": ["ts gen3 eq control", "eq control"],
    "snoop_val": ["snoop value"],
    "snoop_scale": ["snoop scale"],
    "nosnoop_val": ["no snoop value"],
    "nosnoop_scale": ["no snoop scale"],
    "lcrc": ["lcrc"],
    "ecrc": ["ecrc"],
    "crc16": ["crc16", "crc 16", "dllp crc"],
    "data": ["data", "payload", "data payload", "tlp data"],
    "delta": ["time delta", "delta time", "delta"],
    "time": ["time stamp", "timestamp", "time", "time (s)", "time stamp (s)"],
}
CANONICAL = list(COLUMN_ALIASES)

# Which analysis features need which columns (missing columns disable the feature, with a note in summary.json).
FEATURE_COLUMNS = {
    "core (packet classification and timing)": ["dir", "time"],
    "TLP transaction layer": ["tlp_type", "length", "req", "tag", "addr"],
    "split completions (Byte Cnt / Lower Address)": ["bcount", "lwr_addr"],
    "NVMe decode (SQE / CQE / doorbells / config values)": ["data", "addr", "register"],
    "data link layer (replay, sequence)": ["psn"],
    "Ack/Nak latency and sequence": ["dllp_type", "acknak"],
    "flow-control credits": ["dllp_type", "hdrfc", "datafc"],
    "LCRC / ECRC / DLLP CRC": ["lcrc", "ecrc", "crc16"],
    "poisoned TLP": ["ep"],
    "link training / events": ["link_event", "os_type"],
    "TS1/TS2 rate and lanes": ["ts_rate", "ts_lane"],
    "error / PM messages": ["msg_code"],
}


def norm_header(s: str) -> str:
    s = str(s).replace("﻿", "").strip().strip('"').strip()
    return re.sub(r"\s+", " ", s.lower())


def detect_mapping(header: list[str]) -> dict[str, int]:
    """canonical name -> column index (first occurrence of a header wins)."""
    normed: dict[str, int] = {}
    for i, h in enumerate(header):
        normed.setdefault(norm_header(h), i)
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for canon, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            i = normed.get(a)
            if i is not None and i not in used:
                mapping[canon] = i
                used.add(i)
                break
    return mapping


def header_score(fields: list[str]) -> int:
    m = detect_mapping(fields)
    return len(m) + (5 if ("dir" in m and ("time" in m or "delta" in m)) else 0)


# ---------------------------------------------------------------------------------------------------- cell parsers
_HEX_CLEAN = re.compile(r"[\s_:]")


def hexint(s: str) -> int:
    """'0x00000000F0001008', 'F0001008', '0xF000_1008', 'F000:1008', '1008h' -> int; '' / junk -> -1."""
    if not s:
        return -1
    try:
        return int(s, 16)
    except ValueError:
        pass
    t = _HEX_CLEAN.sub("", s.strip())
    if t.lower().endswith("h"):
        t = t[:-1]
    i = t.find("(")
    if i > 0:
        t = t[:i]
    try:
        return int(t, 16)
    except ValueError:
        return -1


def autoint(s: str) -> int:
    """Decimal unless it carries 0x / hex letters (used for Tag, Length, PSN, Byte Cnt, credits)."""
    if not s:
        return -1
    try:
        return int(s)
    except ValueError:
        pass
    t = s.strip().replace(",", "").replace("_", "").replace(" ", "")
    i = t.find("(")
    if i > 0:
        t = t[:i]
    try:
        if t[:2].lower() == "0x":
            return int(t, 16)
        if t.lower().endswith("h"):
            return int(t[:-1], 16)
        return int(t)
    except ValueError:
        try:
            return int(t, 16)
        except ValueError:
            return -1


_UNIT = (("ns", 1e-9), ("us", 1e-6), ("µs", 1e-6), ("μs", 1e-6), ("ms", 1e-3), ("ps", 1e-12), ("s", 1.0))


def parse_time(s: str, default_scale: float = 1.0) -> float:
    """'12.345678901', '12.345 678 901 s', '1,234,567 ns', '12,345.678 us', '0.000 012 345' -> seconds (NaN if junk)."""
    try:
        return float(s) * default_scale
    except ValueError:
        pass
    t = s.strip().lower()
    if not t:
        return float("nan")
    scale = default_scale
    for u, k in _UNIT:
        if t.endswith(u):
            t = t[: -len(u)]
            scale = k
            break
    t = t.replace(" ", "").replace(" ", "").replace("_", "").replace("'", "")
    if t.count(",") and t.count("."):
        t = t.replace(",", "")
    elif t.count(",") > 1:
        t = t.replace(",", "")
    elif t.count(",") == 1:
        a, b = t.split(",")
        t = a + b if len(b) == 3 and scale != 1.0 else a + "." + b
    try:
        return float(t) * scale
    except ValueError:
        return float("nan")


def direction(s: str) -> str:
    """'Upstream'/'Up'/'U'/'D->H'/'Device to Host' -> 'U'; 'Downstream'/'Down'/'Dn'/'H->D' -> 'D'; else ''."""
    t = s.strip().lower()
    if not t:
        return ""
    if t.startswith("up") or t in ("u", "us", "ups", "d->h", "d2h", "dev->host", "device to host", "rx"):
        return "U"
    if t.startswith("down") or t.startswith("dn") or t in ("d", "ds", "h->d", "h2d", "host->dev", "host to device", "tx"):
        return "D"
    if "up" in t:
        return "U"
    if "down" in t:
        return "D"
    return ""


_HEX_TOKEN = re.compile(r"\b(?:0x[0-9a-f]+|[0-9a-f]*\d[0-9a-f]*)\b")
_BAD_WORD = re.compile(r"bad|err|invalid|fail|mismatch|incorrect|wrong")


def is_bad(cell: str) -> bool:
    """'0x1234ABCD (Bad)', 'Bad', 'Error', 'CRC mismatch' -> True.  Pure hex values such as 0x6446BADA -> False."""
    if not cell:
        return False
    if " " not in cell and "(" not in cell:
        t = cell.lower()
        if t.startswith("0x") or (all(ch in "0123456789abcdef" for ch in t) and any(ch.isdigit() for ch in t)):
            return False
        return bool(_BAD_WORD.search(t))
    t = _HEX_TOKEN.sub(" ", cell.lower())
    return bool(_BAD_WORD.search(t))


def truthy(cell: str) -> bool:
    if not cell:
        return False
    t = cell.strip().lower()
    return t not in ("0", "no", "false", "-", "n", "0x0", "off", "")


_BYTE_TOKENS = re.compile(r"[0-9A-Fa-f]+")


def data_bytes(s: str, dword_order: bool = False) -> tuple[bytes, bool]:
    """Payload cell -> (bytes, truncated).

    Accepted: '07 00 00 00', '07000000', '0x07 0x00', '00000006 00000000' (8-digit groups).  With dword_order=True each
    8-digit group is a DW value printed MSB first and is byte-swapped to wire (address) order."""
    if not s:
        return b"", False
    trunc = False
    if "..." in s or "…" in s:
        trunc = True
        s = s.replace("…", "").replace("...", "")
    if "0x" in s or "0X" in s:
        s = s.replace("0x", " ").replace("0X", " ")
    if dword_order:
        toks = _BYTE_TOKENS.findall(s)
        out = bytearray()
        for t in toks:
            if len(t) == 8:
                out += bytes.fromhex(t)[::-1]
            else:
                if len(t) % 2:
                    t = "0" + t
                out += bytes.fromhex(t)
        return bytes(out), trunc
    try:
        return bytes.fromhex(s), trunc
    except ValueError:
        toks = _BYTE_TOKENS.findall(s)
        try:
            return bytes.fromhex("".join(t if len(t) % 2 == 0 else "0" + t for t in toks)), trunc
        except ValueError:
            return b"", True


def payload_len(s: str) -> int:
    """Number of payload bytes shown in a Data cell without decoding it (-1 if truncated/unknown)."""
    if not s:
        return 0
    if "..." in s or "…" in s:
        return -1
    if "0x" in s:
        s = s.replace("0x", "")
    n = len(s) - s.count(" ") - s.count(",") - s.count("-")
    return n // 2


# ---------------------------------------------------------------------------------------------------- type classifiers
# TLP kinds
MRD, MWR, CPL, CPLD, CFGRD, CFGWR, IORD, IOWR, MSG, MSGD, ATOMIC, OTHER = range(12)
TLP_NAMES = ["MRd", "MWr", "Cpl", "CplD", "CfgRd", "CfgWr", "IORd", "IOWr", "Msg", "MsgD", "AtomicOp", "TLP"]
NON_POSTED = {MRD, CFGRD, CFGWR, IORD, IOWR, ATOMIC}
POSTED = {MWR, MSG, MSGD}
COMPLETIONS = {CPL, CPLD}
WITH_DATA = {MWR, CPLD, MSGD, CFGWR, IOWR, ATOMIC}


def classify_tlp(s: str) -> int:
    t = s.strip().lower().replace(" ", "").replace("_", "")
    if not t:
        return OTHER
    if t.startswith("mrd") or t.startswith("memoryread") or t.startswith("memrd") or t.startswith("mrdlk"):
        return MRD
    if t.startswith("mwr") or t.startswith("memorywrite") or t.startswith("memwr"):
        return MWR
    if t.startswith("cpld") or t.startswith("completionwithdata") or t.startswith("cpldlk"):
        return CPLD
    if t.startswith("cpl") or t.startswith("completion"):
        return CPL
    if t.startswith("cfgrd") or t.startswith("configread") or t.startswith("cfgread"):
        return CFGRD
    if t.startswith("cfgwr") or t.startswith("configwrite") or t.startswith("cfgwrite"):
        return CFGWR
    if t.startswith("iord") or t.startswith("ioread"):
        return IORD
    if t.startswith("iowr") or t.startswith("iowrite"):
        return IOWR
    if t.startswith("msgd") or t.startswith("messagewithdata"):
        return MSGD
    if t.startswith("msg") or t.startswith("message") or t.startswith("ltr") or t.startswith("pm_") or t.startswith("err"):
        return MSG
    if t.startswith("fetchadd") or t.startswith("swap") or t.startswith("cas") or "atomic" in t:
        return ATOMIC
    return OTHER


# DLLP kinds
D_ACK, D_NAK, D_FC, D_PM, D_OTHER = range(5)


def classify_dllp(s: str):
    """-> (kind, fc_type ('P'|'NP'|'CPL'|''), is_init, pm_name)"""
    t = s.strip().lower()
    if t in ("ack", "ack dllp"):
        return D_ACK, "", False, ""
    if t in ("nak", "nack", "nak dllp"):
        return D_NAK, "", False, ""
    if "fc" in t and ("update" in t or "init" in t or "upd" in t):
        toks = re.split(r"[^a-z0-9]+", t)
        typ = ""
        for tok in reversed(toks):
            if tok in ("p", "np", "cpl"):
                typ = tok.upper()
                break
        if not typ:
            if t.endswith("np"):
                typ = "NP"
            elif t.endswith("cpl"):
                typ = "CPL"
            elif t.endswith("p"):
                typ = "P"
        return D_FC, typ, "init" in t, ""
    if t.startswith("pm") or "l1" in t or "l23" in t:
        return D_PM, "", False, s.strip()
    return D_OTHER, "", False, s.strip()


def cpl_status(s: str) -> str:
    t = s.strip().upper()
    if not t or t in ("SC", "SUCCESS", "SUCCESSFUL COMPLETION", "0", "000", "0X0", "SUCCESSFUL", "OK"):
        return "SC"
    if t.startswith("UR") or "UNSUPPORTED" in t or t in ("1", "001", "0X1"):
        return "UR"
    if t.startswith("CA") or "ABORT" in t or t in ("4", "100", "0X4"):
        return "CA"
    if t.startswith("CRS") or t.startswith("RRS") or "RETRY" in t or t in ("2", "010", "0X2"):
        return "CRS"
    return "?" + t[:12]


MSG_CODES = {0x30: "ERR_COR", 0x31: "ERR_NONFATAL", 0x33: "ERR_FATAL", 0x10: "LTR", 0x14: "PM_Active_State_Nak",
             0x18: "PM_PME", 0x19: "PME_Turn_Off", 0x1B: "PME_TO_Ack", 0x12: "OBFF", 0x7E: "Vendor_Defined_Type0",
             0x7F: "Vendor_Defined_Type1", 0x50: "Set_Slot_Power_Limit", 0x00: "Unlock", 0x40: "Ignored"}


def msg_name(s: str) -> str:
    if not s:
        return ""
    t = s.strip()
    u = t.upper().replace(" ", "_")
    for k in ("ERR_COR", "ERR_NONFATAL", "ERR_FATAL", "LTR", "PM_PME", "PME_TURN_OFF", "PME_TO_ACK", "PM_ACTIVE_STATE_NAK",
              "OBFF", "VENDOR"):
        if k in u:
            return {"PME_TURN_OFF": "PME_Turn_Off", "PME_TO_ACK": "PME_TO_Ack", "PM_ACTIVE_STATE_NAK": "PM_Active_State_Nak",
                    "VENDOR": "Vendor_Defined"}.get(k, k)
    if "CORRECTABLE" in u and "UN" not in u:
        return "ERR_COR"
    if "NONFATAL" in u or "NON-FATAL" in u:
        return "ERR_NONFATAL"
    if "FATAL" in u:
        return "ERR_FATAL"
    v = hexint(t)
    if v >= 0:
        return MSG_CODES.get(v, f"0x{v:02X}")
    return t


def be_bytes(first_be: int, last_be: int, length_dw: int) -> int:
    """Bytes actually requested by a memory request given its byte enables (PCIe byte-count rules)."""
    if length_dw <= 0:
        return 0
    if first_be < 0:
        first_be = 0xF
    if last_be < 0:
        last_be = 0xF if length_dw > 1 else 0
    if length_dw == 1:
        if first_be == 0:
            return 1
        lo = (first_be & -first_be).bit_length() - 1
        hi = first_be.bit_length() - 1
        return hi - lo + 1
    lead = (first_be & -first_be).bit_length() - 1 if first_be else 4
    trail = 4 - last_be.bit_length() if last_be else 4
    return length_dw * 4 - lead - trail


def be_offset(first_be: int) -> int:
    if first_be <= 0:
        return 0
    return (first_be & -first_be).bit_length() - 1


def parse_rate(s: str) -> float:
    """'16.0 GT/s', 'Gen4', '16G', '2.5/5.0/8.0' -> GT/s (max listed), 0 if unknown."""
    if not s:
        return 0.0
    t = s.lower()
    vals = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*(?:gt|g\b|gt/s)", t)]
    if not vals:
        m = re.findall(r"gen\s*(\d)", t)
        if m:
            gen_rate = {1: 2.5, 2: 5.0, 3: 8.0, 4: 16.0, 5: 32.0, 6: 64.0, 7: 128.0}
            vals = [gen_rate.get(int(g), 0.0) for g in m]
    if not vals:
        vals = [float(x) for x in re.findall(r"\b(2\.5|5\.0|5|8\.0|8|16\.0|16|32\.0|32|64\.0|64)\b", t)]
    return max(vals) if vals else 0.0


def parse_lanes(s: str) -> int:
    """'0-3' -> 4, '0' -> 1, 'x8' -> 8, '0,1,2,3' -> 4."""
    if not s:
        return 0
    t = s.strip().lower()
    m = re.match(r"^x?(\d+)$", t)
    if m and t.startswith("x"):
        return int(m.group(1))
    m = re.match(r"^(\d+)\s*[-:]\s*(\d+)$", t)
    if m:
        return abs(int(m.group(2)) - int(m.group(1))) + 1
    if "," in t:
        return len([x for x in t.split(",") if x.strip()])
    if t.isdigit():
        return 1
    return 0


GEN_OF_RATE = {2.5: 1, 5.0: 2, 8.0: 3, 16.0: 4, 32.0: 5, 64.0: 6, 128.0: 7}
RATE_OF_SPEED_CODE = {1: 2.5, 2: 5.0, 3: 8.0, 4: 16.0, 5: 32.0, 6: 64.0, 7: 128.0}
