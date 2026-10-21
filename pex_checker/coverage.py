"""Coverage of the 326 atlas cards: which ones have their own rule, which ones are reported through a sibling rule
(same evidence, the sibling's finding lists the card in related_cards), and which cannot be checked from a single-link
packet export (with the reason).  The 'implemented' set is computed from the source code, so it cannot drift."""
from __future__ import annotations

import os
import re

from .cards import atlas

HERE = os.path.dirname(os.path.abspath(__file__))

# card -> (sibling card(s) whose finding carries the evidence, note)
SIBLING = {
    "S-90": ("C-01", "CSTS.CFS=1 in a captured CSTS read is reported as C-01 (same register bit; S-90 is the status view)."),
    "T-03": ("C-13", "CRTO vs CAP.TO inconsistency from captured register reads is reported as C-13."),
    "T-04": ("C-03", "CSTS.RDY still 1 after CC.EN=0 beyond CAP.TO is reported as C-03."),
    "T-05": ("C-04", "Shutdown not complete (CSTS.SHST != 10b) within RTD3E / the configurable limit is reported as C-04."),
    "T-15": ("C-16 T-44", "A controller that never wakes shows as CSTS all-ones (C-16) or link down under I/O (T-44)."),
    "T-36": ("X-19", "Four transmissions of one PSN without Ack (REPLAY_NUM rollover) is rule X-19."),
    "V-35": ("X-15 L-02 T-35 X-18 X-19", "The DLL replay pattern is split into its parts: bad LCRC, Nak, replay-timer replay, Nak storm, rollover."),
    "P-11": ("L-25 X-09", "Link below capability at the end of the capture (L-25) and downshifts during operation (X-09)."),
    "P-03": ("T-47", "Per-command tail-latency outliers with stage attribution are T-47 findings; percentiles are in summary.json."),
    "C-09": ("V-01 V-02 V-03 V-04 S-78", "Bad doorbell writes are V-01..V-04; the resulting async event (if an AER was outstanding) is S-78."),
    "C-11": ("S-79", "Persistent Internal Error async event (type 0, info 03h) is reported as S-79 with C-11 in related_cards."),
    "C-15": ("T-21 V-15", "CQE consumed by the host without an MSI-X (polled) is T-21; CQE with no MSI-X within the window is V-15."),
    "D-30": ("T-21 V-15", "Same evidence as C-15."),
    "C-19": ("V-09 D-29", "CQE for an unknown CID is V-09, a second CQE for a completed CID is D-29."),
    "D-28": ("V-09 V-10", "A wrong CID shows as a CQE for an unknown CID (V-09) or naming the wrong SQ (V-10)."),
    "C-24": ("D-22", "Duplicate NGUID/EUI-64 across Identify Namespace data captured in the trace is D-22."),
    "C-23": ("D-05", "Write Zeroes success is recorded (D-05, partly visible); with --verify-data a later non-zero read is flagged."),
    "D-02": ("D-01", "Deallocate ranges are decoded from the range-list DMA and listed in the D-01 finding; edges need a read-back."),
    "D-06": ("D-01", "Same as D-02: the trace proves the ranges sent, not what the FTL unmapped."),
    "D-13": ("D-15", "With --verify-data every captured write is hashed per LBA; a later read that differs is D-15 (related D-13/D-14/D-16)."),
    "D-14": ("D-15", "See D-13."),
    "D-16": ("D-15", "See D-13."),
    "L-27": ("T-44", "Link down with commands outstanding is T-44; whether Surprise Down was reported is in AER (not on this link)."),
    "L-29": ("T-44", "Hot removal under I/O shows as link down with outstanding commands (T-44)."),
    "L-36": ("L-20", "An SSD DMA rejected by the IOMMU shows as a UR completion to the device's MRd (L-20 rule UR-DMA); "
                     "the DMAR log is host-side."),
    "L-37": ("T-45 T-44 X-14", "ASPM exit too slow (T-45), failed L1 entry / errors on idle exit (X-14), link loss (T-44)."),
    "C-07": ("T-14 C-16", "Power-state exit slower than EXLAT (T-14) or device gone (C-16)."),
    "C-22": ("T-14 C-16", "See C-07."),
    "C-17": ("C-06 C-16", "Reset loop (C-06) or device gone (C-16); the DEAD decision is in the host driver."),
    "C-18": ("X-26 L-28", "Error messages before recovery (X-26) and DPC link disable (L-28); the recovery callbacks are host-side."),
    "T-48": ("T-47", "A slow command with no overlapping link event is attributed to the device stage by T-47, which names GC; proof "
                     "needs vendor telemetry."),
    "T-49": ("T-47", "Same as T-48 (thermal throttling is named as a candidate; prove with SMART temperature)."),
    "P-05": ("T-47", "Same as T-48."),
    "P-06": ("T-47", "Same as T-49."),
}

