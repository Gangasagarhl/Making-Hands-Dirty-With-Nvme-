"""NVMe definitions used by the decoder: opcode tables, the status-code table mapped to atlas S-cards, SQE / CQE
decoding, data-length rules, Identify / log-page field offsets.

Status codes and names follow the atlas chapter II.5 ("Complete Status Code Table", Linux v7.2 include/linux/nvme.h
layout: SCT in bits 10:8, SC in bits 7:0).  Field offsets follow the NVMe Base / NVM Command Set layouts as used by
Linux and QEMU; where the atlas marks something version-dependent the README says so.
"""
from __future__ import annotations

import struct

ADMIN_OPC = {
    0x00: "Delete I/O SQ", 0x01: "Create I/O SQ", 0x02: "Get Log Page", 0x04: "Delete I/O CQ", 0x05: "Create I/O CQ",
    0x06: "Identify", 0x08: "Abort", 0x09: "Set Features", 0x0A: "Get Features", 0x0C: "Asynchronous Event Request",
    0x0D: "Namespace Management", 0x10: "Firmware Commit", 0x11: "Firmware Image Download", 0x14: "Device Self-test",
    0x15: "Namespace Attachment", 0x18: "Keep Alive", 0x19: "Directive Send", 0x1A: "Directive Receive",
    0x1C: "Virtualization Management", 0x1D: "NVMe-MI Send", 0x1E: "NVMe-MI Receive", 0x20: "Capacity Management",
    0x24: "Lockdown", 0x7C: "Doorbell Buffer Config", 0x7F: "Fabrics", 0x80: "Format NVM", 0x81: "Security Send",
    0x82: "Security Receive", 0x84: "Sanitize", 0x86: "Get LBA Status",
}
NVM_OPC = {
    0x00: "Flush", 0x01: "Write", 0x02: "Read", 0x04: "Write Uncorrectable", 0x05: "Compare", 0x08: "Write Zeroes",
    0x09: "Dataset Management", 0x0C: "Verify", 0x0D: "Reservation Register", 0x0E: "Reservation Report",
    0x11: "Reservation Acquire", 0x15: "Reservation Release", 0x18: "Cancel", 0x19: "Copy",
    0x79: "Zone Management Send", 0x7A: "Zone Management Receive", 0x7D: "Zone Append",
}
# admin opcodes that only make sense on the admin queue (used for the V-23 heuristic)
ADMIN_ONLY_LOOKING = {0x06, 0x0A, 0x0C, 0x10, 0x11, 0x14, 0x18, 0x80, 0x84}
DESTRUCTIVE_IO = {0x01, 0x04, 0x08, 0x09, 0x19, 0x79, 0x7D}
DESTRUCTIVE_ADMIN = {0x80, 0x84, 0x10, 0x11, 0x0D, 0x15, 0x00, 0x04}
# commands that legitimately stay outstanding for a long time (excluded from timeout rules)
LONG_LIVED_ADMIN = {0x0C}      # Asynchronous Event Request


def opcode_name(qid: int, opc: int) -> str:
    return (ADMIN_OPC if qid == 0 else NVM_OPC).get(opc, f"opcode 0x{opc:02X}")


# ------------------------------------------------------------------------------------------------- status -> card
# (sct, sc) -> (name, card_id).  Unknown codes fall back to S-70 for SCT 4..7 and to None otherwise.
_STATUS: dict[tuple[int, int], tuple[str, str]] = {}


def _s(sct, sc, name, card):
    _STATUS[(sct, sc)] = (name, card)


