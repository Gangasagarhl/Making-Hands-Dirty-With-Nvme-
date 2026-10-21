"""Self-contained report.html (no network, light/dark aware): summary, findings grouped by category, each with the
evidence rows, the command's stage timeline, cause, fault side, atlas card text and an nvme-cli reproduction."""
from __future__ import annotations

import html
import json

from .cards import CATEGORIES

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--mut:#62625d;--line:#e2e1dc;--card:#ffffff;--acc:#2f5f8f;--err:#b3261e;--warn:#9a6700;
--info:#2f5f8f;--code:#f3f2ee;--s1:#5b8def;--s2:#e0a33a;--s3:#47a37b;--s4:#a77bd1;--s5:#d1675b;--s6:#7aa6b8;--s7:#9b9b93}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#161615;--fg:#e8e7e2;--mut:#a3a29b;--line:#34332f;
--card:#1f1f1d;--acc:#8db4e2;--err:#f2827a;--warn:#e3b341;--info:#8db4e2;--code:#2a2a27}}
:root[data-theme="dark"]{--bg:#161615;--fg:#e8e7e2;--mut:#a3a29b;--line:#34332f;--card:#1f1f1d;--acc:#8db4e2;--err:#f2827a;
--warn:#e3b341;--info:#8db4e2;--code:#2a2a27}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 10px;
border-bottom:1px solid var(--line);padding-bottom:4px}h3{font-size:15px;margin:0}.mut{color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.tile{background:var(--card);
border:1px solid var(--line);border-radius:8px;padding:10px 12px}.tile b{display:block;font-size:20px}
table{border-collapse:collapse;width:100%;font-size:12.5px}th,td{border-bottom:1px solid var(--line);padding:4px 6px;
text-align:left;vertical-align:top}th{color:var(--mut);font-weight:600}.wrap{overflow-x:auto}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}pre{background:var(--code);
padding:8px 10px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;word-break:break-all;margin:6px 0}
details.f{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0;padding:0}
details.f>summary{cursor:pointer;padding:9px 12px;list-style:none;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
details.f>summary::-webkit-details-marker{display:none}.body{padding:4px 14px 14px;border-top:1px solid var(--line)}
.sev{font-size:11px;font-weight:700;text-transform:uppercase;padding:1px 6px;border-radius:4px;border:1px solid currentColor}
.error{color:var(--err)}.warning{color:var(--warn)}.info{color:var(--info)}.card{font-weight:700;color:var(--acc)}
.kv{display:grid;grid-template-columns:170px 1fr;gap:3px 12px;margin:8px 0}.kv div:nth-child(odd){color:var(--mut)}
tr.focus td{background:color-mix(in srgb,var(--acc) 14%,transparent)}.bar{display:flex;height:18px;border-radius:4px;overflow:hidden;
border:1px solid var(--line);margin:6px 0 2px}.bar span{display:block;height:100%}.legend{display:flex;flex-wrap:wrap;gap:4px 12px;
font-size:11.5px;color:var(--mut)}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;
vertical-align:-1px}.top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
button{font:inherit;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.filters input{font:inherit;padding:4px 8px;border:1px solid var(--line);
border-radius:6px;background:var(--card);color:var(--fg);min-width:220px}.pill{font-size:11.5px;color:var(--mut)}
@media (max-width:640px){.kv{grid-template-columns:1fr}.kv div:nth-child(odd){margin-top:6px}}
"""

JS = """
const root=document.documentElement;const tb=document.getElementById('theme');
tb.onclick=()=>{const d=root.dataset.theme==='dark'||(!root.dataset.theme&&matchMedia('(prefers-color-scheme: dark)').matches);
root.dataset.theme=d?'light':'dark';};
const q=document.getElementById('q');const sevs=document.querySelectorAll('[data-sev]');
function apply(){const t=q.value.toLowerCase();const on=[...document.querySelectorAll('.sevbox')].filter(x=>x.checked).map(x=>x.value);
document.querySelectorAll('details.f').forEach(d=>{const ok=on.includes(d.dataset.sev)&&(!t||d.dataset.s.includes(t));
d.style.display=ok?'':'none';});}
q.oninput=apply;document.querySelectorAll('.sevbox').forEach(x=>x.onchange=apply);
document.getElementById('openall').onclick=()=>document.querySelectorAll('details.f').forEach(d=>{if(d.style.display!=='none')d.open=true});
document.getElementById('closeall').onclick=()=>document.querySelectorAll('details.f').forEach(d=>d.open=false);
"""

STAGE_COLORS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)", "var(--s7)"]


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def fmt_ts(t):
    return "" if t is None else f"{t:.9f} s"


def stage_bar(st: dict) -> str:
    items = [(k, v) for k, v in st.items() if v is not None and not k.startswith("total") and v > 0]
    tot = sum(v for _, v in items)
    if not items or tot <= 0:
        return ""
    segs, leg = [], []
    for i, (k, v) in enumerate(items):
        c = STAGE_COLORS[i % len(STAGE_COLORS)]
        segs.append(f'<span style="width:{max(0.5, v / tot * 100):.2f}%;background:{c}" title="{e(k)}: {v:.3f} us"></span>')
        leg.append(f'<span><i style="background:{c}"></i>{e(k)} {v:,.3f} us</span>')
    total = st.get("total_doorbell_to_cqe")
    return (f'<div class="bar">{"".join(segs)}</div><div class="legend">{"".join(leg)}'
            + (f'<span><b>total doorbell→CQE {total:,.3f} us</b></span>' if total else "") + "</div>")


def rows_table(rows, extra_col=None):
    if not rows:
        return '<p class="mut">Rows not extracted (pass 2 disabled or row cap reached).</p>'
    keys = []
    for r in rows:
        for k in r:
            if k not in keys and k not in ("focus", "role", "row"):
                keys.append(k)
    pref = ["Packet", "Time", "Dir", "TLP", "DLLP", "OS", "Event"]
    keys = [k for k in pref if k in keys] + [k for k in keys if k not in pref]
    head = "<tr><th>Row</th>" + ("<th>Role</th>" if extra_col else "") + "".join(f"<th>{e(k)}</th>" for k in keys) + "</tr>"
    body = []
    for r in rows:
        cls = ' class="focus"' if r.get("focus") else ""
        body.append(f"<tr{cls}><td>{e(r.get('row'))}</td>" + (f"<td>{e(r.get('role'))}</td>" if extra_col else "")
                    + "".join(f"<td><code>{e(r.get(k, ''))}</code></td>" for k in keys) + "</tr>")
    return f'<div class="wrap"><table>{head}{"".join(body)}</table></div>'


def finding_html(f) -> str:
    sev = f["severity"]
    head = (f'<span class="sev {sev}">{e(sev)}</span><span class="card">{e(f["card_id"])}</span>'
            f'<span>{e(f["card_name"])}</span><span class="pill">pkt {e(f.get("pkt"))} · row {e(f["row"])} · '
            f'{e(fmt_ts(f.get("timestamp")))} · {e(f["fault_side"])}</span>')
    kv = [("Where", f"packet {f.get('pkt')}, row {f['row']}, t = {fmt_ts(f.get('timestamp'))}"),
          ("What", f["what"]), ("Rule", f"{f['rule']} · category {f['category']} · atlas: {f['visibility']}"),
          ("Likely fault side", f["fault_side"])]
    if f.get("sqid") is not None:
        kv.append(("Command", f"{f.get('opcode_name')} SQ{f.get('sqid')} CID 0x{f.get('cid', 0) & 0xFFFF:X} NSID {f.get('nsid')}"
                              + (f" SLBA {f.get('slba')} NLB {f.get('nlb')}" if f.get("slba") is not None else "")
                              + f" · {f.get('bytes')} B"))
        qd = f.get("queue_depth_at_submit") or {}
        kv.append(("Queue depth at submit", f"SQ {qd.get('sq')} · all queues {qd.get('all_queues')}"))
        ls = f.get("link_state_at_submit")
        if ls:
            kv.append(("Link at submit", f"{ls.get('ltssm')} {ls.get('speed_gts') or '?'} GT/s x{ls.get('width') or '?'}"))
    ls = f.get("link_state") or {}
    kv.append(("Link at finding", f"{ls.get('ltssm')} {ls.get('speed_gts') or '?'} GT/s x{ls.get('width') or '?'} · last event "
                                  f"'{ls.get('last_event') or '-'}' · recoveries {ls.get('recoveries_so_far')} · replays {ls.get('replays_so_far')}"))
    if f.get("related_cards"):
        kv.append(("Related cards", ", ".join(f["related_cards"])))
    if f.get("metrics"):
        kv.append(("Metrics", json.dumps(f["metrics"], default=str)[:1500]))
    parts = ['<div class="kv">' + "".join(f"<div>{e(k)}</div><div>{e(v)}</div>" for k, v in kv) + "</div>"]
    if f.get("stage_breakdown_us"):
        parts.append("<h3>Command timeline (stages)</h3>" + stage_bar(f["stage_breakdown_us"]))
    if f.get("command_packets"):
        cp = ", ".join(f"{k} row {v[0]}" for k, v in f["command_packets"].items() if v and v[0])
        parts.append(f'<p class="mut">Lifecycle packets: {e(cp)}</p>')
    parts.append("<h3>Evidence packets</h3>" + rows_table(f.get("evidence_rows"), extra_col=True))
    parts.append(f"<h3>Context (±rows around packet {e(f.get('pkt'))})</h3>" + rows_table(f.get("context_rows")))
    parts.append("<h3>Why (atlas)</h3><p>" + e(f["why"]) + "</p>")
    if f.get("typical_causes"):
        parts.append("<p><b>Typical causes:</b> " + e(f["typical_causes"]) + "</p>")
    parts.append("<p><b>How to confirm:</b> " + e(f["how_to_confirm"]) + "</p>")
    if f.get("extra_evidence_needed"):
        parts.append("<p><b>Partly visible — extra evidence needed:</b> " + e(f["extra_evidence_needed"]) + "</p>")
    if f.get("spec_basis"):
        parts.append('<p class="mut"><b>Spec basis (atlas wording):</b> ' + e(f["spec_basis"]) + "</p>")
    rp = f.get("repro")
    if rp:
        txt = []
        if rp.get("nvme_cli"):
            txt.append("# exact SQE as nvme-cli passthru\n" + rp["nvme_cli"])
        if rp.get("friendly"):
            txt.append("# equivalent\n" + rp["friendly"])
        if rp.get("fio_load"):
            txt.append("# background load at the same queue depth\n" + rp["fio_load"])
        if rp.get("note"):
            txt.append("# " + rp["note"])
        if rp.get("safety"):
            txt.append("# " + rp["safety"])
        parts.append("<h3>Reproduce</h3><pre>" + e("\n".join(txt)) + "</pre>")
        if rp.get("sqe_hex"):
            parts.append(f'<p class="mut">SQE bytes: <code>{e(rp["sqe_hex"])}</code></p>')
    if f.get("test_to_provoke"):
        parts.append("<p><b>Test to provoke it (atlas):</b> " + e(f["test_to_provoke"]) + "</p>")
    if f.get("preceding_commands"):
        pc = "; ".join(f"{c['opcode']} SQ{c['sqid']} CID 0x{c['cid'] & 0xFFFF:X} row {c['doorbell_row']}" for c in f["preceding_commands"])
        parts.append(f'<p class="mut"><b>Preceding commands:</b> {e(pc)}</p>')
    search = " ".join(str(x) for x in (f["card_id"], f["card_name"], f["what"], f["rule"], f.get("opcode_name"), f.get("pkt"))).lower()
    return (f'<details class="f" data-sev="{e(sev)}" data-s="{e(search)}"><summary>{head}</summary>'
            f'<div class="body">{"".join(parts)}</div></details>')


def write_report(path, summary, findings, max_detail=3000):
    s = summary
    sev = s["findings"]["by_severity"]
    tiles = [("rows", f"{s['rows']:,}"), ("file", f"{s['file_bytes'] / 1e9:.3f} GB"), ("trace time", f"{s['trace_duration_s']:.6f} s"),
             ("NVMe commands", f"{s['nvme']['commands_completed']:,}"), ("errors", sev.get("error", 0)),
             ("warnings", sev.get("warning", 0)), ("info", sev.get("info", 0)), ("throughput", f"{s['rows_per_s']:,.0f} rows/s")]
    H = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>PEX Trace Findings</title><style>{CSS}</style></head><body><main>",
         "<div class='top'><div><h1>PEX trace findings</h1>"
         f"<div class='mut'>{e(s['file'])} · pex_checker {e(s['version'])} · {e(s['generated'])}</div></div>"
         "<button id='theme'>Light / dark</button></div>",
         "<div class='grid' style='margin-top:14px'>" + "".join(f"<div class='tile'><span class='mut'>{e(k)}</span><b>{e(v)}</b></div>"
                                                               for k, v in tiles) + "</div>"]
    if s.get("schema", {}).get("missing_features"):
        H.append("<h2>Missing columns</h2><ul>" + "".join(
            f"<li>{e(m['feature'])}: missing {e(', '.join(m['missing_columns']))} — {e(m['effect'])}</li>"
            for m in s["schema"]["missing_features"]) + "</ul>")
    # learned config
    lc = s["learned"]
    H.append("<h2>What the decoder learned from the trace</h2><div class='wrap'><table>" + "".join(
        f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in lc.items()) + "</table></div>")
    # findings by card
    H.append("<h2>Findings by card</h2><div class='wrap'><table><tr><th>Card</th><th>Name</th><th>Category</th><th>Worst</th>"
             "<th>Count</th><th>Side</th><th>First packet</th></tr>" + "".join(
        f"<tr><td class='card'>{e(r['card'])}</td><td>{e(r['name'])}</td><td>{e(r['category'])}</td>"
        f"<td class='{e(r['severity'])}'>{e(r['severity'])}</td><td>{r['count']}</td><td>{e(r['side'])}</td><td>{e(r['first_pkt'])}</td></tr>"
        for r in s["findings"]["by_card"]) + "</table></div>")
    # latency
    lat = s["nvme"]["latency_by_opcode_us"]
    if lat:
        H.append("<h2>Command latency (doorbell → CQE)</h2><div class='wrap'><table><tr><th>Opcode</th><th>n</th><th>p50 us</th>"
                 "<th>p99 us</th><th>p99.9 us</th><th>max us</th></tr>" + "".join(
            f"<tr><td>{e(k)}</td><td>{v['n']}</td><td>{v.get('p50_us')}</td><td>{v.get('p99_us')}</td><td>{v.get('p999_us')}</td>"
            f"<td>{v.get('max_us')}</td></tr>" for k, v in lat.items()) + "</table></div>")
    lk = s["link"]
    H.append("<h2>Link</h2><div class='kv'>" + "".join(f"<div>{e(k)}</div><div>{e(v)}</div>" for k, v in lk.items()
                                                       if k != "events_tail") + "</div>")
    # findings
    H.append("<h2>Findings</h2><div class='filters'><input id='q' placeholder='filter: card, opcode, text, packet'>"
             "<label><input type='checkbox' class='sevbox' value='error' checked> errors</label>"
             "<label><input type='checkbox' class='sevbox' value='warning' checked> warnings</label>"
             "<label><input type='checkbox' class='sevbox' value='info' checked> info</label>"
             "<button id='openall'>Expand all</button><button id='closeall'>Collapse all</button></div>")
    order = {"error": 0, "warning": 1, "info": 2}
    shown = 0
    for cat in CATEGORIES:
        fs = [f for f in findings if f["category"] == cat]
        if not fs:
            continue
        fs.sort(key=lambda f: (order.get(f["severity"], 3), f["row"]))
        H.append(f"<h2 style='font-size:16px'>{e(cat)} <span class='mut'>({len(fs)})</span></h2>")
        for f in fs:
            if shown >= max_detail:
                break
            H.append(finding_html(f))
            shown += 1
    if shown < len(findings):
        H.append(f"<p class='mut'>{len(findings) - shown} more findings are in findings.jsonl (detail cap {max_detail}).</p>")
    sup = s["findings"].get("suppressed_by_card")
    if sup:
        H.append("<p class='mut'>Suppressed repeats (per-card cap): " + e(", ".join(f"{k} ×{v}" for k, v in sup.items())) + "</p>")
    H.append("<h2>Limits of a single-link trace</h2><p class='mut'>Everything here is derived from the packets on the probed link. "
             "Device internals (media, FTL, caches), host memory that never crosses the link, host-side timers and logs, other links "
             "and anything outside the capture window are not visible. Timeouts are absence-plus-time; check capture gaps (X-30) "
             "before trusting a missing completion. Thresholds are configurable defaults, listed in summary.json.</p>")
    H.append(f"<script>{JS}</script></main></body></html>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(H))