# card -> reason it is not checkable from a packet-level export of one link
NOT_CHECKABLE = {
    "S-86": "Persistent Event log entries are vendor/implementation specific in content; the log bytes are in the trace only if "
            "the host reads them, and decoding them reliably needs the vendor layout.",
    "S-87": "Telemetry log content is vendor-proprietary; the trace can only show that it was read.",
    "S-93": "Counters creeping up without a warning need two SMART reads and a trend judgement; not visible as an error on the link.",
    "T-06": "Runtime D3 entry/exit is driven by host PM; RTD3R is in Identify data but the D3 transitions of the NVMe function "
            "need PMCSR decoding plus host intent; only partly visible and not implemented.",
    "T-07": "Keep Alive timeout is an NVMe-oF mechanism (not visible on a PCIe link).",
    "T-08": "Keep Alive on fabrics, not on this link.",
    "T-12": "Sanitize progress is reported through repeated Sanitize Status log reads (SPROG); needs host polling in the capture "
            "and an estimate to compare; not implemented.",
    "T-13": "Self-test duration needs repeated log reads and EDSTT; not implemented (S-88 reports failed results).",
    "T-19": "ANA transitions are a multipath/fabrics concept (not visible on a single PCIe link).",
    "T-20": "Controller timestamp drift needs Get Features Timestamp reads against an external clock.",
    "T-25": "A timeout during controller reset is a host-driver state; the trace shows the reset (CC.EN) and the commands, but "
            "not the host's timer.",
    "T-26": "Multipath failover timing is host-side.",
    "T-27": "Path loss handling is host/fabrics side.",
    "T-28": "Fabrics reconnect (not on PCIe).",
    "T-29": "NVMe/TCP or RDMA transport (not on PCIe).",
    "T-30": "TLS handshake (NVMe/TCP).",
    "T-31": "A quirk delay before reading CSTS.RDY is a host-driver behaviour; the trace shows the CSTS reads (see T-02/C-02).",
    "T-50": "Write cliff needs a sustained, controlled workload and steady-state comparison; latency stats are in summary.json only.",
    "D-04": "A discard that never left the host produces no packet: absence cannot be attributed from the trace alone.",
    "D-09": "Whether a FUA write reached media is internal; the FUA bit is decoded (commands.csv) but durability needs a "
            "power-loss test.",
    "D-10": "Power-loss data loss is decided inside the SSD.",
    "D-11": "Torn writes need power-cut testing and read-back.",
    "D-12": "Persistence ordering across a power cut is not visible on the link.",
    "D-17": "Internal read retries show only as latency; T-47 flags the outliers but cannot prove media retries.",
    "D-18": "Needs a read-back after Sanitize plus the sanitize semantics; not implemented beyond S-89 (failed sanitize log).",
    "D-19": "Crypto erase effectiveness needs read-back of known data; not implemented.",
    "D-21": "PI checking needs the metadata format and recomputing CRC-16 T10-DIF over captured data; not implemented.",
    "D-23": "Needs knowledge of which CNS values the device claims to support (Identify CNS 00h/06h) against its replies; not "
            "implemented.",
    "D-24": "Capacity/format mis-reporting needs a reference capacity (out-of-band) to compare Identify data against.",
    "D-25": "SMART counter plausibility needs repeated reads and workload accounting; not implemented.",
    "D-27": "ZNS write-pointer consistency needs zone report decoding; not implemented.",
    "D-33": "Verify/Compare doing no media work is internal; only latency could hint at it.",
    "L-24": "IDE (integrity and data encryption) fields are not columns of the 13.28.b.15 packet export.",
    "L-33": "ACS acts in the switch/root port, not on the SSD link; needs a probe on the other links.",
    "C-10": "Keep Alive with KATO is a fabrics feature; not used on PCIe controllers in practice.",
    "C-25": "Legacy MSI (not MSI-X) delivery is not modelled: MSI capability writes are not decoded.",
    "C-27": "Same as T-31 (host quirk delay).",
    "C-28": "Usability after FLR needs host intent; FLR timing itself is T-43.",
    "X-23": "Flit-mode (PCIe 6.x) fields are not columns of this export.",
    "X-24": "Flit-mode replay/sequence fields are not columns of this export.",
    "X-25": "Flit negotiation fields are not columns of this export.",
    "X-27": "Probe-induced signal-integrity problems need a comparison capture without the interposer.",
    "X-29": "Analyzer setup errors (lane map, polarity, descrambler) corrupt the decode itself; the export cannot reveal them "
            "reliably beyond the PSN-gap/re-lock heuristics (X-30, X-28).",
    "V-36": "Flit-mode errors: no Flit columns in this export.",
    "P-01": "Throughput vs link capability needs a saturating workload; bytes moved per second are reported in summary.json "
            "(io_stats) but not judged.",
    "P-02": "IOPS collapse at high QD needs a controlled QD sweep; queue depth per command is in commands.csv.",
    "P-04": "SLC-cache write cliff needs a sustained sequential-write workload and its throughput curve.",
    "P-09": "CPU-bound submission is host-side (doorbell spacing is visible but not attributable).",
    "P-10": "NUMA placement is host-side.",
}


