"""Finding construction: one finding = one problem at one packet, mapped to an atlas card, with the evidence packets,
the likely fault side, link state, stage breakdown and a reproduction block (nvme-cli passthru of the exact SQE)."""
from __future__ import annotations

from collections import Counter

from .cards import SIDE_NAMES, atlas
from . import nvme_defs as nd

SEV_ORDER = {"error": 0, "warning": 1, "info": 2}


def _hx(v: int) -> str:
    return f"0x{v:X}"


def nvme_cli_repro(cmd) -> dict:
    """Exact SQE fields as an nvme-cli passthru command (plus a friendlier equivalent where one exists)."""
    s = cmd.sqe
    if s is None:
        return {"note": "SQE bytes were not captured (fetch completion missing or payload filtered); only the doorbell is known."}
    admin = cmd.sqid == 0
    dirn = nd.data_direction(s.opc)
    nbytes = cmd.nbytes
    dev = "/dev/nvme0" if admin else f"/dev/nvme0n{s.nsid if 0 < s.nsid < 0xFFFFFFFF else 1}"
    parts = ["nvme", "admin-passthru" if admin else "io-passthru", dev, f"--opcode={_hx(s.opc)}",
             f"--namespace-id={s.nsid}"]
    if s.cdw2:
        parts.append(f"--cdw2={_hx(s.cdw2)}")
    if s.cdw3:
        parts.append(f"--cdw3={_hx(s.cdw3)}")
    for i, v in enumerate((s.cdw10, s.cdw11, s.cdw12, s.cdw13, s.cdw14, s.cdw15)):
        if v:
            parts.append(f"--cdw{10 + i}={_hx(v)}")
    if nbytes:
        parts.append(f"--data-len={nbytes}")
        if dirn == 2:
            parts.append("--read")
        elif dirn == 1:
            parts += ["--write", "--input-file=<file with the original payload>"]
    if s.fuse or s.psdt:
        parts.append(f"# FUSE={s.fuse} PSDT={s.psdt}: nvme-cli cannot set CDW0 flags (Linux rejects them); use a user-space driver")
    if s.mptr:
        parts.append("# MPTR was set: add --metadata-len=<bytes> if the namespace has separate metadata")
    cli = " ".join(parts)
    friendly = ""
    if not admin and s.opc in (0x01, 0x02) and s.psdt == 0:
        slba = s.cdw10 | (s.cdw11 << 32)
        nlb0 = s.cdw12 & 0xFFFF
        verb = "read" if s.opc == 2 else "write"
        friendly = (f"nvme {verb} {dev} --start-block={slba} --block-count={nlb0} --data-size={nbytes}"
                    + (" --data=out.bin" if verb == "read" else " --data=in.bin") + (" --force-unit-access" if nd.fua(1, s) else ""))
    elif not admin and s.opc == 0x00:
        friendly = f"nvme flush {dev} --namespace-id={s.nsid}"
    elif admin and s.opc == 0x06:
        friendly = f"nvme id-ctrl /dev/nvme0" if (s.cdw10 & 0xFF) == 1 else f"nvme id-ns /dev/nvme0 --namespace-id={s.nsid}"
    elif admin and s.opc == 0x02:
        lid = s.cdw10 & 0xFF
        friendly = f"nvme get-log /dev/nvme0 --log-id={lid} --log-len={nbytes}"
    destructive = (admin and s.opc in nd.DESTRUCTIVE_ADMIN) or (not admin and s.opc in nd.DESTRUCTIVE_IO)
    fio = ""
    if not admin and s.opc in (0x01, 0x02) and nbytes:
        rw = "randread" if s.opc == 2 else "randwrite"
        fio = (f"fio --name=repro --filename={dev} --direct=1 --ioengine=io_uring --rw={rw} --bs={nbytes} "
               f"--iodepth={max(1, cmd.qd_sq)} --runtime=60 --time_based")
    return {"nvme_cli": cli, "friendly": friendly, "fio_load": fio,
            "safety": ("CAUTION: destructive command - run only on a scratch namespace / test system" if destructive else
                       "non-destructive (read-only) command"),
            "sqe_hex": s.raw.hex(" "),
            "sqe_fields": {"opcode": _hx(s.opc), "fuse": s.fuse, "psdt": s.psdt, "cid": _hx(s.cid), "nsid": s.nsid,
                           "mptr": _hx(s.mptr), "prp1_or_sgl": _hx(s.prp1), "prp2": _hx(s.prp2),
                           "cdw10": _hx(s.cdw10), "cdw11": _hx(s.cdw11), "cdw12": _hx(s.cdw12), "cdw13": _hx(s.cdw13),
                           "cdw14": _hx(s.cdw14), "cdw15": _hx(s.cdw15)}}


