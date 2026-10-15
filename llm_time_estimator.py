#!/usr/bin/env python
"""
llm_time_estimator.py - standalone: how long would it take to push a whole CSV through an LLM API?

Needs only Python 3.8+. If `tiktoken` is installed (pip install tiktoken) tokens are counted with a real
tokenizer on a random sample of rows; otherwise a 3-characters-per-token estimate is used.

What it does
  1. Streams the CSV once (constant memory; a 5 GB file takes about a minute) to count rows and bytes.
  2. Tokenizes a random sample of rows and extrapolates the total number of tokens.
  3. For each context size: how many calls are needed to read the file once (with overlap between chunks)
     plus the calls to merge the chunk answers.
  4. Time under your API limits. Wall time is the slowest of:
       - request rate        calls / requests-per-second
       - token rate          total tokens / tokens-per-minute
       - concurrency         calls x seconds-per-call / requests-in-flight
     and it tells you which one is the bottleneck and how many requests must be in flight to reach the rate.

Examples
  python llm_time_estimator.py --csv D:\\traces\\export.csv --rps 5
  python llm_time_estimator.py --csv D:\\traces\\export.csv --rps 5 --tpm 2000000 --contexts 100000 128000
  python llm_time_estimator.py --csv D:\\traces\\export.csv --rps 5 --max-in-flight 20 --seconds-per-call 40
  python llm_time_estimator.py --tokens 3000000000 --rps 5          # skip the file, give a token count
"""
import argparse
import math
import os
import random
import sys


# ------------------------------------------------------------------ token counting
def make_counter():
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return (lambda s: len(enc.encode(s, disallowed_special=()))), "tiktoken cl100k_base"
    except Exception:
        return (lambda s: max(1, round(len(s) / 3.0))), "estimate: 3 characters per token (pip install tiktoken for a real count)"


def count_csv_tokens(path, sample_rows=20000, seed=1):
    size = os.path.getsize(path)
    lines = 0
    with open(path, "rb") as f:
        done = 0
        while True:
            b = f.read(64 << 20)
            if not b:
                break
            lines += b.count(b"\n")
            done += len(b)
            print(f"\r  reading file {done / size * 100:5.1f} %", end="", file=sys.stderr)
    print(file=sys.stderr)
    rows = max(0, lines - 1)
    count, tok_name = make_counter()
    rnd = random.Random(seed)
    with open(path, "rb") as f:
        header = f.readline().decode("utf-8", "replace")
        start = f.tell()
        sample = []
        for _ in range(min(sample_rows, rows)):
            f.seek(rnd.randrange(start, max(start + 1, size - 1)))
            f.readline()
            line = f.readline()
            if line:
                sample.append(line.decode("utf-8", "replace"))
    per_row = sum(count(s) for s in sample) / max(1, len(sample))
    header_tokens = count(header)
    return {"size_gb": size / 1e9, "rows": rows, "tokens_per_row": per_row, "header_tokens": header_tokens,
            "total_tokens": int(header_tokens + per_row * rows), "tokenizer": tok_name}


