#!/usr/bin/env python
"""
token_budget.py - what would it cost to send a whole analyzer CSV straight to an LLM?

Reads the CSV once (streaming, constant memory, a 5 GB file takes about a minute), measures how many tokens
it contains with a real tokenizer on a random sample of rows, and computes for several context sizes:
  * how many LLM calls are needed to read the file once (with overlap between chunks)
  * how many more calls are needed to merge the per-chunk answers (hierarchical reduce)
  * how long that takes (per-call latency model, parallel requests, tokens-per-minute rate limit)
  * optionally what it costs (if you give prices per million tokens)
and compares it with the pipeline in this project (deterministic pass + evidence windows).

It writes a self-contained HTML report written for a manager, plus a JSON file with every number.

Examples
  python tools/token_budget.py --csv D:/traces/export.csv
  python tools/token_budget.py --csv D:/traces/export.csv --contexts 28000 100000 128000 --parallel 4 --tpm 1000000
  python tools/token_budget.py --csv D:/traces/export.csv --hf-tokenizer Qwen/Qwen2.5-7B-Instruct   # exact Qwen tokenizer
  python tools/token_budget.py --csv D:/traces/export.csv --price-in 0.4 --price-out 1.2              # USD per 1M tokens
  python tools/token_budget.py --csv D:/traces/export.csv --job-dir data/jobs/<job>                   # use a real pipeline run for comparison

Tokenizer choice (first that works): --hf-tokenizer (exact for Qwen) > tiktoken cl100k_base > character heuristic.
The tokenizer only changes the estimate by roughly +-20 %; the conclusion does not depend on it.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import random
import statistics
import sys
import time

# ----------------------------------------------------------------------------------------- tokenizer
class Tokenizer:
    def __init__(self, hf_name: str | None):
        self.name = "heuristic (3.0 characters per token)"
        self._enc = None
        if hf_name:
            try:
                from transformers import AutoTokenizer  # type: ignore
                tok = AutoTokenizer.from_pretrained(hf_name)
                self._enc = lambda s: len(tok.encode(s, add_special_tokens=False))
                self.name = f"Hugging Face tokenizer {hf_name} (exact)"
                return
            except Exception as e:  # noqa: BLE001
                print(f"[warn] could not load {hf_name}: {e}; falling back", file=sys.stderr)
        try:
            import tiktoken  # type: ignore
            enc = tiktoken.get_encoding("cl100k_base")
            self._enc = lambda s: len(enc.encode(s, disallowed_special=()))
            self.name = "tiktoken cl100k_base (close to Qwen and Claude tokenizers, within ~20 %)"
        except Exception:  # noqa: BLE001
            self._enc = None

    def count(self, s: str) -> int:
        if self._enc is None:
            return max(1, round(len(s) / 3.0))
        return self._enc(s)


# ----------------------------------------------------------------------------------------- measuring the file
def scan_file(path: str, block: int = 64 << 20) -> tuple[int, int]:
    """Exact byte and line count, streaming."""
    size = os.path.getsize(path)
    lines = 0
    t0 = time.time()
    with open(path, "rb") as f:
        done = 0
        while True:
            b = f.read(block)
            if not b:
                break
            lines += b.count(b"\n")
            done += len(b)
            if time.time() - t0 > 2:
                print(f"\r  scanning {done / size * 100:5.1f} %", end="", file=sys.stderr)
    print("\r  scanning 100.0 %", file=sys.stderr)
    return size, lines


def sample_rows(path: str, n: int, seed: int = 1) -> tuple[str, list[str]]:
    """Header + n rows taken at random byte offsets (no full parse needed)."""
    size = os.path.getsize(path)
    rnd = random.Random(seed)
    rows = []
    with open(path, "rb") as f:
        header = f.readline().decode("utf-8", "replace")
        start = f.tell()
        for _ in range(n):
            f.seek(rnd.randrange(start, max(start + 1, size - 1)))
            f.readline()                      # skip the partial line
            line = f.readline()
            if line:
                rows.append(line.decode("utf-8", "replace"))
    return header, rows


# ----------------------------------------------------------------------------------------- the model
def plan(total_tokens: int, header_tokens: int, context: int, *, prompt_tokens: int, answer_tokens: int,
         overlap: float, summary_tokens: int, safety: float) -> dict:
    usable = int(context * safety) - prompt_tokens - answer_tokens - header_tokens
    if usable <= 0:
        raise SystemExit(f"context {context} too small for prompt+answer+header")
    step = usable * (1 - overlap)                       # new rows per call when chunks overlap
    map_calls = math.ceil(total_tokens / step)
    # hierarchical reduce: each level merges as many per-chunk summaries as fit in one call
    per_call = max(2, (int(context * safety) - prompt_tokens - answer_tokens) // summary_tokens)
    levels, remaining, reduce_calls = [], map_calls, 0
    while remaining > 1:
        nxt = math.ceil(remaining / per_call)
        levels.append(nxt)
        reduce_calls += nxt
        remaining = nxt
    input_tokens = map_calls * (usable + prompt_tokens + header_tokens) + reduce_calls * (per_call * summary_tokens + prompt_tokens)
    output_tokens = (map_calls + reduce_calls) * answer_tokens
    return {"context": context, "data_tokens_per_call": usable, "overlap_pct": overlap * 100, "map_calls": map_calls,
            "reduce_levels": levels, "reduce_calls": reduce_calls, "total_calls": map_calls + reduce_calls,
            "input_tokens": input_tokens, "output_tokens": output_tokens}


def timing(p: dict, *, overhead_s: float, prefill_tps: float, decode_tps: float, parallel: int, tpm: float | None,
           measured_s_per_call: float | None, rps: float | None = None) -> dict:
    """Wall time = the slowest of three limits: concurrency (parallel requests in flight), request rate (rps),
    and token rate (tpm). With rps the needed concurrency is rps x seconds-per-call."""
    calls = p["total_calls"]
    if measured_s_per_call:
        per_call = measured_s_per_call
    else:
        avg_in = p["input_tokens"] / calls
        avg_out = p["output_tokens"] / calls
        per_call = overhead_s + avg_in / prefill_tps + avg_out / decode_tps
    seq = calls * per_call
    if rps:
        parallel = max(parallel, math.ceil(rps * per_call))      # enough requests in flight to actually reach rps
    par = seq / max(1, parallel)
    rate = calls / rps if rps else 0.0
    limit = (p["input_tokens"] + p["output_tokens"]) / tpm * 60 if tpm else 0.0
    wall = max(par, rate, limit) + per_call                   # + the last call finishing
    bound = max((par, f"{parallel} requests in flight"), (rate, f"{rps:g} requests/second" if rps else ""),
                (limit, "tokens-per-minute limit"))[1]
    tokens_per_min_needed = (p["input_tokens"] + p["output_tokens"]) / max(wall, 1e-9) * 60
    return {"seconds_per_call": per_call, "sequential_s": seq, "parallel_s": par, "rps_floor_s": rate, "rate_limit_floor_s": limit,
            "wall_s": wall, "bound_by": bound, "in_flight": parallel, "tokens_per_min_needed": tokens_per_min_needed}


def human_time(s: float) -> str:
    if s < 90:
        return f"{s:.0f} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"


def human_n(n: float) -> str:
    for unit, div in (("billion", 1e9), ("million", 1e6), ("thousand", 1e3)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n:.0f}"


# ----------------------------------------------------------------------------------------- pipeline reference
def pipeline_reference(job_dir: str | None) -> dict:
    """Numbers of the deterministic+windows pipeline: from a real job if given, else the measured 2 GB run."""
    ref = {"source": "measured 2 GB run of this project (data/jobs/big-2gb-haiku): 3.95 M packets",
           "deterministic_s": 142.4, "evidence_s": 63.9, "llm_calls": 21, "llm_input_tokens": 160_000,
           "llm_wall_s": 1201, "rows_read_by_llm_pct": 0.03}
    if not job_dir:
        return ref
    try:
        st = json.load(open(os.path.join(job_dir, "status.json"), encoding="utf-8"))
        log = json.load(open(os.path.join(job_dir, "llm_log.json"), encoding="utf-8"))
        det = json.load(open(os.path.join(job_dir, "deterministic_trace.json"), encoding="utf-8"))
        win = json.load(open(os.path.join(job_dir, "windows_trace.json"), encoding="utf-8"))
        sub = {s["name"].split(" [")[0]: s for s in st.get("substages", [])}
        def dur(k):
            s = sub.get(k, {})
            return (s.get("finished") or 0) - (s.get("started") or 0)
        rows_in_windows = sum(len(w["lines"]) for w in win)
        ref = {"source": f"job {os.path.basename(os.path.normpath(job_dir))}: {det['rows']:,} packets",
               "deterministic_s": dur("2.1 deterministic pass"), "evidence_s": dur("2.3 evidence windows"),
               "llm_calls": sum(1 for l in log if l.get("ok")),
               "llm_input_tokens": sum((l.get("usage") or {}).get("prompt_tokens", 0) for l in log) or ref["llm_input_tokens"],
               "llm_wall_s": sum(l.get("seconds", 0) for l in log),
               "rows_read_by_llm_pct": round(100 * rows_in_windows / max(1, det["rows"]), 4)}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not read job dir: {e}; using the reference numbers", file=sys.stderr)
    return ref


# ----------------------------------------------------------------------------------------- report
def write_html(out: str, r: dict) -> None:
    e = html.escape
    rows = []
    for p in r["plans"]:
        t = p["time"]
        cost = f"{p['cost_usd']:,.0f} USD" if p.get("cost_usd") is not None else "-"
        rows.append(f"<tr><td>{p['context']:,}</td><td>{p['data_tokens_per_call']:,}</td><td><b>{p['map_calls']:,}</b></td>"
                    f"<td>{p['reduce_calls']:,} ({' → '.join(str(x) for x in p['reduce_levels'])})</td><td><b>{p['total_calls']:,}</b></td>"
                    f"<td>{human_n(p['input_tokens'])}</td><td>{human_time(t['sequential_s'])}</td><td><b>{human_time(t['wall_s'])}</b><br>"
                    f"<span class='m'>{e(t['bound_by'])}</span></td><td>{cost}</td></tr>")
    pr = r["pipeline"]
    big = r["plans"][-1]
    ratio_calls = big["total_calls"] / max(1, pr["llm_calls"])
    ratio_tokens = big["input_tokens"] / max(1, pr["llm_input_tokens"])
    a = r["assumptions"]
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLM Token Budget</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#1b2430;--m:#5b6675;--line:#e1e5eb;--acc:#1f5fbf;--bad:#b3261e;--good:#2f855a}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0f1318;--card:#171c23;--ink:#e6e9ee;--m:#9aa5b4;--line:#2a323d;--acc:#6ea3ff;--bad:#fc8181;--good:#68d391}}}}
:root[data-theme="dark"]{{--bg:#0f1318;--card:#171c23;--ink:#e6e9ee;--m:#9aa5b4;--line:#2a323d;--acc:#6ea3ff;--bad:#fc8181;--good:#68d391}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,Segoe UI,Roboto,sans-serif}}main{{max-width:1040px;margin:0 auto;padding:28px 16px}}
h1{{font-size:27px;margin:0 0 6px}}h2{{font-size:21px;margin:36px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px;margin:14px 0}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}.k{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.k b{{display:block;font-size:24px}}.k span,.m{{color:var(--m);font-size:13px}}.bad b{{color:var(--bad)}}.good b{{color:var(--good)}}
.tw{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}}th{{color:var(--m)}}
code{{font:13px ui-monospace,Consolas,monospace;background:rgba(127,127,127,.12);padding:1px 5px;border-radius:4px}}li{{margin:6px 0}}
</style></head><body><main>
<h1>Why the trace CSV is not sent straight to the LLM</h1>
<p class="m">Measured on <code>{e(r['file'])}</code> · {r['size_gb']:.2f} GB · {r['rows']:,} rows · tokenizer: {e(r['tokenizer'])} · generated {e(r['generated'])}</p>

<div class="card"><p><b>Bottom line.</b> Sending this file directly to the model would take <b>{big['total_calls']:,} LLM calls</b> and about
<b>{human_time(big['time']['wall_s'])}</b> even with a {big['context']:,}-token context, and the answers would still be unreliable, because each call sees only a slice of the trace.
The pipeline we built reads every row with ordinary code in minutes and sends the model only the packets around each problem:
<b>{pr['llm_calls']} calls</b>, about {human_n(pr['llm_input_tokens'])} tokens. That is about <b>{ratio_calls:,.0f}× fewer calls</b> and <b>{ratio_tokens:,.0f}× fewer tokens</b>.</p></div>

<div class="kpis">
<div class="k"><b>{human_n(r['total_tokens'])}</b><span>tokens in the file</span></div>
<div class="k"><b>{r['tokens_per_row']:.0f}</b><span>tokens per CSV row (112 columns, mostly empty, hex payload)</span></div>
<div class="k bad"><b>{big['total_calls']:,}</b><span>calls to read it raw at {big['context']:,} context</span></div>
<div class="k good"><b>{pr['llm_calls']}</b><span>calls with the pipeline (any file size)</span></div>
</div>

<h2>1. What direct LLM reading would take</h2>
<div class="tw"><table>
<tr><th>Context size</th><th>Data per call (tokens)</th><th>Calls to read the file once</th><th>Calls to merge the answers (levels)</th><th>Total calls</th><th>Input tokens</th><th>Time one at a time</th><th>Wall time with {a['parallel']} parallel</th><th>Cost</th></tr>
{''.join(rows)}
</table></div>
<p class="m">Per call: {a['prompt_tokens']:,} tokens of instructions + the CSV header ({r['header_tokens']:,} tokens, repeated in every chunk) + {a['answer_tokens']:,} tokens of answer; {a['safety']*100:.0f} % of the context used; {a['overlap']*100:.0f} % overlap between chunks so an event on a boundary is not cut;
each chunk answer is summarised in {a['summary_tokens']} tokens and merged level by level.
Time per call: {"measured " + str(a['measured_s_per_call']) + " s" if a['measured_s_per_call'] else f"{a['overhead_s']} s overhead + input at {a['prefill_tps']:,.0f} tokens/s + output at {a['decode_tps']:,.0f} tokens/s"}{f"; rate limit {a['tpm']:,.0f} tokens/min" if a['tpm'] else ""}{f"; {a['rps']:g} requests/second, which needs about {big['time']['in_flight']:,} requests in flight at once and {human_n(big['time']['tokens_per_min_needed'])} tokens per minute" if a.get('rps') else ""}. These are optimistic: retries, throttling and longer prompts make it slower.</p>

<h2>2. Why splitting the file into chunks gives wrong answers, not only slow ones</h2>
<div class="card"><ol>
<li><b>Problems span chunks.</b> One NVMe command is a doorbell, a command fetch, data transfers and a completion, often hundreds of rows apart. A completion timeout is the <i>absence</i> of an answer over 50 ms of traffic. A model that sees one slice at a time cannot connect the start of a command with its end, so it cannot measure it.</li>
<li><b>The checks need memory of the whole trace.</b> Flow-control credits are a running total since the link came up. A replay is only visible by remembering every sequence number already sent. Each open request must be remembered until its answer arrives. Between calls the model remembers nothing.</li>
<li><b>"Too slow" needs every command.</b> The expected time for a command is the median of all commands of that type (tens of thousands of them). No chunk holds enough, and language models are unreliable at exact arithmetic over thousands of timestamps.</li>
<li><b>Almost all rows are healthy.</b> More than 99.9 % of the rows are routine acknowledgements and data. Thousands of calls would spend nearly all their effort confirming nothing is wrong, and every extra call is another chance of a false alarm the team then has to chase.</li>
<li><b>Results would not be repeatable.</b> Thousands of independent model answers, merged by more model calls, give a slightly different conclusion each run. An engineering decision needs the same answer every time and a packet number to verify it.</li>
<li><b>Cost and time grow with file size.</b> Twice the trace means twice the calls. Our approach does not grow: the model only looks at problems.</li>
</ol></div>

<h2>3. What the pipeline does instead</h2>
<div class="tw"><table>
<tr><th></th><th>Send the CSV to the LLM</th><th>Our pipeline</th></tr>
<tr><td>Who reads every row</td><td>the LLM, {big['total_calls']:,} times in slices</td><td>ordinary code, once, with full memory of the trace ({human_time(pr['deterministic_s'])} for {e(pr['source'])})</td></tr>
<tr><td>How problems are found</td><td>model guesses per slice</td><td>exact protocol rules (missing answers, repeated sequence numbers, credit arithmetic, error flags, timing); every finding has a packet number</td></tr>
<tr><td>What the LLM reads</td><td>100 % of the rows</td><td>about {pr['rows_read_by_llm_pct']} % of the rows: only the packets around each problem, plus a one-line-per-segment summary of the whole trace</td></tr>
<tr><td>LLM calls</td><td>{big['total_calls']:,}</td><td>{pr['llm_calls']} (one per problem, one to merge, one per test case)</td></tr>
<tr><td>LLM tokens</td><td>{human_n(big['input_tokens'])}</td><td>{human_n(pr['llm_input_tokens'])}</td></tr>
<tr><td>Time</td><td>{human_time(big['time']['wall_s'])}</td><td>{human_time(pr['deterministic_s'] + pr['evidence_s'] + pr['llm_wall_s'])} end to end</td></tr>
<tr><td>Repeatable, verifiable</td><td>no</td><td>yes: the same file gives the same findings; each claim cites packets you can open in the analyzer software</td></tr>
<tr><td>What the LLM is used for</td><td>everything</td><td>what it is good at: explaining a mechanism from a small piece of evidence, writing the summary and the reproduction tests</td></tr>
</table></div>

<h2>4. How to say it in one minute</h2>
<div class="card"><p>"A {r['size_gb']:.1f} GB trace is about {human_n(r['total_tokens'])} tokens. Even a {big['context']//1000}K-context model needs {big['total_calls']:,} calls and roughly {human_time(big['time']['wall_s'])} to read it once, and because each call only sees a slice, it cannot follow a command from start to end or keep the running counters the checks need. So we let normal code read every packet in minutes and find the problems exactly, and we use the model only to explain each problem and write the tests: {pr['llm_calls']} calls instead of {big['total_calls']:,}, the same answer every run, and every claim points to a packet number we can verify."</p></div>

<h2>5. Assumptions and how to change them</h2>
<div class="card"><ul>
<li>Token count: {e(r['tokenizer'])}, measured on {r['sample_rows']:,} random rows ({r['tokens_per_row']:.1f} tokens per row, {r['chars_per_token']:.2f} characters per token), extrapolated to {r['rows']:,} rows. Use <code>--hf-tokenizer Qwen/...</code> for the exact Qwen tokenizer.</li>
<li>Re-run with your own numbers: <code>--contexts</code>, <code>--parallel</code>, <code>--tpm</code>, <code>--seconds-per-call</code>, <code>--price-in/--price-out</code>, <code>--job-dir</code> to compare against a real pipeline run.</li>
<li>Pipeline numbers: {e(pr['source'])}.</li>
</ul></div>
</main></body></html>"""
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)