for _sc, _n, _c in [
    (0x01, "Invalid Command Opcode", "S-01"), (0x02, "Invalid Field in Command", "S-02"), (0x03, "Command ID Conflict", "S-03"),
    (0x04, "Data Transfer Error", "S-04"), (0x05, "Commands Aborted due to Power Loss Notification", "S-05"),
    (0x06, "Internal Error", "S-06"), (0x07, "Command Abort Requested", "S-07"), (0x08, "Command Aborted due to SQ Deletion", "S-08"),
    (0x09, "Command Aborted due to Failed Fused Command", "S-09"), (0x0A, "Command Aborted due to Missing Fused Command", "S-09"),
    (0x0B, "Invalid Namespace or Format", "S-10"), (0x0C, "Command Sequence Error", "S-11"),
    (0x0D, "Invalid SGL Segment Descriptor", "S-12"), (0x0E, "Invalid Number of SGL Descriptors", "S-12"),
    (0x0F, "Data SGL Length Invalid", "S-12"), (0x10, "Metadata SGL Length Invalid", "S-12"), (0x11, "SGL Descriptor Type Invalid", "S-12"),
    (0x12, "Invalid Use of Controller Memory Buffer", "S-13"), (0x13, "PRP Offset Invalid", "S-12"),
    (0x14, "Atomic Write Unit Exceeded", "S-14"), (0x15, "Operation Denied", "S-15"), (0x16, "SGL Offset Invalid", "S-12"),
    (0x17, "Reserved", "S-70"), (0x18, "Host Identifier Inconsistent Format", "S-16"), (0x19, "Keep Alive Timeout Expired", "S-16"),
    (0x1A, "Keep Alive Timeout Invalid", "S-16"), (0x1B, "Command Aborted due to Preempt and Abort", "S-17"),
    (0x1C, "Sanitize Failed", "S-18"), (0x1D, "Sanitize In Progress", "S-19"), (0x1E, "SGL Data Block Granularity Invalid", "S-12"),
    (0x1F, "Command Not Supported for Queue in CMB", "S-13"), (0x20, "Namespace is Write Protected", "S-20"),
    (0x21, "Command Interrupted", "S-21"), (0x22, "Transient Transport Error", "S-22"),
    (0x23, "Command Prohibited by Command and Feature Lockdown", "S-15"),
    (0x24, "Admin Command Media Not Ready", "S-23"), (0x29, "FDP Disabled / I/O Command Set group (0x29)", "S-24"),
    (0x2A, "I/O Command Set group (0x2A)", "S-24"), (0x2B, "I/O Command Set group (0x2B)", "S-24"),
    (0x2C, "Invalid IO Command Set", "S-24"), (0x80, "LBA Out of Range", "S-25"), (0x81, "Capacity Exceeded", "S-26"),
    (0x82, "Namespace Not Ready", "S-27"), (0x83, "Reservation Conflict", "S-28"), (0x84, "Format In Progress", "S-29"),
]:
    _s(0, _sc, _n, _c)

