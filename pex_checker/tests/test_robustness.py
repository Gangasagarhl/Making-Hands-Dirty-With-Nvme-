"""Robustness to real-export variations: header aliases, quoted fields, Up/Down, integer-ns timestamps, hex without 0x,
hex tags, blank cells, missing columns; plus parser unit tests."""
import csv
import math
import os

from pex_checker import schema as S
from pex_checker import nvme_defs as nd
from pex_checker.config import Config
from pex_checker.engine import Engine
from pex_checker.stats import LogHist

RENAME = {"Packet": "Packet #", "Direction": "Dir", "TLP Type": "TLP", "DLLP Type": "DLLP", "Time Stamp": "Timestamp",
          "Requester ID": "Requester", "Cpl Status": "Completion Status", "Byte Cnt": "Byte Count (TLP)",
          "Lwr Addr": "Lower Address", "ACK/NAK Sequence Number": "Ack/Nak Seq Num", "Address": "Addr"}
DROP = {"ECRC", "Time Delta"}


def make_variant(src, dst):
    with open(src, newline="", encoding="utf-8") as fi, open(dst, "w", newline="", encoding="utf-8") as fo:
        r = csv.reader(fi)
        w = csv.writer(fo, quoting=csv.QUOTE_ALL)
        head = next(r)
        keep = [i for i, h in enumerate(head) if h not in DROP]
        ix = {h: i for i, h in reversed(list(enumerate(head)))}
        fo.write("Teledyne LeCroy PCIe Protocol Analysis export (SYNTHETIC variant)\n")
        w.writerow([RENAME.get(head[i], head[i]) for i in keep])
        for row in r:
            d = row[ix["Direction"]]
            row[ix["Direction"]] = "Up" if d == "Upstream" else ("Down" if d == "Downstream" else d)
            t = row[ix["Time Stamp"]]
            if t:
                s, ns = t.split(".")
                row[ix["Time Stamp"]] = str(int(s) * 1_000_000_000 + int(ns))
            a = row[ix["Address"]]
            if a.startswith("0x"):
                row[ix["Address"]] = a[2:]
            tg = row[ix["Tag"]]
            if tg:
                row[ix["Tag"]] = f"0x{int(tg):X}"
            w.writerow([row[i] for i in keep])


def test_variant_export_gives_same_findings(fault_run, tmp_path):
    dst = str(tmp_path / "variant.csv")
    make_variant(fault_run["trace"], dst)
    e = Engine(Config()).run(dst, quiet=True)
    assert "ecrc" in e.missing_cols
    assert any("ECRC" in m["feature"] for m in e.missing_features)
    assert e.time_unit_used.startswith("ns")
    by_card = {}
    for f in e.findings:
        by_card.setdefault(f["card_id"], set()).add(f["pkt"])
    missed = [t["id"] for t in fault_run["truth"]["faults"]
              if t["card"] != "V-34" and t["pkt"] not in by_card.get(t["card"], set())]
    assert not missed, missed
    assert "V-34" not in by_card       # ECRC column dropped -> rule disabled, not guessed


def test_header_mapping_handles_duplicate_device_id():
    head = ["Packet", "Direction", "Device ID", "Time Stamp", "Vendor ID", "Device ID"]
    m = S.detect_mapping(head)
    assert m["devid"] == 2 and m["pkt"] == 0 and m["time"] == 3