# ------------------------------------------------------------------ calls and time
def calls_needed(total_tokens, header_tokens, context, prompt, answer, overlap, summary, usable_frac):
    room = int(context * usable_frac) - prompt - answer - header_tokens
    if room <= 0:
        sys.exit(f"context {context} is too small for prompt + answer + header")
    map_calls = math.ceil(total_tokens / (room * (1 - overlap)))
    per_merge = max(2, (int(context * usable_frac) - prompt - answer) // summary)
    merge_calls, n = 0, map_calls
    while n > 1:
        n = math.ceil(n / per_merge)
        merge_calls += n
    in_tokens = map_calls * (room + prompt + header_tokens) + merge_calls * (per_merge * summary + prompt)
    out_tokens = (map_calls + merge_calls) * answer
    return map_calls, merge_calls, in_tokens, out_tokens


def wall_time(calls, tokens, s_per_call, rps, tpm, max_in_flight):
    limits = {}
    if rps:
        limits["requests/second"] = calls / rps
    if tpm:
        limits["tokens/minute"] = tokens / tpm * 60
    needed_in_flight = math.ceil((rps or 1e9) * s_per_call) if rps else None
    in_flight = max_in_flight or needed_in_flight or 1
    limits["requests in flight"] = calls * s_per_call / in_flight
    bottleneck = max(limits, key=limits.get)
    return limits[bottleneck] + s_per_call, bottleneck, in_flight, needed_in_flight


def fmt_time(s):
    if s < 90:
        return f"{s:.0f} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"


def fmt_n(n):
    for unit, d in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= d:
            return f"{n / d:.2f}{unit}"
    return f"{n:.0f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="the CSV file to measure")
    src.add_argument("--tokens", type=float, help="skip the file and use this total token count")
    ap.add_argument("--contexts", type=int, nargs="+", default=[28000, 100000, 128000])
    ap.add_argument("--rps", type=float, default=None, help="API limit: requests per second (e.g. 5)")
    ap.add_argument("--tpm", type=float, default=None, help="API limit: tokens per minute (e.g. 2000000)")
    ap.add_argument("--max-in-flight", type=int, default=None, help="API limit: concurrent requests (default: as many as needed)")
    ap.add_argument("--seconds-per-call", type=float, default=None,
                    help="measured time of one call; default = 1 s + input/3000 tok/s + output/50 tok/s")
    ap.add_argument("--prompt", type=int, default=1500, help="instruction tokens per call")
    ap.add_argument("--answer", type=int, default=1000, help="answer tokens per call")
    ap.add_argument("--overlap", type=float, default=0.10, help="fraction of each chunk repeated in the next")
    ap.add_argument("--summary", type=int, default=300, help="tokens per chunk answer when merging")
    ap.add_argument("--usable", type=float, default=0.90, help="fraction of the context actually filled")
    ap.add_argument("--price-in", type=float, default=None, help="USD per 1M input tokens")
    ap.add_argument("--price-out", type=float, default=None, help="USD per 1M output tokens")
    a = ap.parse_args()

    if a.csv:
        m = count_csv_tokens(a.csv)
        print(f"\n{a.csv}: {m['size_gb']:.2f} GB, {m['rows']:,} rows, {m['tokens_per_row']:.1f} tokens/row "
              f"-> {fmt_n(m['total_tokens'])} tokens  [{m['tokenizer']}]")
        total, header = m["total_tokens"], m["header_tokens"]
    else:
        total, header = int(a.tokens), 600
        print(f"\ninput: {fmt_n(total)} tokens")
    lim = ", ".join(x for x in (f"{a.rps:g} req/s" if a.rps else "", f"{fmt_n(a.tpm)} tokens/min" if a.tpm else "",
                                f"{a.max_in_flight} in flight" if a.max_in_flight else "") if x) or "no limits given"
    print(f"API limits: {lim}\n")
    print(f"{'context':>9} {'calls':>8} {'tokens':>9} {'s/call':>7} {'in flight':>10} {'time':>9}  bottleneck")
    for ctx in a.contexts:
        mc, rc, tin, tout = calls_needed(total, header, ctx, a.prompt, a.answer, a.overlap, a.summary, a.usable)
        calls = mc + rc
        s_call = a.seconds_per_call or (1.0 + (tin / calls) / 3000 + (tout / calls) / 50)
        t, bott, in_flight, needed = wall_time(calls, tin + tout, s_call, a.rps, a.tpm, a.max_in_flight)
        line = f"{ctx:>9,} {calls:>8,} {fmt_n(tin + tout):>9} {s_call:>7.0f} {in_flight:>10,} {fmt_time(t):>9}  {bott}"
        if a.rps and a.max_in_flight and needed and a.max_in_flight < needed:
            line += f"  (5 req/s would need {needed} in flight)".replace("5", f"{a.rps:g}", 1)
        if a.price_in:
            line += f"  ~{tin / 1e6 * a.price_in + tout / 1e6 * (a.price_out or a.price_in):,.0f} USD"
        print(line)
    if a.rps and not a.tpm:
        print("\nNote: no --tpm given. Real API keys almost always also limit tokens per minute; that limit, not the "
              "request rate, usually decides the time. Add --tpm with your key's limit.")
    print("\nCalls = chunks to read the file once + calls to merge the chunk answers. Time assumes no retries or throttling.")


if __name__ == "__main__":
    main()