for _sc, _n, _c in [
    (0x00, "Completion Queue Invalid", "S-30"), (0x01, "Invalid Queue Identifier", "S-31"), (0x02, "Invalid Queue Size", "S-32"),
    (0x03, "Abort Command Limit Exceeded", "S-33"), (0x04, "Abort Command Is Missing (reserved)", "S-33"),
    (0x05, "Asynchronous Event Request Limit Exceeded", "S-34"), (0x06, "Invalid Firmware Slot", "S-35"),
    (0x07, "Invalid Firmware Image", "S-36"), (0x08, "Invalid Interrupt Vector", "S-37"), (0x09, "Invalid Log Page", "S-38"),
    (0x0A, "Invalid Format", "S-39"), (0x0B, "Firmware Activation Requires Conventional Reset", "S-40"),
    (0x0C, "Invalid Queue Deletion", "S-42"), (0x0D, "Feature Identifier Not Saveable", "S-43"),
    (0x0E, "Feature Not Changeable", "S-43"), (0x0F, "Feature Not Namespace Specific", "S-43"),
    (0x10, "Firmware Activation Requires NVM Subsystem Reset", "S-40"), (0x11, "Firmware Activation Requires Reset", "S-40"),
    (0x12, "Firmware Activation Requires Maximum Time Violation", "S-41"), (0x13, "Firmware Activation Prohibited", "S-41"),
    (0x14, "Overlapping Range", "S-36"), (0x15, "Namespace Insufficient Capacity", "S-44"),
    (0x16, "Namespace Identifier Unavailable", "S-44"), (0x18, "Namespace Already Attached", "S-44"),
    (0x19, "Namespace Is Private", "S-44"), (0x1A, "Namespace Not Attached", "S-44"), (0x1B, "Thin Provisioning Not Supported", "S-44"),
    (0x1C, "Controller List Invalid", "S-44"), (0x1D, "Device Self-test In Progress", "S-45"),
    (0x1E, "Boot Partition Write Prohibited", "S-46"), (0x1F, "Invalid Controller Identifier", "S-47"),
    (0x20, "Invalid Secondary Controller State", "S-47"), (0x21, "Invalid Number of Controller Resources", "S-47"),
    (0x22, "Invalid Resource Identifier", "S-47"), (0x23, "Sanitize Prohibited While PMR Enabled", "S-48"),
    (0x24, "ANA Group Identifier Invalid", "S-44"), (0x25, "ANA Attach Failed", "S-44"),
    (0x26, "Insufficient Capacity (libnvme)", "S-44"), (0x27, "Namespace Attachment Limit Exceeded (libnvme)", "S-44"),
    (0x28, "Prohibition of Command Execution Not Supported (libnvme)", "S-44"),
    (0x29, "I/O Command Set Not Supported", "S-24"), (0x2A, "I/O Command Set Not Enabled", "S-24"),
    (0x2B, "I/O Command Set Combination Rejected", "S-24"), (0x2C, "Invalid I/O Command Set", "S-24"),
    (0x2D, "Identifier Unavailable", "S-24"),
    (0x80, "Conflicting Attributes", "S-49"), (0x81, "Invalid Protection Information", "S-50"),
    (0x82, "Attempted Write to Read Only Range", "S-51"), (0x83, "Command Size Limits Exceeded", "S-52"),
    (0x84, "Fabrics Connect Invalid Host (not on PCIe)", "S-53"), (0x85, "Copy: Incompatible Namespace or Format", "S-52"),
    (0x86, "Copy: Fast Copy Not Possible", "S-52"), (0x87, "Copy: Overlapping I/O Range", "S-52"),
    (0x89, "Copy: Insufficient Resources", "S-52"), (0x90, "Discovery Restart (Fabrics, not on PCIe)", "S-53"),
    (0x91, "Authentication Required (Fabrics, not on PCIe)", "S-53"),
    (0xB6, "Invalid Zone Operation Request", "S-58"), (0xB7, "ZRWA Resources Unavailable", "S-58"),
    (0xB8, "Zone Boundary Error", "S-54"), (0xB9, "Zone Is Full", "S-55"), (0xBA, "Zone Is Read Only", "S-55"),
    (0xBB, "Zone Is Offline", "S-55"), (0xBC, "Zone Invalid Write", "S-56"), (0xBD, "Too Many Active Zones", "S-57"),
    (0xBE, "Too Many Open Zones", "S-57"), (0xBF, "Invalid Zone State Transition", "S-58"),
]:
    _s(1, _sc, _n, _c)

for _sc, _n, _c in [
    (0x80, "Write Fault", "S-59"), (0x81, "Unrecovered Read Error", "S-60"), (0x82, "End-to-end Guard Check Error", "S-61"),
    (0x83, "End-to-end Application Tag Check Error", "S-61"), (0x84, "End-to-end Reference Tag Check Error", "S-61"),
    (0x85, "Compare Failure", "S-62"), (0x86, "Access Denied", "S-63"), (0x87, "Deallocated or Unwritten Logical Block", "S-64"),
    (0x88, "End-to-end Storage Tag Check Error", "S-61"),
]:
    _s(2, _sc, _n, _c)

for _sc, _n, _c in [
    (0x00, "Internal Path Error", "S-65"), (0x01, "Asymmetric Access Persistent Loss", "S-66"),
    (0x02, "Asymmetric Access Inaccessible", "S-66"), (0x03, "Asymmetric Access Transition", "S-66"),
    (0x60, "Controller Pathing Error", "S-67"), (0x70, "Host Pathing Error (host-generated)", "S-68"),
    (0x71, "Host Aborted Command (host-generated)", "S-69"),
]:
    _s(3, _sc, _n, _c)