def test_parsers():
    assert S.hexint("0x00000000F0001008") == 0xF0001008
    assert S.hexint("F000_1008") == 0xF0001008
    assert S.hexint("1008h") == 0x1008
    assert S.hexint("") == -1
    assert S.autoint("0x1F") == 31 and S.autoint("31") == 31 and S.autoint("1,024") == 1024
    assert math.isclose(S.parse_time("12.345 678 901 s"), 12.345678901)
    assert math.isclose(S.parse_time("1,234,567 ns"), 1.234567e-3)
    assert math.isclose(S.parse_time("12,345.678 us"), 12.345678e-3)
    assert math.isclose(S.parse_time("83171", 1e-9), 83171e-9)
    assert S.direction("Upstream") == "U" and S.direction("Up") == "U" and S.direction("Down") == "D"
    assert S.direction("Downstream") == "D" and S.direction("") == ""
    assert S.is_bad("0x12345678 (Bad)") and not S.is_bad("0x6446BADA") and S.is_bad("LCRC Error")
    assert S.data_bytes("07 00 00 00")[0] == b"\x07\x00\x00\x00"
    assert S.data_bytes("00000007", dword_order=True)[0] == b"\x07\x00\x00\x00"
    assert S.data_bytes("0x07 0x00")[0] == b"\x07\x00"
    assert S.be_bytes(0xF, 0x0, 1) == 4 and S.be_bytes(0x3, 0x0, 1) == 2 and S.be_bytes(0xE, 0x7, 2) == 6
    assert S.cpl_status("Successful Completion") == "SC" and S.cpl_status("UR") == "UR" and S.cpl_status("CRS") == "CRS"
    assert S.classify_tlp("MWr64") == S.MWR and S.classify_tlp("CplD") == S.CPLD and S.classify_tlp("Cpl") == S.CPL
    assert S.classify_dllp("UpdateFC-NP")[1] == "NP" and S.classify_dllp("InitFC1-P")[2] is True
    assert S.parse_rate("16.0 GT/s (speed_change)") == 16.0 and S.parse_lanes("0-3") == 4


def test_status_table_maps_every_code_to_a_card():
    assert nd.status_info(0, 0x80) == ("LBA Out of Range", "S-25")
    assert nd.status_info(1, 0x02)[1] == "S-32"
    assert nd.status_info(2, 0x81)[1] == "S-60"
    assert nd.status_info(3, 0x02)[1] == "S-66"
    assert nd.status_info(7, 0x12)[1] == "S-70"
    sc, sct, crd, more, dnr = nd.split_status(0x80 | (1 << 14))
    assert (sc, sct, dnr) == (0x80, 0, 1)


def test_loghist_quantiles():
    h = LogHist()
    for i in range(1, 10001):
        h.add(float(i))
    assert 4500 < h.quantile(0.5) < 5500
    assert 9300 < h.quantile(0.99) <= 10000
    assert h.summary()["max_us"] == 10000.0


def test_quoted_multiline_and_short_rows(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text('Packet,Direction,TLP Type,Link Event,Time Stamp,Data\n'
                 '1,Downstream,,"Link Up (L0)",0.000000001,\n'
                 '2,Upstream,,"Recovery Entry\n(note, multi-line)",0.000000002\n'
                 '3,Downstream,,Recovery Exit (L0),0.000000003,\n', encoding="utf-8")
    e = Engine(Config()).run(str(p), quiet=True)
    assert e.rows_total == 3
    assert e.recovery_count == 1
    assert "psn" in e.missing_cols


def test_late_start_capture(tmp_path):
    """Capture that starts after initialisation: no false errors; with --bar0/--sq/--cq the commands decode."""
    import json
    from pex_checker import synth
    from pex_checker.cli import main
    path, _ = synth.generate(str(tmp_path), rows=60_000, seed=5, clean=True)
    late = tmp_path / "late.csv"
    with open(path, encoding="utf-8") as fi, open(late, "w", encoding="utf-8") as fo:
        for i, line in enumerate(fi):
            if i == 0 or i > 20_000:
                fo.write(line)
    assert main([str(late), "--out", str(tmp_path / "o1"), "--quiet"]) == 0
    s1 = json.load(open(tmp_path / "o1" / "summary.json"))
    assert not s1["findings"]["by_severity"].get("error") and not s1["findings"]["by_severity"].get("warning")
    assert "inferred" in s1["learned"]["BAR0"]
    args = [str(late), "--out", str(tmp_path / "o2"), "--quiet", "--bar0", "0xF0000000"]
    for q in range(1, 9):
        args += ["--cq", f"{q}:0x{0x120000000 + q * 0x10000:X}:256:{q}", "--sq", f"{q}:0x{0x110000000 + q * 0x10000:X}:256:{q}"]
    assert main(args) == 0
    s2 = json.load(open(tmp_path / "o2" / "summary.json"))
    assert s2["nvme"]["counts"]["completed"] > 100
    assert not s2["findings"]["by_severity"].get("error") and not s2["findings"]["by_severity"].get("warning")
