"""Clean SYNTHETIC trace: no errors, no warnings.  Output files: structure and consistency."""
import json
import os


def test_clean_trace_has_no_errors_or_warnings(clean_run):
    bad = [f for f in clean_run["findings"] if f["severity"] in ("error", "warning")]
    assert not bad, [(f["card_id"], f["pkt"], f["what"][:120]) for f in bad[:10]]
    # info findings allowed only for partly-visible records (deallocate) - justified, not problems
    assert {f["card_id"] for f in clean_run["findings"]} <= {"D-01"}


def test_clean_trace_decodes_every_command(clean_run):
    s = clean_run["summary"]
    n = s["nvme"]["counts"]
    assert n["completed"] > 500
    assert n["data_unattributed"] == 0
    assert s["schema"]["missing_columns"] == []
    assert s["schema"]["columns"] == 112
    # one row per command in commands.csv, all with a full lifecycle (the long-lived AER has no CQE)
    cmds = clean_run["commands"]
    assert len(cmds) >= n["completed"]
    io = [c for c in cmds if c["sqid"] != "0"]
    assert all(c["cqe_row"] and c["doorbell_row"] and c["fetch_row"] for c in io)
    assert all(float(c["us_total"]) > 0 for c in io)


def test_learned_configuration(clean_run):
    lc = clean_run["summary"]["learned"]
    assert lc["BAR0"].startswith("0xF0000000")
    assert lc["Max_Payload_Size"].startswith("256 B")
    assert lc["MDTS"].startswith("131072 B")
    assert lc["LBA size"].startswith("512 B")


def test_output_files(fault_run):
    out = fault_run["out"]
    for fn in ("findings.jsonl", "commands.csv", "summary.json", "report.html", "coverage.md"):
        assert os.path.getsize(os.path.join(out, fn)) > 0
    html = open(os.path.join(out, "report.html"), encoding="utf-8").read()
    assert "prefers-color-scheme: dark" in html and "<script" in html
    assert "http://" not in html and "https://" not in html      # self-contained, no network
    assert "nvme io-passthru" in html
    f = fault_run["findings"][0]
    for k in ("card_id", "card_name", "category", "severity", "fault_side", "pkt", "row", "timestamp", "what", "why",
              "how_to_confirm", "evidence", "link_state"):
        assert k in f


def test_coverage_table(fault_run):
    s = fault_run["summary"]["coverage"]
    assert s["total"] == 326
    assert s["implemented"] >= 230
    assert s["implemented"] + s["covered by sibling rule"] + s["not checkable"] == 326
    md = open(os.path.join(fault_run["out"], "coverage.md"), encoding="utf-8").read()
    assert md.count("\n| ") >= 326


def test_summary_throughput_and_memory_reported(fault_run):
    s = fault_run["summary"]
    assert s["rows"] > 100_000 and s["rows_per_s"] > 0 and s["peak_rss_mb"]
    assert s["config"]["io_timeout_s"] == 30.0 and s["config"]["cpl_timeout_ms"] == 50.0