# codes that indicate success-with-information rather than failure
SUCCESS_LIKE = {(1, 0x0B), (1, 0x10), (1, 0x11)}
# codes that cannot appear on NVMe over PCIe (Fabrics- or host-generated); seeing them is itself suspicious
NOT_ON_PCIE = {(0, 0x16), (0, 0x18), (0, 0x19), (0, 0x1A), (0, 0x22), (1, 0x84), (1, 0x90), (1, 0x91), (3, 0x60), (3, 0x70), (3, 0x71)}


def status_info(sct: int, sc: int) -> tuple[str, str | None]:
    v = _STATUS.get((sct, sc))
    if v:
        return v
    if sct >= 4:
        return (f"{'Vendor Specific' if sct == 7 else 'Reserved SCT'} (SCT {sct} / SC 0x{sc:02X})", "S-70")
    return (f"Unknown status SCT {sct} / SC 0x{sc:02X}", None)


def all_status_cards() -> set[str]:
    return {c for _, c in _STATUS.values()} | {"S-70"}


# ------------------------------------------------------------------------------------------------- AEN (Async Event)
# CQE DW0 of an Asynchronous Event Request: event type bits 2:0, event info bits 15:8, log page bits 23:16
AEN_CARD = {
    (0, 0x00): ("Error: Invalid Doorbell Register", "S-78"), (0, 0x01): ("Error: Invalid Doorbell Write Value", "S-78"),
    (0, 0x02): ("Error: Diagnostic Failure", "S-79"), (0, 0x03): ("Error: Persistent Internal Error", "S-79"),
    (0, 0x04): ("Error: Transient Internal Error", "S-79"), (0, 0x05): ("Error: Firmware Image Load Error", "S-79"),
    (1, 0x00): ("SMART/Health: NVM subsystem reliability", "S-74"), (1, 0x01): ("SMART/Health: temperature threshold", "S-73"),
    (1, 0x02): ("SMART/Health: spare below threshold", "S-72"),
    (2, 0x00): ("Notice: Namespace Attribute Changed", "S-80"), (2, 0x01): ("Notice: Firmware Activation Starting", "S-81"),
    (2, 0x02): ("Notice: Telemetry Log Changed", "S-82"), (2, 0x03): ("Notice: Asymmetric Namespace Access Change", "S-83"),
    (2, 0x04): ("Notice: Predictable Latency Event Aggregate Log Change", "S-84"),
    (2, 0x05): ("Notice: LBA Status Information Alert", "S-84"), (2, 0x06): ("Notice: Endurance Group Event Aggregate Log Page Change", "S-84"),
    (2, 0xF0): ("Notice: Discovery Log Page Change", "S-84"),
    (6, 0x00): ("I/O Command Set: Reservation Log Page Available", "S-85"), (6, 0x01): ("I/O Command Set: Sanitize Operation Completed", "S-85"),
    (6, 0x02): ("I/O Command Set: Sanitize Completed With Unexpected Deallocation", "S-85"),
}
AEN_TYPE = {0: "Error", 1: "SMART/Health", 2: "Notice", 6: "I/O Command Set specific", 7: "Vendor specific"}


# ------------------------------------------------------------------------------------------------- SQE / CQE decode
class SQE:
    __slots__ = ("raw", "opc", "fuse", "psdt", "cid", "nsid", "cdw2", "cdw3", "mptr", "prp1", "prp2", "cdw10", "cdw11",
                 "cdw12", "cdw13", "cdw14", "cdw15", "cdw0")

    def __init__(self, b: bytes):
        self.raw = b
        (self.cdw0, self.nsid, self.cdw2, self.cdw3, self.mptr, self.prp1, self.prp2,
         self.cdw10, self.cdw11, self.cdw12, self.cdw13, self.cdw14, self.cdw15) = struct.unpack_from("<IIIIQQQIIIIII", b, 0)
        self.opc = self.cdw0 & 0xFF
        self.fuse = (self.cdw0 >> 8) & 3
        self.psdt = (self.cdw0 >> 14) & 3
        self.cid = self.cdw0 >> 16

    @property
    def sgl_type(self) -> int:
        return self.raw[39] >> 4

    @property
    def sgl_len(self) -> int:
        return struct.unpack_from("<I", self.raw, 32)[0]


