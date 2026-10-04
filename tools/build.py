#!/usr/bin/env python3
"""Build the NVMe + PCIe Engineering Journey site.

Reads   src/manifest.json, src/pages/<id>.html (content fragments), src/glossary/*.json, src/assets/*
Writes  docs/<id>.html (one page per manifest entry), docs/book.html (single-file edition),
        docs/assets/{academy.css,academy.js,glossary.js,search-index.js}

Standard library only. Usage:
    python3 tools/build.py            # build everything
    python3 tools/build.py --check    # validate fragments, print problems, build nothing
    python3 tools/build.py --check ch04 ch05   # validate only these pages
    python3 tools/build.py --strict   # build, but fail on any missing page or error
"""
import html
import json
import math
import re
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OUT = ROOT / "docs"

CALLOUTS = {
    "kid": "🧒 Kid analogy",
    "analogy": "🌍 Real-world analogy",
    "breaks": "🧩 Where the analogy breaks",
    "hw": "🔧 Hardware view",
    "spec": "📘 Specification view",
    "proto": "📡 Protocol view",
    "trace": "🔍 Trace view",
    "linux": "🐧 Linux view",
    "debug": "🐞 Debugging",
    "lab": "🧪 Lab",
    "exercise": "🎯 Exercise",
    "warn": "⚠️ Warning",
    "insight": "💡 Senior insight",
    "remember": "📌 Remember",
    "interview": "🧠 Interview",
    "advanced": "🚀 Advanced",
    "verified": "✅ Verified against the specification",
    "derived": "🧮 Derived (reasoned, not quoted)",
    "impl": "⚙️ Implementation-dependent",
    "version": "🗓️ Version-dependent",
    "notsaid": "🚫 What the specification does not say",
    "mastery": "🏁 Mastery gate",
    "prereq": "↩️ Prerequisite reminder",
    "safety": "🛡️ Safety",
}
RUNGS = {
    "A": "Child / story",
    "B": "Simple technical",
    "C": "Hardware",
    "D": "Specification",
    "E": "Protocol",
    "F": "Trace",
    "G": "Debugging",
}
TRACE_KINDS = {
    "synthetic": "Synthetic trace — invented for teaching, not captured from hardware",
    "real": "Real trace — captured from hardware",
    "excerpted": "Excerpted trace — trimmed from a longer capture",
    "reconstructed": "Reconstructed trace — rebuilt from logs and evidence",
    "conceptual": "Conceptual trace — shows the idea, not a specific analyzer format",
}
CODE_KINDS = {
    "executable": "Executable",
    "pseudocode": "Pseudocode",
    "simulation": "Simulation",
    "read-only": "Read-only inspection",
    "hardware-dependent": "Hardware-dependent",
}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
SVG_SELF = {"path", "rect", "circle", "line", "polyline", "polygon", "ellipse", "use", "stop", "text"}


# ---------------------------------------------------------------- manifest

def load_manifest():
    m = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))
    pages = []
    for g in m["groups"]:
        for p in g["pages"]:
            p = dict(p)
            p["group"] = g["title"]
            p["group_id"] = g["id"]
            pages.append(p)
    for i, p in enumerate(pages):
        p["index"] = i
    return m, pages


def page_label(p):
    if p["kind"] == "chapter":
        return f"Chapter {p['num']}"
    if p["kind"] == "module":
        return f"Deep-Dive Module {p['num']:02d}"
    return {"front": "Start here", "part": "Course part", "tool": "Interactive tool", "appendix": "Reference"}.get(p["kind"], "")


def nav_num(p):
    if p["kind"] == "chapter":
        return str(p["num"])
    if p["kind"] == "module":
        return f"M{p['num']:02d}"
    if p["kind"] == "tool":
        return "⚙"
    return "·"


def sec_prefix(p):
    if p["kind"] == "chapter":
        return f"{p['num']}."
    if p["kind"] == "module":
        return f"M{p['num']:02d}."
    return ""


# ---------------------------------------------------------------- glossary

def load_glossary():
    gl = {}
    files = sorted((SRC / "glossary").glob("*.json"), key=lambda f: (f.name != "core.json", f.name))
    for f in files:
        try:
            entries = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"ERROR glossary {f.name}: {e}")
            continue
        for e in entries:
            k = e.get("key", "").strip().lower()
            if not k or not e.get("def"):
                continue
            if k in gl:
                seen = set(gl[k].get("see", []))
                gl[k]["see"] = gl[k].get("see", []) + [s for s in e.get("see", []) if s not in seen]
                continue
            gl[k] = {"key": k, "term": e.get("term", k), "exp": e.get("exp", ""), "def": e["def"], "see": list(e.get("see", []))}
    return gl


# ---------------------------------------------------------------- helpers

def slug(text):
    s = re.sub(r"<[^>]+>", "", text)
    s = html.unescape(s).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60] or "section"


