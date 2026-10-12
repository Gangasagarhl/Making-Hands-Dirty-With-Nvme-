#!/usr/bin/env python3
"""Assemble book/src/*.html fragments into one self-contained HTML book."""
import glob, html, os, re, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "PEX_Trace_Error_Atlas.html")

def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()

files = sorted(glob.glob(os.path.join(HERE, "src", "*.html")))
content = "\n".join(open(f, encoding="utf-8").read() for f in files)


# Auto-generate the master catalog from error cards (replaces <!--CATALOG--> in a fragment).
cards=[]
pexv=[]
for m in re.finditer(r'<div class="err" id="([^"]+)">\s*<div class="err-head">(.*?)</div>', content, re.S):
    head=m.group(2)
    eid=re.search(r'class="err-id">(.*?)<',head); name=re.search(r'<b>(.*?)</b>',head,re.S)
    rep=re.search(r'class="rep (\w+)">(.*?)<',head); lyr=re.search(r'class="lyr">(.*?)<',head)
    pexm=re.search(r'class="pex (yes|part|no)">(.*?)<', content[m.end():m.end()+4000])
    pexv.append((pexm.group(1), strip_tags(pexm.group(2))) if pexm else ('',''))
    cards.append((m.group(1), strip_tags(eid.group(1)) if eid else '', strip_tags(name.group(1)) if name else m.group(1),
                  rep.group(1) if rep else '', strip_tags(rep.group(2)) if rep else '', strip_tags(lyr.group(1)) if lyr else ''))
rows=''.join(f'<tr data-rep="{c[3]}"><td class="mono">{html.escape(c[1])}</td><td><a href="#{c[0]}">{html.escape(c[2])}</a></td><td>' + (f'<span class="pex {p[0]}">{html.escape(p[1])}</span>' if p[0] else '') + f'</td><td><span class="rep {c[3]}">{html.escape(c[4])}</span></td><td>{html.escape(c[5])}</td></tr>' for c,p in zip(cards,pexv))
pc={k:sum(1 for p in pexv if p[0]==k) for k in ['yes','part','no']}
counts={k:sum(1 for c in cards if c[3]==k) for k in ['reported','timeout','silent','degraded']}
summary=f'<p><b>{len(cards)}</b> failure modes: {counts["reported"]} reported with an error code or log, {counts["timeout"]} caught only by a timer, {counts["silent"]} silent (no error anywhere), {counts["degraded"]} degraded but "working". In a PEX trace: <b>{pc["yes"]}</b> visible, <b>{pc["part"]}</b> partly visible, <b>{pc["no"]}</b> not visible at all.</p>'
content=content.replace('<!--CATALOG-->', summary+'<div class="table-wrap"><table><thead><tr><th>ID</th><th>Failure</th><th>In a PEX trace</th><th>How it shows</th><th>Layer</th></tr></thead><tbody>'+rows+'</tbody></table></div>')

# Resolve part-anchor aliases: agents used both "part-xvii" and "p6-part-xvii".
ids = set(re.findall(r'\bid="([^"]+)"', content))
alias = {}
for i in ids:
    m = re.fullmatch(r"p\d+-(part-[a-z]+)", i)
    if m and m.group(1) not in ids:
        alias[m.group(1)] = i
content = re.sub(r'href="#(part-[a-z]+)"', lambda m: f'href="#{alias.get(m.group(1), m.group(1))}"', content)

# Collect parts and chapters in document order.
pat = re.compile(r'<section\s+class="(part|chapter)"[^>]*\bid="([^"]+)"[^>]*>', re.I)
items = []
for m in pat.finditer(content):
    kind, sid = m.group(1), m.group(2)
    tail = content[m.end():m.end() + 6000]
    hm = re.search(r"<h1[^>]*>(.*?)</h1>" if kind == "part" else r"<h2[^>]*>(.*?)</h2>", tail, re.S)
    title = strip_tags(hm.group(1)) if hm else sid
    if kind == "part":
        num = re.search(r'<div class="part-num">(.*?)</div>', tail, re.S)
        if num:
            title = strip_tags(num.group(1)) + " — " + title
    items.append((kind, sid, re.sub(r"\s+", " ", title)))

chapters = [i for i in items if i[0] == "chapter"]

# Prev/next navigation, inserted after each chapter's <h2>; the page script moves it to the end.
def add_nav(m):
    sid = m.group(2)
    idx = next(i for i, c in enumerate(chapters) if c[1] == sid)
    prev = chapters[idx - 1] if idx > 0 else None
    nxt = chapters[idx + 1] if idx + 1 < len(chapters) else None
    nav = '<div class="chapter-nav">'
    nav += f'<a href="#{prev[1]}">← {html.escape(prev[2])}</a>' if prev else "<span></span>"
    nav += f'<a href="#{nxt[1]}">{html.escape(nxt[2])} →</a>' if nxt else "<span></span>"
    nav += "</div>"
    end = content.find("</h2>", m.end())
    return m.group(0), end + 5, nav

inserts = []
for m in re.finditer(r'<section\s+class="(chapter)"[^>]*\bid="([^"]+)"[^>]*>', content):
    _, pos, nav = add_nav(m)
    inserts.append((pos, nav))
for pos, nav in sorted(inserts, reverse=True):
    content = content[:pos] + nav + content[pos:]

# TOC
toc, open_part = [], False
for kind, sid, title in items:
    if kind == "part":
        if open_part:
            toc.append("</details>")
        toc.append(f'<details><summary><a href="#{sid}" style="display:inline;padding:0">{html.escape(title)}</a></summary>')
        open_part = True
    else:
        cls = "" if open_part else ' class="solo"'
        toc.append(f'<a{cls} href="#{sid}"><span>{html.escape(title)}</span><span class="done">✓</span></a>')
if open_part:
    toc.append("</details>")

text_words = len(strip_tags(content).split())
stats = f"{len(chapters)} chapters · ~{text_words // 1000}k words"
shell = open(os.path.join(HERE, "shell.html"), encoding="utf-8").read()
page = (shell.replace("<!--TOC-->", "\n".join(toc))
             .replace("<!--CONTENT-->", content)
             .replace("<!--DATE-->", datetime.date.today().strftime("%B %Y"))
             .replace("<!--STATS-->", stats))
open(OUT, "w", encoding="utf-8").write(page)
all_ids = set(re.findall(r'\bid="([^"]+)"', page))
broken = sorted({h for h in re.findall(r'href="#([^"]+)"', page) if h not in all_ids})
print(f"broken internal links ({len(broken)}): {broken[:60]}")
print(f"wrote {OUT}: {len(page)/1e6:.2f} MB, {len(files)} fragments, {len(items)} toc items, {stats}")
