"""Session fixtures: generate SYNTHETIC fault and clean traces once, run the full CLI on them, load the outputs."""
import csv
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pex_checker import synth  # noqa: E402
from pex_checker.cli import main  # noqa: E402

ROWS = 120_000


def _run(trace, out, *extra):
    rc = main([trace, "--out", out, "--quiet", *extra])
    assert rc == 0
    with open(os.path.join(out, "summary.json"), encoding="utf-8") as f:
        summary = json.load(f)
    with open(os.path.join(out, "findings.jsonl"), encoding="utf-8") as f:
        findings = [json.loads(line) for line in f]
    with open(os.path.join(out, "commands.csv"), encoding="utf-8", newline="") as f:
        commands = list(csv.DictReader(f))
    return {"out": out, "summary": summary, "findings": findings, "commands": commands}


@pytest.fixture(scope="session")
def fault_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("faults")
    path, meta = synth.generate(str(d), rows=ROWS, seed=11)
    res = _run(path, str(d / "out"))
    res["truth"] = meta
    res["trace"] = path
    return res


@pytest.fixture(scope="session")
def clean_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("clean")
    path, meta = synth.generate(str(d), rows=ROWS, seed=12, clean=True)
    res = _run(path, str(d / "out"))
    res["truth"] = meta
    res["trace"] = path
    return res