def decode_cqe(b: bytes, off: int = 0):
    """-> (dw0, dw1, sqhd, sqid, cid, phase, status15) where status15 = DW3[31:17]."""
    dw0, dw1, sqhd, sqid, cid, st = struct.unpack_from("<IIHHHH", b, off)
    return dw0, dw1, sqhd, sqid, cid, st & 1, st >> 1


def split_status(status15: int):
    """status15 (DW3 bits 31:17) -> sc, sct, crd, more, dnr"""
    sc = status15 & 0xFF
    sct = (status15 >> 8) & 0x7
    crd = (status15 >> 11) & 0x3
    more = (status15 >> 13) & 1
    dnr = (status15 >> 14) & 1
    return sc, sct, crd, more, dnr


def data_direction(opc: int) -> int:
    """NVMe convention: opcode bits 1:0 = 00 no data, 01 host->controller, 10 controller->host, 11 bidirectional."""
    return opc & 3


def data_length(qid: int, s: SQE, lba_size: int) -> int:
    """Bytes of host-memory data the command moves (0 if none / unknown)."""
    opc = s.opc
    if qid == 0:
        if opc == 0x06:
            return 4096
        if opc == 0x02:
            numd = ((s.cdw11 & 0xFFFF) << 16 | (s.cdw10 >> 16)) + 1
            return numd * 4
        if opc == 0x11:
            return ((s.cdw10) + 1) * 4
        if opc in (0x0D, 0x15):
            return 4096 if (opc == 0x0D and (s.cdw10 & 0xF) == 0) or opc == 0x15 else 0
        if opc in (0x81, 0x82):
            return s.cdw11
        if opc in (0x19, 0x1A):
            return ((s.cdw10) + 1) * 4
        if opc == 0x86:
            return ((s.cdw12) + 1) * 4
        if opc == 0x0A and (s.cdw10 & 0xFF) in (0x03, 0x0E):   # Get Features with data (LBA Range Type, Timestamp)
            return 4096 if (s.cdw10 & 0xFF) == 0x03 else 8
        if opc == 0x09 and (s.cdw10 & 0xFF) in (0x03, 0x0E):
            return 4096 if (s.cdw10 & 0xFF) == 0x03 else 8
        return 0
    if opc in (0x01, 0x02, 0x05, 0x7D):
        return ((s.cdw12 & 0xFFFF) + 1) * lba_size
    if opc == 0x09:
        return ((s.cdw10 & 0xFF) + 1) * 16
    if opc == 0x19:
        return ((s.cdw12 & 0xFF) + 1) * 32
    if opc == 0x0E:
        return (s.cdw10 + 1) * 4
    if opc == 0x0D:
        return 24
    if opc == 0x11:
        return 16
    if opc == 0x15:
        return 8
    if opc == 0x7A:
        return (s.cdw12 + 1) * 4
    if opc == 0x79:
        return 0
    return 0


def lba_range(qid: int, s: SQE):
    """(slba, nlb) for LBA-addressed I/O commands, else (None, None)."""
    if qid != 0 and s.opc in (0x01, 0x02, 0x04, 0x05, 0x08, 0x0C, 0x7D):
        return s.cdw10 | (s.cdw11 << 32), (s.cdw12 & 0xFFFF) + 1
    return None, None


def fua(qid: int, s: SQE) -> bool:
    return qid != 0 and s.opc in (0x01, 0x02) and bool(s.cdw12 & (1 << 30))


