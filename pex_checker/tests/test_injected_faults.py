"""Every fault injected by the SYNTHETIC generator must be reported with the right atlas card at the right packet."""
import json
import os

import pytest

from pex_checker import synth

# The fault list is produced by the generator; collect the ids once (cheap, small trace) for parametrisation.
_IDS = None


def fault_ids():
    global _IDS
    if _IDS is None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            _, meta = synth.generate(d, rows=120_000, seed=11)
            _IDS = [f["id"] for f in meta["faults"]]
    return _IDS


def test_generator_injects_every_family(fault_run):
    cards = {f["card"] for f in fault_run["truth"]["faults"]}
    families = {c.split("-")[0] for c in cards}
    assert {"S", "T", "D", "L", "C", "X", "V"} <= families
    assert len(fault_run["truth"]["faults"]) >= 50
    assert fault_run["truth"]["synthetic"] is True


@pytest.mark.parametrize("fid", fault_ids())
def test_fault_found_at_packet(fault_run, fid):
    t = next(f for f in fault_run["truth"]["faults"] if f["id"] == fid)
    hits = [f for f in fault_run["findings"] if f["card_id"] == t["card"]]
    assert hits, f"{fid}: no {t['card']} finding; cards present {sorted({f['card_id'] for f in fault_run['findings']})}"
    exact = [f for f in hits if f["pkt"] == t["pkt"]]
    assert exact, f"{fid}: {t['card']} expected at packet {t['pkt']}, found at {[f['pkt'] for f in hits][:10]}"
    f = exact[0]
    # every finding carries the explanation, the fault side and the evidence
    assert f["what"] and f["why"] and f["how_to_confirm"]
    assert f["fault_side"] and f["category"] in ("timing", "timeout", "reordering", "protocol-state", "status-code",
                                                  "link-layer", "performance")
    assert f["evidence_rows"], "pass 2 must attach the evidence rows"
    assert any(r.get("focus") for r in f["context_rows"])


def test_command_findings_have_repro(fault_run):
    with_cmd = [f for f in fault_run["findings"] if f.get("sqid") is not None and f.get("repro", {}).get("nvme_cli")]
    assert len(with_cmd) > 20
    f = next(x for x in with_cmd if x["card_id"] == "S-25")
    assert "io-passthru" in f["repro"]["nvme_cli"] and "--opcode=0x2" in f["repro"]["nvme_cli"]
    assert f["queue_depth_at_submit"]["sq"] >= 1
    assert f["repro"]["sqe_hex"].count(" ") == 63


def test_slow_command_attributed_to_device(fault_run):
    f = next(x for x in fault_run["findings"] if x["card_id"] == "T-47")
    assert f["metrics"]["dominant_stage"].startswith("last_data_to_cqe")
    assert f["fault_side_code"] == "D"
    st = f["stage_breakdown_us"]
    assert st["last_data_to_cqe (device)"] > 12000


def test_l1_exit_slow_uses_capability(fault_run):
    f = next(x for x in fault_run["findings"] if x["card_id"] == "T-45")
    assert f["metrics"]["limit_us"] == 64.0
    assert f["metrics"]["l1_exit_us"] > 140