# ----------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--contexts", type=int, nargs="+", default=[28000, 100000, 128000])
    ap.add_argument("--sample-rows", type=int, default=20000)
    ap.add_argument("--hf-tokenizer", default=None, help="Hugging Face tokenizer name/path, e.g. Qwen/Qwen2.5-7B-Instruct")
    ap.add_argument("--prompt-tokens", type=int, default=1500, help="instructions sent with every chunk")
    ap.add_argument("--answer-tokens", type=int, default=1000, help="answer per call")
    ap.add_argument("--summary-tokens", type=int, default=300, help="size of one chunk's answer when merged")
    ap.add_argument("--overlap", type=float, default=0.10, help="fraction of each chunk repeated in the next")
    ap.add_argument("--safety", type=float, default=0.90, help="fraction of the context actually used")
    ap.add_argument("--overhead-s", type=float, default=1.0, help="fixed latency per call")
    ap.add_argument("--prefill-tps", type=float, default=3000, help="input tokens processed per second per call")
    ap.add_argument("--decode-tps", type=float, default=50, help="output tokens generated per second per call")
    ap.add_argument("--seconds-per-call", type=float, default=None, help="use a measured seconds-per-call instead of the model")
    ap.add_argument("--parallel", type=int, default=4, help="concurrent requests allowed")
    ap.add_argument("--tpm", type=float, default=None, help="tokens-per-minute rate limit of the API key")
    ap.add_argument("--rps", type=float, default=None, help="requests-per-second limit of the API key (e.g. 5)")
    ap.add_argument("--price-in", type=float, default=None, help="USD per 1M input tokens")
    ap.add_argument("--price-out", type=float, default=None, help="USD per 1M output tokens")
    ap.add_argument("--job-dir", default=None, help="a finished pipeline job folder, to compare with real numbers")
    ap.add_argument("--out", default=None, help="HTML report path (default: next to the CSV)")
    a = ap.parse_args()

    print(f"file: {a.csv}", file=sys.stderr)
    size, lines = scan_file(a.csv)
    rows = max(0, lines - 1)
    tok = Tokenizer(a.hf_tokenizer)
    header, sample = sample_rows(a.csv, a.sample_rows)
    header_tokens = tok.count(header)
    per_row = [tok.count(s) for s in sample]
    chars = sum(len(s) for s in sample)
    tokens_per_row = statistics.mean(per_row) if per_row else 0
    total_tokens = int(header_tokens + tokens_per_row * rows)
    chars_per_token = chars / max(1, sum(per_row))

    plans = []
    for ctx in a.contexts:
        p = plan(total_tokens, header_tokens, ctx, prompt_tokens=a.prompt_tokens, answer_tokens=a.answer_tokens,
                 overlap=a.overlap, summary_tokens=a.summary_tokens, safety=a.safety)
        p["time"] = timing(p, overhead_s=a.overhead_s, prefill_tps=a.prefill_tps, decode_tps=a.decode_tps, parallel=a.parallel,
                           tpm=a.tpm, measured_s_per_call=a.seconds_per_call, rps=a.rps)
        p["cost_usd"] = (p["input_tokens"] / 1e6 * a.price_in + p["output_tokens"] / 1e6 * (a.price_out or a.price_in)) if a.price_in else None
        plans.append(p)

    report = {"file": os.path.abspath(a.csv), "size_gb": size / 1e9, "rows": rows, "tokenizer": tok.name,
              "sample_rows": len(sample), "header_tokens": header_tokens, "tokens_per_row": tokens_per_row,
              "chars_per_token": chars_per_token, "total_tokens": total_tokens, "plans": plans,
              "pipeline": pipeline_reference(a.job_dir), "generated": time.strftime("%Y-%m-%d %H:%M"),
              "assumptions": {k: getattr(a, k) for k in ("prompt_tokens", "answer_tokens", "summary_tokens", "overlap", "safety",
                                                         "overhead_s", "prefill_tps", "decode_tps", "parallel", "tpm", "rps")}
                             | {"measured_s_per_call": a.seconds_per_call}}

    out = a.out or os.path.splitext(a.csv)[0] + "_token_budget.html"
    write_html(out, report)
    with open(os.path.splitext(out)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)

    print(f"\n{size / 1e9:.2f} GB, {rows:,} rows, {tokens_per_row:.1f} tokens/row -> {human_n(total_tokens)} tokens  [{tok.name}]")
    print(f"{'context':>9} {'calls':>8} {'input tok':>15} {'s/call':>7} {'in flight':>9} {'wall':>10} {'tokens/min needed':>18}  limited by")
    for p in plans:
        t = p["time"]
        print(f"{p['context']:>9,} {p['total_calls']:>8,} {human_n(p['input_tokens']):>15} {t['seconds_per_call']:>7.0f} {t['in_flight']:>9,} "
              f"{human_time(t['wall_s']):>10} {human_n(t['tokens_per_min_needed']):>18}  {t['bound_by']}"
              + (f"  ~{p['cost_usd']:,.0f} USD" if p['cost_usd'] is not None else ""))
    pr = report["pipeline"]
    print(f"pipeline: {pr['llm_calls']} LLM calls, ~{human_n(pr['llm_input_tokens'])} tokens ({pr['source']})")
    print(f"report: {out}")


if __name__ == "__main__":
    main()