# ------------------------------------------------------------------------------------------------- reserved-bit masks
def reserved_violations(qid: int, s: SQE) -> list[str]:
    """Conservative subset of reserved fields (stable across NVMe 1.3 - 2.x).  Verify for your revision."""
    out = []
    if s.cdw0 & (0xF << 10):
        out.append(f"CDW0 bits 13:10 = {(s.cdw0 >> 10) & 0xF:#x}")
    if qid != 0:
        if s.opc == 0x00 and (s.cdw10 or s.cdw11 or s.cdw12 or s.cdw13 or s.cdw14 or s.cdw15 or s.prp1 or s.prp2):
            out.append("Flush with non-zero CDW10-15 / data pointer")
        if s.opc == 0x09:
            if s.cdw10 >> 8:
                out.append(f"DSM CDW10 bits 31:8 = {s.cdw10 >> 8:#x}")
            if s.cdw11 >> 3:
                out.append(f"DSM CDW11 bits 31:3 = {s.cdw11 >> 3:#x}")
    else:
        if s.opc == 0x05 and (s.cdw11 >> 2) & 0x3FFF:
            out.append(f"Create I/O CQ CDW11 bits 15:2 = {(s.cdw11 >> 2) & 0x3FFF:#x}")
        if s.opc == 0x01 and (s.cdw11 >> 3) & 0x1FFF:
            out.append(f"Create I/O SQ CDW11 bits 15:3 = {(s.cdw11 >> 3) & 0x1FFF:#x}")
        if s.opc in (0x00, 0x04) and (s.cdw10 >> 16 or s.cdw11 or s.cdw12):
            out.append("Delete I/O queue with non-zero CDW10[31:16]/CDW11/CDW12")
        if s.opc == 0x08 and (s.cdw11 or s.cdw12 or s.cdw13 or s.cdw14 or s.cdw15):
            out.append("Abort with non-zero CDW11-15")
    return out


# ------------------------------------------------------------------------------------------------- Identify fields
def parse_identify_ctrl(b: bytes) -> dict:
    out = {}
    if len(b) >= 78:
        out["vid"] = struct.unpack_from("<H", b, 0)[0]
        out["sn"] = b[4:24].decode("ascii", "replace").strip()
        out["mn"] = b[24:64].decode("ascii", "replace").strip()
        out["fr"] = b[64:72].decode("ascii", "replace").strip()
        out["mdts"] = b[77]
    if len(b) >= 134:
        out["crdt"] = list(struct.unpack_from("<HHH", b, 128))
    if len(b) >= 260:
        out["acl"] = b[258]
        out["aerl"] = b[259]
    if len(b) >= 276:
        out["hmpre"] = struct.unpack_from("<I", b, 272)[0]
    if len(b) >= 540:
        out["nn"] = struct.unpack_from("<I", b, 516)[0]
        out["oncs"] = struct.unpack_from("<H", b, 520)[0]
        out["fuses"] = struct.unpack_from("<H", b, 522)[0]
        out["vwc"] = b[525]
        out["sgls"] = struct.unpack_from("<I", b, 536)[0]
    return out


def parse_identify_ns(b: bytes) -> dict:
    out = {}
    if len(b) >= 34:
        out["nsze"], out["ncap"], out["nuse"] = struct.unpack_from("<QQQ", b, 0)
        out["nsfeat"] = b[24]
        out["flbas"] = b[26]
        out["dlfeat"] = b[33]
    if len(b) >= 128:
        out["nguid"] = b[104:120].hex()
        out["eui64"] = b[120:128].hex()
    if len(b) >= 192 and "flbas" in out:
        idx = (out["flbas"] & 0xF) | (((out["flbas"] >> 5) & 3) << 4)
        if idx < 64 and 128 + idx * 4 + 4 <= len(b):
            ms, lbads = struct.unpack_from("<HB", b, 128 + idx * 4)
            if 9 <= lbads <= 16:
                out["lba_size"] = 1 << lbads
                out["ms"] = ms
    return out


SMART_WARN_CARDS = [(0, "Available Spare below threshold", "S-72"), (1, "Temperature threshold", "S-73"),
                    (2, "NVM subsystem reliability degraded", "S-74"), (3, "Media placed in read-only mode", "S-75"),
                    (4, "Volatile memory backup device failed", "S-76"), (5, "PMR read-only / unreliable", "S-77")]