def strip_tags(s):
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def attr(tag, name):
    m = re.search(r'\b' + re.escape(name) + r'\s*=\s*"([^"]*)"', tag)
    return html.unescape(m.group(1)) if m else None


def match_close(s, start, tag):
    """Index just past the close tag matching the open tag that starts at `start`."""
    pat = re.compile(r"<(/?)" + tag + r"\b[^>]*>", re.I)
    depth = 0
    for m in pat.finditer(s, start):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return m.start(), m.end()
    return None, None


def has_class(tag, cls):
    c = attr(tag, "class") or ""
    return cls in c.split()


# ---------------------------------------------------------------- SVG renderers

def svg_text_width(t, size=12):
    return len(t) * size * 0.56


def render_seq(open_tag, inner):
    actors = [a.strip() for a in (attr(open_tag, "data-actors") or "").split("|") if a.strip()]
    if not actors:
        return None, "sequence diagram without data-actors"
    items = re.findall(r"<li\b([^>]*)>(.*?)</li>", inner, flags=re.S)
    has_t = any(attr(a, "data-t") for a, _ in items)
    gutter = 78 if has_t else 10
    col = int(attr(open_tag, "data-col") or max(150, min(200, 1000 // len(actors))))
    x = {a: gutter + col / 2 + i * col for i, a in enumerate(actors)}
    width = gutter + col * len(actors) + 10
    out = []
    y = 14
    hh = 40
    for a in actors:
        out.append(f'<g class="actor"><rect x="{x[a]-col/2+8:.0f}" y="{y}" width="{col-16}" height="{hh}" rx="6"/>'
                   f'<text x="{x[a]:.0f}" y="{y+hh/2+4:.0f}" text-anchor="middle" font-size="13" font-weight="700">{html.escape(a)}</text></g>')
    y += hh + 22
    body = []
    problems = []
    for a_attrs, text in items:
        label = strip_tags(text)
        t = attr(a_attrs, "data-t")
        cls = attr(a_attrs, "class") or ""
        bad = "bad" in cls.split()
        dashed = "dashed" in cls.split() or attr(a_attrs, "data-style") == "dashed"
        note = attr(a_attrs, "data-note")
        if t:
            body.append(f'<text x="6" y="{y+4}" font-size="11" class="mono muted">{html.escape(t)}</text>')
        if note is not None:
            span = [n.strip() for n in note.split("|") if n.strip()] or actors[:1]
            if any(n not in x for n in span):
                problems.append(f"note actor not in data-actors: {note}")
                span = [n for n in span if n in x] or actors[:1]
            x0 = min(x[n] for n in span) - col / 2 + 14
            x1 = max(x[n] for n in span) + col / 2 - 14
            need = svg_text_width(label, 12) + 20
            if x1 - x0 < need:
                c = (x0 + x1) / 2
                x0, x1 = c - need / 2, c + need / 2
            x0 = max(x0, gutter)
            x1 = min(x1, width - 4)
            lines = wrap(label, max(10, int((x1 - x0 - 16) / 6.7)))
            h = 10 + 15 * len(lines)
            body.append(f'<g class="note"><rect x="{x0:.0f}" y="{y-12}" width="{x1-x0:.0f}" height="{h}" rx="4"/>')
            for i, ln in enumerate(lines):
                body.append(f'<text x="{(x0+x1)/2:.0f}" y="{y+3+i*15}" text-anchor="middle" font-size="12">{html.escape(ln)}</text>')
            body.append("</g>")
            y += h + 14
            continue
        fr, to = attr(a_attrs, "data-from"), attr(a_attrs, "data-to")
        if fr not in x or to not in x:
            problems.append(f"arrow actor not in data-actors: {fr} -> {to}")
            continue
        c = " bad" if bad else ""
        d = " dashed" if dashed else ""
        if fr == to:
            x0 = x[fr]
            body.append(f'<path class="arrow{c}{d}" d="M{x0:.0f},{y} h34 v16 h-30"/>'
                        f'<polygon class="ah{c}" points="{x0+2:.0f},{y+16} {x0+11:.0f},{y+11} {x0+11:.0f},{y+21}"/>')
            body.append(f'<text x="{x0+40:.0f}" y="{y+12}" font-size="12">{html.escape(label)}</text>')
            y += 36
            continue
        x0, x1 = x[fr], x[to]
        sgn = 1 if x1 > x0 else -1
        lines = wrap(label, max(14, int(abs(x1 - x0) / 6.6)))
        y += 14 * (len(lines) - 1)
        for i, ln in enumerate(lines):
            ly = y - 6 - (len(lines) - 1 - i) * 14
            body.append(f'<text x="{(x0+x1)/2:.0f}" y="{ly}" text-anchor="middle" font-size="12">{html.escape(ln)}</text>')
        body.append(f'<line class="arrow{c}{d}" x1="{x0:.0f}" y1="{y+4}" x2="{x1-sgn*9:.0f}" y2="{y+4}"/>'
                    f'<polygon class="ah{c}" points="{x1:.0f},{y+4} {x1-sgn*10:.0f},{y-1} {x1-sgn*10:.0f},{y+9}"/>')
        y += 36
    for a in actors:
        out.insert(0, f'<line class="life" x1="{x[a]:.0f}" y1="{14+hh}" x2="{x[a]:.0f}" y2="{y}"/>')
    height = y + 10
    svg = (f'<div class="svg-diagram"><svg viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" style="min-width:{min(width, 680):.0f}px" role="img" '
           f'aria-label="Sequence diagram: {html.escape(", ".join(actors))}" xmlns="http://www.w3.org/2000/svg">'
           + "".join(out) + "".join(body) + "</svg></div>")
    return svg, "; ".join(problems) or None


def wrap(text, n):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > n:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def render_ring(tag):
    try:
        n = int(attr(tag, "data-size") or 8)
        head = int(attr(tag, "data-head") or 0)
        tail = int(attr(tag, "data-tail") or 0)
    except ValueError:
        return None, "queue diagram with non-numeric attributes"
    if not (2 <= n <= 32) or not (0 <= head < n) or not (0 <= tail < n):
        return None, "queue diagram attributes out of range (size 2..32, head/tail < size)"
    label = attr(tag, "data-label") or "Queue"
    hl = attr(tag, "data-head-label") or "Head (consumer)"
    tl = attr(tag, "data-tail-label") or "Tail (producer)"
    full = set()
    i = head
    while i != tail:
        full.add(i)
        i = (i + 1) % n
    cell = 44 if n <= 16 else 30
    left = 20
    width = max(left + n * cell + 30, 560)
    y0 = 52
    parts = [f'<text x="{left}" y="18" font-size="13" font-weight="700">{html.escape(label)}</text>']
    for k in range(n):
        cx = left + k * cell
        parts.append(f'<rect class="slot{" full" if k in full else ""}" x="{cx}" y="{y0}" width="{cell-4}" height="34" rx="4"/>')
        parts.append(f'<text x="{cx+(cell-4)/2:.0f}" y="{y0+50}" text-anchor="middle" font-size="10" class="mono muted">{k}</text>')
        if k in full:
            parts.append(f'<text x="{cx+(cell-4)/2:.0f}" y="{y0+21}" text-anchor="middle" font-size="10" class="mono">cmd</text>')
    tx = left + tail * cell + (cell - 4) / 2
    hx = left + head * cell + (cell - 4) / 2
    parts.append(f'<polygon class="ptr" points="{tx:.0f},{y0-3} {tx-6:.0f},{y0-13} {tx+6:.0f},{y0-13}"/>'
                 f'<text x="{tx:.0f}" y="{y0-17}" text-anchor="middle" font-size="11" font-weight="700">{html.escape(tl)}</text>')
    parts.append(f'<polygon class="ptr2" points="{hx:.0f},{y0+56} {hx-6:.0f},{y0+66} {hx+6:.0f},{y0+66}"/>'
                 f'<text x="{hx:.0f}" y="{y0+80}" text-anchor="middle" font-size="11" font-weight="700">{html.escape(hl)}</text>')
    parts.append(f'<text x="{left}" y="{y0+100}" font-size="11" class="muted">{len(full)} of {n} slots in use; '
                 f'empty when head = tail, full when (tail + 1) mod {n} = head</text>')
    height = y0 + 110
    return (f'<div class="svg-diagram"><svg viewBox="0 0 {width} {height}" width="{width}" style="min-width:{min(width, 680)}px" role="img" '
            f'aria-label="{html.escape(label)}: {n} slots, head {head}, tail {tail}" xmlns="http://www.w3.org/2000/svg">'
            + "".join(parts) + "</svg></div>"), None


# ---------------------------------------------------------------- validation

class Checker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.problems = []
        self.ids = []
        self.in_svg = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a and a["id"]:
            self.ids.append(a["id"])
        if tag in VOID:
            return
        if tag == "svg":
            self.in_svg += 1
        self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a and a["id"]:
            self.ids.append(a["id"])

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag == "svg":
            self.in_svg = max(0, self.in_svg - 1)
        if not self.stack:
            self.problems.append(f"line {self.getpos()[0]}: stray </{tag}>")
            return
        if self.stack[-1][0] == tag:
            self.stack.pop()
            return
        # implicit closes allowed for p/li/td/th/tr/dt/dd/option/thead/tbody
        names = [t for t, _ in self.stack]
        if tag in names:
            while self.stack and self.stack[-1][0] != tag:
                t, ln = self.stack.pop()
                if t not in {"p", "li", "td", "th", "tr", "dt", "dd", "option", "thead", "tbody", "tfoot", "colgroup"}:
                    self.problems.append(f"line {ln}: <{t}> not closed before </{tag}> at line {self.getpos()[0]}")
            self.stack.pop()
        else:
            self.problems.append(f"line {self.getpos()[0]}: </{tag}> has no matching open tag")

    def finish(self):
        for t, ln in self.stack:
            if t not in {"p", "li", "td", "th", "tr", "dt", "dd", "option", "thead", "tbody"}:
                self.problems.append(f"line {ln}: <{t}> never closed")


def validate_fragment(pid, src, page_ids, glossary):
    errors, warns = [], []
    c = Checker()
    try:
        c.feed(src)
        c.close()
        c.finish()
    except Exception as e:  # noqa: BLE001
        errors.append(f"parse failure: {e}")
    errors += c.problems[:20]
    dup = {i for i in c.ids if c.ids.count(i) > 1}
    if dup:
        errors.append("duplicate ids: " + ", ".join(sorted(dup)[:10]))
    if re.search(r"<h1\b", src, re.I) and 'class="hero"' not in src:
        errors.append("<h1> found: the page title comes from the manifest; start headings at <h2>")
    for m in re.finditer(r"<pre\b[^>]*>", src):
        t = m.group(0)
        if has_class(t, "code") and (attr(t, "data-kind") not in CODE_KINDS):
            errors.append(f"<pre class=\"code\"> needs data-kind in {sorted(CODE_KINDS)}: {t[:80]}")
    for m in re.finditer(r"<div\b[^>]*>", src):
        t = m.group(0)
        if has_class(t, "trace") and attr(t, "data-kind") not in TRACE_KINDS:
            errors.append(f"<div class=\"trace\"> needs data-kind in {sorted(TRACE_KINDS)}: {t[:80]}")
    for m in re.finditer(r"<aside\b[^>]*>", src):
        t = m.group(0)
        if has_class(t, "callout"):
            kinds = [k for k in (attr(t, "class") or "").split() if k != "callout"]
            if not kinds or kinds[0] not in CALLOUTS:
                errors.append(f"callout type must be one of {sorted(CALLOUTS)}: {t[:80]}")
    for m in re.finditer(r'href="([^"#:]+)\.html(#[^"]*)?"', src):
        if m.group(1) not in page_ids:
            errors.append(f"link to unknown page: {m.group(0)}")
    for m in re.finditer(r'<span class="g" data-term="([^"]+)"', src):
        if m.group(1).lower() not in glossary:
            warns.append(f"glossary term not defined: {m.group(1)}")
    if re.search(r'<(script|link|img|iframe)\b[^>]*(src|href)="(https?:)?//', src, re.I):
        errors.append("external resource (script/link/img/iframe) found; the site must stay self-contained")
    if re.search(r"<script\b", src) and pid and not pid.startswith("tool-"):
        warns.append("inline <script> outside a tool page")
    words = len(strip_tags(src).split())
    return errors, warns, words


# ---------------------------------------------------------------- transform

def transform(p, src, glossary, problems):
    s = src

    # callouts
    def callout(m):
        tag = m.group(0)
        kinds = [k for k in (attr(tag, "class") or "").split() if k != "callout"]
        title = attr(tag, "data-title")
        k = kinds[0] if kinds else ""
        base = CALLOUTS.get(k, "Note")
        if title:
            icon = base.split(" ")[0]
            label = f"{icon} {html.escape(title)}"
        else:
            label = base
        return tag + f'<div class="callout-title">{label}</div>'
    s = re.sub(r'<aside\b[^>]*class="callout\b[^"]*"[^>]*>', callout, s)

    # teaching ladder rungs
    out, pos = [], 0
    for m in re.finditer(r'<div\b[^>]*class="rung"[^>]*>', s):
        if m.start() < pos:
            continue
        close_s, close_e = match_close(s, m.start(), "div")
        if close_s is None:
            problems.append("unclosed rung")
            continue
        r = (attr(m.group(0), "data-rung") or "").upper()
        label = attr(m.group(0), "data-label") or RUNGS.get(r, "")
        out.append(s[pos:m.start()])
        out.append(f'<div class="rung" data-rung="{r}"><div class="rung-label"><b>{r}</b>{html.escape(label)}</div>'
                   f'<div class="rung-body">{s[m.end():close_s]}</div></div>')
        pos = close_e
    out.append(s[pos:])
    s = "".join(out)

    # trace provenance
    def trace(m):
        tag = m.group(0)
        kind = attr(tag, "data-kind") or "conceptual"
        title = attr(tag, "data-title") or ""
        desc = TRACE_KINDS.get(kind, kind)
        bk = desc.split(" — ")[0]
        extra = f"<strong>{html.escape(title)}</strong>" if title else ""
        return (f'<div class="trace-block" data-kind="{kind}"><div class="trace-head"><span class="badge {kind}">{html.escape(bk)}</span>'
                f'{extra}<span>{html.escape(desc.split(" — ")[1] if " — " in desc else "")}</span></div>')
    s = re.sub(r'<div\b[^>]*class="trace"[^>]*>', trace, s)

    # code blocks
    def code(m):
        tag, inner = m.group(1), m.group(2)
        kind = attr(tag, "data-kind") or "pseudocode"
        lang = attr(tag, "data-lang") or ""
        title = attr(tag, "data-title") or ""
        head = (f'<div class="codeblock-head"><span class="badge {kind}">{CODE_KINDS.get(kind, kind)}</span>'
                f'<span class="lang">{html.escape(lang)}</span>{("<span>" + html.escape(title) + "</span>") if title else ""}</div>')
        return f'<div class="codeblock">{head}{tag}{inner}</pre></div>'
    s = re.sub(r'(<pre\b[^>]*class="code"[^>]*>)(.*?)</pre>', code, s, flags=re.S)

    # sequence diagrams
    def seq(m):
        svg, prob = render_seq(m.group(1), m.group(2))
        if prob:
            problems.append(prob)
        return svg or m.group(0)
    s = re.sub(r'(<ol\b[^>]*class="seq"[^>]*>)(.*?)</ol>', seq, s, flags=re.S)

    # queue rings
    def ring(m):
        svg, prob = render_ring(m.group(1))
        if prob:
            problems.append(prob)
        return svg or m.group(0)
    s = re.sub(r'(<div\b[^>]*class="ring"[^>]*>)\s*</div>', ring, s)

    # tables get a scroll wrapper
    out, pos = [], 0
    for m in re.finditer(r"<table\b", s):
        if m.start() < pos:
            continue
        before = s[max(0, m.start() - 40):m.start()]
        close_s, close_e = match_close(s, m.start(), "table")
        if close_s is None:
            problems.append("unclosed table")
            continue
        out.append(s[pos:m.start()])
        if 'class="table-wrap"' in before:
            out.append(s[m.start():close_e])
        else:
            out.append(f'<div class="table-wrap">{s[m.start():close_e]}</div>')
        pos = close_e
    out.append(s[pos:])
    s = "".join(out)

    # glossary terms
    def gterm(m):
        key = m.group(1).lower()
        return f'<a class="g" href="glossary.html#term-{key}" data-term="{key}">{m.group(2)}</a>'
    s = re.sub(r'<span class="g" data-term="([^"]+)">(.*?)</span>', gterm, s, flags=re.S)

    # headings: ids, numbering, anchors
    toc = []
    pre = sec_prefix(p)
    counters = [0, 0]
    used = set(re.findall(r'\bid="([^"]+)"', s))

    def heading(m):
        level, attrs_, inner = m.group(1), m.group(2), m.group(3)
        hid = attr(attrs_, "id")
        if not hid:
            hid = slug(inner)
            base, k = hid, 2
            while hid in used:
                hid = f"{base}-{k}"
                k += 1
            used.add(hid)
            attrs_ = attrs_ + f' id="{hid}"'
        if level == "2":
            counters[0] += 1
            counters[1] = 0
            num = f"{pre}{counters[0]}"
            toc.append((num, strip_tags(inner), hid))
        else:
            counters[1] += 1
            num = f"{pre}{counters[0]}.{counters[1]}" if counters[0] else ""
        nospan = "nonum" in (attr(attrs_, "class") or "")
        numhtml = "" if nospan or not num else f'<span class="secnum">{num}</span> '
        return f'<h{level}{attrs_}>{numhtml}<span class="ht">{inner}</span><a class="anchor" href="#{hid}" aria-label="Link to this section">#</a></h{level}>'
    s = re.sub(r"<h([23])((?:\s[^>]*)?)>(.*?)</h\1>", heading, s, flags=re.S)
    return s, toc


# ---------------------------------------------------------------- page assembly

def theme_boot():
    return ("<script>try{var t=localStorage.getItem('nvme-journey:theme')||"
            "(window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');"
            "document.documentElement.setAttribute('data-theme',t)}catch(e){}</script>")


def sidebar(pages, current_id, href=lambda pid: f"{pid}.html"):
    groups, order = {}, []
    for p in pages:
        if p["group_id"] not in groups:
            groups[p["group_id"]] = (p["group"], [])
            order.append(p["group_id"])
        groups[p["group_id"]][1].append(p)
    h = ['<nav class="sidebar" aria-label="Course contents"><div class="sb-tools">'
         '<button class="icon-btn" id="sb-expand" type="button">Expand all</button>'
         '<button class="icon-btn" id="sb-collapse" type="button">Collapse</button></div>']
    for gid in order:
        title, ps = groups[gid]
        is_open = any(p["id"] == current_id for p in ps) or gid == "front"
        h.append(f'<details{" open" if is_open else ""}><summary>{html.escape(title)}'
                 f'<span class="grp-count">{len(ps)}</span></summary><ol>')
        for p in ps:
            cur = ' class="current" aria-current="page"' if p["id"] == current_id else ""
            h.append(f'<li><a href="{href(p["id"])}" data-id="{p["id"]}"{cur}><span class="num">{nav_num(p)}</span>'
                     f'<span>{html.escape(p["title"])}</span></a></li>')
        h.append("</ol></details>")
    h.append("</nav>")
    return "".join(h)


def topbar():
    return ('<header class="topbar"><button class="icon-btn menu-btn" id="menu-toggle" type="button" aria-label="Open contents">☰</button>'
            '<a class="brand" href="index.html">NVMe <span>+</span> PCIe <span class="long">Engineering Journey</span></a>'
            '<span class="spacer"></span><span class="course-progress" id="course-progress"></span>'
            '<div class="search"><input id="search-input" type="search" placeholder="Search the course  ( / )" aria-label="Search the course" autocomplete="off">'
            '<div class="search-results" id="search-results" role="listbox"></div></div>'
            '<a class="icon-btn" href="book.html" title="Single-file edition for printing and offline reading" '
            'style="display:inline-flex;align-items:center;text-decoration:none">Book</a>'
            '<button class="icon-btn" id="theme-toggle" type="button" aria-label="Toggle dark mode">☾</button></header>'
            '<div class="read-progress"></div>')


def page_header(p, words):
    if p["id"] == "index":
        return ""
    label = page_label(p)
    mins = max(1, round(words / 200))
    return (f'<div class="crumbs"><a href="index.html">Home</a> / {html.escape(p["group"])}</div>'
            f'<h1 class="page-title"><span class="pt-num">{html.escape(label)}</span>{html.escape(p["title"])}</h1>'
            f'<div class="page-meta"><span class="badge level">{html.escape(p["kind"])}</span>'
            f'<span>≈ {mins} min read</span><span>·</span><span>Baseline: NVMe 2.4 specification set (August 2026)</span></div>')


def page_toc(toc):
    if len(toc) < 3:
        return ""
    items = "".join(f'<li><a href="#{hid}"><span class="tn">{html.escape(num)}</span>{html.escape(t)}</a></li>' for num, t, hid in toc)
    return (f'<details class="page-toc" open><summary>On this page ({len(toc)} sections)</summary><ol>{items}</ol></details>'
            '<div class="section-controls"><button class="icon-btn" id="expand-all" type="button">Expand all sections</button>'
            '<button class="icon-btn" id="collapse-all" type="button">Collapse all sections</button></div>')


def pager(pages, p):
    i = p["index"]
    prev = pages[i - 1] if i > 0 else None
    nxt = pages[i + 1] if i + 1 < len(pages) else None
    h = ['<nav class="pager" aria-label="Previous and next">']
    if prev:
        h.append(f'<a class="prev" href="{prev["id"]}.html"><span class="pl">← Previous · {html.escape(page_label(prev))}</span>{html.escape(prev["title"])}</a>')
    if nxt:
        h.append(f'<a class="next" href="{nxt["id"]}.html"><span class="pl">Next · {html.escape(page_label(nxt))} →</span>{html.escape(nxt["title"])}</a>')
    h.append("</nav>")
    return "".join(h)


def placeholder(p):
    return (f'<div class="placeholder"><p><strong>This page is being written.</strong></p>'
            f'<p>It covers <em>{html.escape(p["title"])}</em> (master prompt lines {html.escape(p.get("src", ""))}).</p></div>')


def glossary_html(glossary, pages_by_id):
    items = sorted(glossary.values(), key=lambda e: e["term"].lower())
    letters, h = [], []
    cur = None
    for e in items:
        L = e["term"][0].upper()
        if not L.isalpha():
            L = "#"
        if L != cur:
            cur = L
            letters.append(L)
            h.append(f'</dl><h2 id="letter-{L if L != "#" else "num"}" class="nonum">{L}</h2><dl class="glossary-list">')
        see = [s for s in e.get("see", []) if s in pages_by_id][:6]
        see_html = ""
        if see:
            see_html = '<div class="see">Learn more: ' + ", ".join(
                f'<a href="{s}.html">{html.escape(pages_by_id[s]["title"])}</a>' for s in see) + "</div>"
        exp = f' <span class="gx">— {html.escape(e["exp"])}</span>' if e.get("exp") else ""
        h.append(f'<dt id="term-{e["key"]}">{html.escape(e["term"])}{exp}</dt><dd>{e["def"]}{see_html}</dd>')
    nav = '<div class="glossary-letters">' + "".join(
        f'<a href="#letter-{L if L != "#" else "num"}">{L}</a>' for L in letters) + "</div>"
    filt = '<input class="glossary-filter" id="glossary-filter" type="search" placeholder="Filter terms…" aria-label="Filter glossary">'
    body = "".join(h)
    body = body[5:] if body.startswith("</dl>") else body
    return (f'<p>{len(items)} terms. Hover any dotted-underlined term anywhere in the course to see its definition.</p>'
            + filt + nav + '<div class="glossary-wrap">' + body + "</dl></div>")


def build_page(p, pages, content, toc, words, total):
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(p["title"])} · NVMe + PCIe Engineering Journey</title>
<meta name="description" content="{html.escape(page_label(p))}: {html.escape(p["title"])} — part of The NVMe + PCIe Engineering Journey, a free course from zero to senior protocol engineer.">
{theme_boot()}
<link rel="stylesheet" href="assets/academy.css">
</head>
<body data-page="{p["id"]}" data-total="{total}" data-search-src="assets/search-index.js">
<a href="#main" class="icon-btn" style="position:absolute;left:-9999px" onfocus="this.style.left='8px';this.style.top='60px'">Skip to content</a>
{topbar()}
{sidebar(pages, p["id"])}
<main class="main" id="main"><div class="page">
{page_header(p, words)}
{page_toc(toc)}
<article class="content">
{content}
</article>
<div class="complete-row"><button id="mark-complete" type="button">Mark this page complete</button><p>Progress is saved in this browser only.</p></div>
{pager(pages, p)}
<footer class="site-foot">The NVMe + PCIe Engineering Journey · Baseline: NVMe 2.4 specification set (ratified 2026-07-31, published 2026-08-04) and PCI Express Base Specification concepts. Synthetic traces are labelled; always confirm normative details in the official specifications.</footer>
</div></main>
<button class="icon-btn to-top" type="button" aria-label="Back to top">↑</button>
<script src="assets/glossary.js"></script>
<script src="assets/academy.js"></script>
</body>
</html>
"""


def book_rewrite(pid, content):
    content = re.sub(r'\bid="([^"]+)"', lambda m: f'id="{pid}--{m.group(1)}"', content)
    content = re.sub(r'href="#([^"]+)"', lambda m: f'href="#{pid}--{m.group(1)}"', content)

    def link(m):
        target, frag = m.group(1), m.group(2)
        return f'href="#{target}--{frag[1:]}"' if frag else f'href="#{target}"'
    content = re.sub(r'href="([a-z0-9-]+)\.html(#[^"]*)?"', link, content)
    return content


def search_entries(p, content):
    entries = []
    parts = re.split(r'(<h2\b[^>]*>.*?</h2>)', content, flags=re.S)
    head, anchor = p["title"], ""
    buf = parts[0]
    chunks = []
    for i in range(1, len(parts), 2):
        chunks.append((head, anchor, buf))
        h = parts[i]
        anchor = attr(h, "id") or ""
        head = strip_tags(re.sub(r'<span class="secnum">.*?</span>|<a class="anchor".*?</a>', "", h))
        buf = parts[i + 1] if i + 1 < len(parts) else ""
    chunks.append((head, anchor, buf))
    for head, anchor, b in chunks:
        text = strip_tags(b)
        if not text and not anchor:
            continue
        seen, uniq = set(), []
        for w in re.findall(r"[a-z0-9][a-z0-9_./-]+", text.lower()):
            if w not in seen:
                seen.add(w)
                uniq.append(w)
        entries.append({"p": p["id"], "t": p["title"], "tl": p["title"].lower(), "h": head, "hl": head.lower(),
                        "a": anchor, "s": text[:240], "l": " ".join(uniq)[:4000]})
    return entries


def main(argv):
    check_only = "--check" in argv
    strict = "--strict" in argv
    only = [a for a in argv if not a.startswith("--")]
    m, pages = load_manifest()
    by_id = {p["id"]: p for p in pages}
    glossary = load_glossary()
    page_ids = set(by_id)
    total_err = 0
    report = []
    missing = []

    sources = {}
    for p in pages:
        f = SRC / "pages" / f'{p["id"]}.html'
        if f.exists():
            sources[p["id"]] = f.read_text(encoding="utf-8")
        else:
            missing.append(p["id"])

    for pid, src in sources.items():
        if only and pid not in only:
            continue
        errors, warns, words = validate_fragment(pid, src, page_ids, glossary)
        if errors or warns:
            report.append((pid, errors, warns))
        total_err += len(errors)
    for pid, errors, warns in report:
        for e in errors:
            print(f"ERROR {pid}: {e}")
        for w in warns[:15]:
            print(f"warn  {pid}: {w}")
        if len(warns) > 15:
            print(f"warn  {pid}: … {len(warns) - 15} more warnings")
    if check_only:
        checked = [p for p in (only or sources)]
        print(f"checked {len(checked)} page(s): {total_err} error(s); {len(missing)} page(s) not written yet")
        return 1 if total_err else 0

    if strict and (total_err or missing):
        print(f"strict: {total_err} errors, missing pages: {missing}")
        return 1

    OUT.mkdir(exist_ok=True)
    (OUT / "assets").mkdir(exist_ok=True)
    for f in (SRC / "assets").iterdir():
        if f.is_file():
            shutil.copy2(f, OUT / "assets" / f.name)
    gl_js = {k: {"t": e["term"], "x": e.get("exp", ""), "d": e["def"]} for k, e in glossary.items()}
    gl_src = "window.NVME_GLOSSARY=" + json.dumps(gl_js, ensure_ascii=False, separators=(",", ":")) + ";\n"
    (OUT / "assets" / "glossary.js").write_text(gl_src, encoding="utf-8")

    search, book_parts, total_words = [], [], 0
    for p in pages:
        src = sources.get(p["id"])
        problems = []
        if p["id"] == "glossary":
            src = (src or "") + glossary_html(glossary, by_id)
        if src is None:
            content, toc, words = placeholder(p), [], 0
        else:
            content, toc = transform(p, src, glossary, problems)
            words = len(strip_tags(src).split())
        for pr in problems:
            print(f"ERROR {p['id']}: {pr}")
        total_words += words
        (OUT / f'{p["id"]}.html').write_text(build_page(p, pages, content, toc, words, len(pages)), encoding="utf-8")
        search += search_entries(p, content)
        book_parts.append((p, content, words))

    # cross-page anchor check (ids only exist after transform, which adds heading ids)
    ids_by_page = {p["id"]: set(re.findall(r'\bid="([^"]+)"', c)) for p, c, _ in book_parts}
    bad_anchors = 0
    for p, content, _ in book_parts:
        for m in re.finditer(r'href="(?:([a-z0-9-]+)\.html)?#([^"]+)"', content):
            target = m.group(1) or p["id"]
            if target in ids_by_page and sources.get(target) is not None and m.group(2) not in ids_by_page[target]:
                bad_anchors += 1
                if bad_anchors <= 60:
                    print(f"warn  {p['id']}: anchor not found: {m.group(1) or ''}#{m.group(2)}")
    if bad_anchors:
        print(f"{bad_anchors} broken anchor link(s)")

    idx_src = "window.NVME_SEARCH=" + json.dumps(search, ensure_ascii=False, separators=(",", ":")) + ";\n"
    (OUT / "assets" / "search-index.js").write_text(idx_src, encoding="utf-8")

    # single-file book
    css = (SRC / "assets" / "academy.css").read_text(encoding="utf-8")
    js = (SRC / "assets" / "academy.js").read_text(encoding="utf-8")
    body = []
    for p, content, words in book_parts:
        hdr = page_header(p, words).replace('<a href="index.html">Home</a> / ', "")
        if p["id"] == "index":
            hdr = ""
        body.append(f'<section class="book-page" id="{p["id"]}" data-page-id="{p["id"]}">{hdr}<div class="content">{book_rewrite(p["id"], content)}</div></section><hr>')
    book_nav = sidebar(pages, "", href=lambda pid: f"#{pid}")
    book = f"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The NVMe + PCIe Engineering Journey — Complete Book</title>
{theme_boot()}
<style>{css}</style>
</head>
<body data-page="book" data-mode="book" data-total="0">
<header class="topbar"><button class="icon-btn menu-btn" id="menu-toggle" type="button" aria-label="Open contents">☰</button>
<a class="brand" href="#index">NVMe <span>+</span> PCIe <span class="long">Engineering Journey · Complete Book</span></a><span class="spacer"></span>
<div class="search"><input id="search-input" type="search" placeholder="Search the book  ( / )" aria-label="Search the book" autocomplete="off"><div class="search-results" id="search-results"></div></div>
<button class="icon-btn" id="theme-toggle" type="button" aria-label="Toggle dark mode">☾</button></header><div class="read-progress"></div>
{book_nav}
<main class="main" id="main"><div class="page"><article class="content">
{"".join(body)}
</article></div></main>
<button class="icon-btn to-top" type="button" aria-label="Back to top">↑</button>
<script>{gl_src}</script>
<script>{idx_src}</script>
<script>{js}</script>
</body>
</html>
"""
    (OUT / "book.html").write_text(book, encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    written = len(pages) - len(missing)
    print(f"built {len(pages)} pages ({written} written, {len(missing)} placeholders), "
          f"{total_words:,} words, {len(glossary)} glossary terms, {len(search)} search sections")
    if missing:
        print("missing:", " ".join(missing))
    return 1 if (strict and total_err) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