class FindingsMixin:
    """Mixed into the Engine: emit() builds a finding dict and applies per-card flood caps."""

    def _init_findings(self):
        self.findings: list[dict] = []
        self.card_counts = Counter()      # all occurrences (including suppressed)
        self.card_sev = {}
        self.suppressed = Counter()
        self.max_per_card = self.cfg.max_per_card
        self._fid = 0

    def ev(self, role: str, row: int, pkt: int, ts: float, **extra) -> dict:
        d = {"role": role, "row": row, "pkt": pkt, "ts": ts}
        if extra:
            d.update(extra)
        return d

    def ev_here(self, role: str, **extra) -> dict:
        return self.ev(role, self.row, self.pkt(), self.now, **extra)

    def emit(self, card: str, rule: str, severity: str, category: str, what: str, *, evidence: list | None = None,
             cmd=None, why: str = "", metrics: dict | None = None, related: list | None = None, side: str | None = None,
             stage: dict | None = None, at: dict | None = None, key=None) -> dict | None:
        """Create a finding.  `at` = primary evidence dict (defaults to the current packet)."""
        self.card_counts[card] += 1
        if SEV_ORDER.get(severity, 2) < SEV_ORDER.get(self.card_sev.get(card, "info"), 2) or card not in self.card_sev:
            self.card_sev[card] = severity
        if self.card_counts[card] > self.max_per_card:
            self.suppressed[card] += 1
            return None
        A = atlas()
        if at is None:
            at = self.ev_here("finding")
        evidence = evidence or []
        if not any(e.get("row") == at["row"] for e in evidence):
            evidence = [dict(at, role=at.get("role", "finding"))] + evidence
        sd = side or A.side_of(card)
        self._fid += 1
        f = {
            "id": self._fid,
            "card_id": card, "card_name": A.name(card), "visibility": A.visibility(card), "rule": rule,
            "category": category, "severity": severity,
            "fault_side": SIDE_NAMES.get(sd, sd), "fault_side_code": sd,
            "pkt": at["pkt"], "row": at["row"], "timestamp": at["ts"],
            "what": what,
            "why": why or A.text(card, "What happens", 600),
            "typical_causes": A.text(card, "Typical causes", 500),
            "how_to_confirm": A.text(card, "How to detect / prove", 600),
            "atlas_in_trace": A.text(card, "In a PEX trace", 500),
            "spec_basis": A.text(card, "Spec basis", 400),
            "test_to_provoke": A.text(card, "Test to provoke it", 400),
            "extra_evidence_needed": A.extra_evidence(card),
            "related_cards": related or [],
            "evidence": evidence[:40],
            "metrics": metrics or {},
            "link_state": self.link_snapshot(),
        }
        if cmd is not None:
            f.update(self.cmd_context(cmd))
            if stage is None and getattr(cmd, "cqe_ts", None) is not None:
                stage = cmd.stages()
        if stage:
            f["stage_breakdown_us"] = stage
        self.findings.append(f)
        return f

    def cmd_context(self, cmd) -> dict:
        return {"sqid": cmd.sqid, "cid": cmd.cid, "opcode": cmd.opc, "opcode_name": cmd.name, "nsid": cmd.nsid,
                "slba": cmd.slba, "nlb": cmd.nlb, "bytes": cmd.nbytes,
                "queue_depth_at_submit": {"sq": cmd.qd_sq, "all_queues": cmd.qd_all},
                "preceding_commands": cmd.prev,
                "link_state_at_submit": cmd.link_at_submit,
                "command_packets": cmd.packets(),
                "repro": nvme_cli_repro(cmd)}
