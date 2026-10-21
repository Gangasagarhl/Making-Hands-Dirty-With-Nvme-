"""PEX Trace Error Atlas knowledge base: card texts (data/atlas_cards.json) and likely-fault side (data/fault_side.txt).

Findings quote the atlas texts for "why" and "how to confirm", so the report explains each problem in the same words
as the atlas card it maps to.
"""
from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

SIDE_NAMES = {"H": "host", "D": "device", "L": "link/platform", "B": "either (host or device)", "N": "expected behaviour",
              "A": "analyzer artifact"}

CATEGORIES = ["timing", "timeout", "reordering", "protocol-state", "status-code", "link-layer", "performance",
              "data-integrity"]


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


class Atlas:
    def __init__(self, cards_path: str | None = None, side_path: str | None = None):
        cards_path = cards_path or os.path.join(DATA, "atlas_cards.json")
        side_path = side_path or os.path.join(DATA, "fault_side.txt")
        with open(cards_path, encoding="utf-8") as f:
            raw = json.load(f)
        self.cards: dict[str, dict] = {c["id"]: c for c in raw}
        self.order = [c["id"] for c in raw]
        self.side: dict[str, str] = {}
        self.trigger: dict[str, str] = {}
        with open(side_path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("|")
                if len(parts) >= 2 and parts[0].strip():
                    self.side[parts[0].strip()] = parts[1].strip()
                    self.trigger[parts[0].strip()] = parts[2].strip() if len(parts) > 2 else ""

    def name(self, cid: str) -> str:
        c = self.cards.get(cid)
        return c["name"] if c else cid

    def visibility(self, cid: str) -> str:
        c = self.cards.get(cid)
        return {"yes": "Visible", "part": "Partly visible", "no": "Not visible"}.get(c["pex"], "?") if c else "?"

    def side_of(self, cid: str) -> str:
        return self.side.get(cid, "B")

    def text(self, cid: str, field: str, limit: int = 0) -> str:
        c = self.cards.get(cid)
        if not c:
            return ""
        t = _clean(c.get(field, ""))
        if field == "In a PEX trace":
            t = re.sub(r"^(Visible|Partly visible|Not visible)\s*", "", t)
        if limit and len(t) > limit:
            cut = t[:limit]
            j = cut.rfind(". ")
            t = cut[: j + 1] if j > limit * 0.5 else cut + "…"
        return t

    def extra_evidence(self, cid: str) -> str:
        """For partly-visible cards: what the trace cannot prove (taken from the card's PEX text)."""
        c = self.cards.get(cid)
        if not c or c["pex"] != "part":
            return ""
        t = self.text(cid, "In a PEX trace")
        m = re.search(r"(Not (?:visible|reliable)[^.]*\.(?:[^.]*\.)?|[^.]*\b(?:NOT|not) visible[^.]*\.|[^.]*needs?[^.]*\.)", t)
        hint = m.group(0).strip() if m else ""
        how = self.text(cid, "How to detect / prove", 300)
        return (hint + " Confirm with: " + how).strip()


_ATLAS: Atlas | None = None


def atlas() -> Atlas:
    global _ATLAS
    if _ATLAS is None:
        _ATLAS = Atlas()
    return _ATLAS