def implemented_cards() -> set:
    from . import engine, link, nvme_defs as nd
    src = ""
    for fn in ("engine.py", "link.py", "nvme.py"):
        with open(os.path.join(HERE, fn), encoding="utf-8") as f:
            src += f.read()
    em = set(re.findall(r'emit\(\s*\(?"([A-Z]-\d\d)"', src))
    em |= set(re.findall(r'card = "([A-Z]-\d\d)"', src))
    em |= set(re.findall(r'"([A-Z]-\d\d)" if', src)) | set(re.findall(r'else "([A-Z]-\d\d)"', src))
    em |= nd.all_status_cards() | {c for _, c in nd.AEN_CARD.values()} | {c for *_, c in nd.SMART_WARN_CARDS}
    em |= {c for *_, c in engine.AER_UNCOR} | {c for *_, c in engine.AER_COR} | set(link.PHYS_CARD.values())
    return em


def coverage_table():
    A = atlas()
    impl = implemented_cards()
    rows = []
    for cid in A.order:
        c = A.cards[cid]
        vis = A.visibility(cid)
        if cid in impl:
            st = "implemented"
            note = ("partly visible: finding says what extra evidence is needed" if c["pex"] == "part" else
                    ("reported when the evidence happens to be on the link (status/register read)" if c["pex"] == "no" else ""))
        elif cid in SIBLING:
            st = "covered by sibling rule"
            note = f"{SIBLING[cid][0]}: {SIBLING[cid][1]}"
        elif c["pex"] == "no":
            st = "not checkable"
            note = NOT_CHECKABLE.get(cid) or "Atlas verdict: not visible in a PEX trace."
        else:
            st = "not checkable"
            note = NOT_CHECKABLE.get(cid, "not implemented")
        rows.append({"card": cid, "name": c["name"], "atlas_visibility": vis, "status": st, "note": note,
                     "fault_side": A.side_of(cid)})
    return rows


def coverage_counts(rows):
    out = {}
    for r in rows:
        out[r["status"]] = out.get(r["status"], 0) + 1
    out["total"] = len(rows)
    return out


def write_coverage_md(path, rows, seen_cards=None):
    seen_cards = seen_cards or {}
    cnt = coverage_counts(rows)
    by_vis = {}
    for r in rows:
        k = (r["atlas_visibility"], r["status"])
        by_vis[k] = by_vis.get(k, 0) + 1
    L = ["# pex_checker coverage of the PEX Trace Error Atlas", "",
         f"{cnt.get('implemented', 0)} of {cnt['total']} cards have their own rule, "
         f"{cnt.get('covered by sibling rule', 0)} are reported through a sibling rule, "
         f"{cnt.get('not checkable', 0)} cannot be checked from a single-link packet export.", "",
         "| Atlas verdict | implemented | covered by sibling | not checkable |", "|---|---:|---:|---:|"]
    for vis in ("Visible", "Partly visible", "Not visible"):
        L.append(f"| {vis} | {by_vis.get((vis, 'implemented'), 0)} | {by_vis.get((vis, 'covered by sibling rule'), 0)} | "
                 f"{by_vis.get((vis, 'not checkable'), 0)} |")
    L += ["", "Fault side: H host, D device, L link/platform, B either, N expected behaviour, A analyzer artifact "
              "(from data/fault_side.txt).", "",
          "| Card | Name | Atlas | Status | Side | Seen in this trace | Note |", "|---|---|---|---|---|---:|---|"]
    for r in rows:
        name = r["name"].replace("|", "/")
        note = r["note"].replace("|", "/")
        L.append(f"| {r['card']} | {name} | {r['atlas_visibility']} | {r['status']} | {r['fault_side']} | "
                 f"{seen_cards.get(r['card'], '')} | {note} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return cnt
