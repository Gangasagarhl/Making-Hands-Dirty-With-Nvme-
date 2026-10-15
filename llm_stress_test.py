#!/usr/bin/env python
"""
llm_stress_test.py - standalone, parallel stress test: send CSV chunks to an LLM API and measure what really happens.

It cuts the CSV into chunks that fill the model's context (CSV header repeated in every chunk, optional overlap between
consecutive chunks), sends them IN PARALLEL to an OpenAI-compatible endpoint (Qwen/DashScope, vLLM, OpenRouter, ...)
or the Anthropic API, respecting your requests-per-second and concurrency limits, retries throttled requests (HTTP 429
/ 5xx) with backoff, and measures:
  * latency per call (p50 / p95 / max), achieved requests/second and tokens/minute
  * how many requests were throttled or failed
  * a projection: how long the WHOLE file would take at the measured speed, and how many tokens it would consume

Every request costs money, so by default only --max-requests (default 20) chunks are sent, picked from evenly spread
positions in the file so they are representative. Use --all --i-understand-the-cost to send the entire file.

Needs: Python 3.8+, `requests` (pip install requests). Optional: `tiktoken` for exact token counting.

Examples
  python llm_stress_test.py --csv D:\\traces\\export.csv --base-url https://dashscope-intl.aliyuncs.com/compatible-mode/v1 \\
         --api-key sk-... --model qwen-plus --context 128000 --rps 5 --concurrency 20 --max-requests 40
  python llm_stress_test.py --csv export.csv --base-url ... --api-key ... --model ... --dry-run      # only chunk + count
  python llm_stress_test.py --csv export.csv ... --context 28000 --rps 5 --duration 300             # run for 5 minutes
  (URL / key / model can also come from environment variables LLM_BASE_URL, LLM_API_KEY, LLM_MODEL)

Outputs (next to the CSV unless --out-dir): stress_<time>.json (summary + projection) and stress_<time>_requests.csv.
"""
import argparse
import csv
import json
import os
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

PROMPT = ("You are a PCIe/NVMe protocol analyst. Below is a chunk of a Teledyne LeCroy PCIe Protocol Analysis CSV export "
          "(first line = header). List protocol problems you can see in THIS chunk only (bad LCRC, Nak, replayed PSN, "
          "non-SC completion status, EP=1, missing completions, long gaps, link events). Answer as compact JSON: "
          '{"problems":[{"packet":<n>,"type":"...","note":"..."}]} and nothing else.')


# ------------------------------------------------------------------------------------------ tokens
def make_counter():
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return (lambda s: len(enc.encode(s, disallowed_special=()))), "tiktoken cl100k_base"
    except Exception:
        return (lambda s: max(1, round(len(s) / 3.0))), "3 chars/token estimate"


# ------------------------------------------------------------------------------------------ chunking
class Chunker:
    """Builds chunks of about `target` tokens. Spread mode: chunk i starts at byte offset i*size/n (representative
    sample, no overlap needed). Sequential mode (--all): consecutive chunks, the last `overlap` fraction of each chunk's
    rows is repeated at the start of the next so events on a boundary are seen whole."""

    def __init__(self, path, target_tokens, overlap, count):
        self.path, self.target, self.overlap, self.count = path, target_tokens, overlap, count
        self.size = os.path.getsize(path)
        with open(path, "rb") as f:
            self.header = f.readline().decode("utf-8", "replace")
            self.data_start = f.tell()
        self.header_tokens = count(self.header)

    def _fill(self, f, carry=None):
        rows, tok = list(carry or []), sum(t for _, t in (carry or []))
        start = f.tell()
        while tok < self.target:
            line = f.readline()
            if not line:
                break
            s = line.decode("utf-8", "replace")
            t = self.count(s)
            rows.append((s, t))
            tok += t
        return rows, tok, f.tell() - start

    def spread(self, n):
        with open(self.path, "rb") as f:
            for i in range(n):
                off = self.data_start + int((self.size - self.data_start) * i / max(1, n))
                f.seek(off)
                if off > self.data_start:
                    f.readline()
                rows, tok, nbytes = self._fill(f)
                if rows:
                    yield i, self.header + "".join(s for s, _ in rows), self.header_tokens + tok, nbytes, len(rows)

    def sequential(self):
        with open(self.path, "rb") as f:
            f.seek(self.data_start)
            carry, i = [], 0
            while True:
                rows, tok, nbytes = self._fill(f, carry)
                if len(rows) <= len(carry):
                    break
                yield i, self.header + "".join(s for s, _ in rows), self.header_tokens + tok, nbytes, len(rows)
                keep = int(len(rows) * self.overlap)
                carry = rows[-keep:] if keep else []
                i += 1


# ------------------------------------------------------------------------------------------ rate limiting
class RateLimiter:
    """Thread-safe: at most `rps` request starts per second, evenly spaced."""

    def __init__(self, rps):
        self.interval = 1.0 / rps if rps else 0.0
        self.next = time.monotonic()
        self.lock = threading.Lock()

    def wait(self):
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.interval
        if t > now:
            time.sleep(t - now)


# ------------------------------------------------------------------------------------------ API call
def call_api(a, text):
    if a.provider == "anthropic":
        url = a.base_url.rstrip("/").removesuffix("/v1") + "/v1/messages"
        body = {"model": a.model, "max_tokens": a.max_output, "system": PROMPT, "messages": [{"role": "user", "content": text}]}
        hdr = {"x-api-key": a.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    else:
        base = a.base_url.rstrip("/")
        for suf in ("/chat/completions", "/completions"):
            if base.endswith(suf):
                base = base[: -len(suf)]
        last = base.rsplit("/", 1)[-1]
        if not (last.startswith("v") and last[1:2].isdigit()) and "compatible-mode" not in base:
            base += "/v1"          # OpenAI, vLLM, Ollama, OpenRouter all serve under /v1
        url = base + "/chat/completions"
        body = {"model": a.model, "max_tokens": a.max_output, "temperature": 0,
                "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": text}]}
        hdr = {"Authorization": f"Bearer {a.api_key}", "Content-Type": "application/json"}
    r = requests.post(url, json=body, headers=hdr, timeout=a.timeout)
    if r.status_code >= 400:
        return r.status_code, None, None, r.text[:300], r.headers.get("retry-after")
    j = r.json()
    u = j.get("usage", {})
    pin = u.get("prompt_tokens", u.get("input_tokens"))
    pout = u.get("completion_tokens", u.get("output_tokens"))
    return r.status_code, pin, pout, "", None


def worker(a, limiter, idx, text, est_tokens, results, lock, progress):
    rec = {"chunk": idx, "est_input_tokens": est_tokens, "attempts": 0, "throttled": 0, "status": None,
           "input_tokens": None, "output_tokens": None, "latency_s": None, "error": ""}
    backoff = 2.0
    t_first = time.time()
    for attempt in range(1, a.retries + 2):
        limiter.wait()
        rec["attempts"] = attempt
        t0 = time.time()
        try:
            status, pin, pout, err, retry_after = call_api(a, text)
        except Exception as e:  # noqa: BLE001
            status, pin, pout, err, retry_after = -1, None, None, f"{type(e).__name__}: {e}"[:300], None
        rec["latency_s"] = round(time.time() - t0, 3)
        rec["status"], rec["error"] = status, err
        if status == 200:
            rec["input_tokens"], rec["output_tokens"] = pin, pout
            break
        if status == 429 or status >= 500 or status == -1:
            rec["throttled"] += 1 if status == 429 else 0
            wait = float(retry_after) if retry_after and str(retry_after).replace(".", "").isdigit() else backoff
            time.sleep(min(60, wait) * (0.8 + 0.4 * random.random()))
            backoff = min(60, backoff * 2)
            continue
        break   # 4xx other than 429: do not retry (bad key, bad model, context too long ...)
    rec["total_s"] = round(time.time() - t_first, 3)
    rec["start"], rec["end"] = t_first, time.time()
    with lock:
        results.append(rec)
        progress()


# ------------------------------------------------------------------------------------------ report
def pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def fmt_t(s):
    if s is None:
        return "-"
    if s < 90:
        return f"{s:.1f} s"
    if s < 5400:
        return f"{s / 60:.1f} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--provider", choices=["openai", "anthropic"], default=None, help="default: anthropic if the URL says so, else openai")
    ap.add_argument("--base-url", default=os.getenv("LLM_BASE_URL", ""))
    ap.add_argument("--api-key", default=os.getenv("LLM_API_KEY", ""))
    ap.add_argument("--model", default=os.getenv("LLM_MODEL", ""))
    ap.add_argument("--context", type=int, default=int(os.getenv("LLM_CONTEXT_TOKENS", "128000")), help="model context window (tokens)")
    ap.add_argument("--fill", type=float, default=0.85, help="fraction of the context filled with CSV rows")
    ap.add_argument("--max-output", type=int, default=800, help="max answer tokens per call")
    ap.add_argument("--overlap", type=float, default=0.10, help="sequential mode: fraction of rows repeated in the next chunk")
    ap.add_argument("--rps", type=float, default=5.0, help="max request starts per second (API limit)")
    ap.add_argument("--concurrency", type=int, default=20, help="max requests in flight at once")
    ap.add_argument("--max-requests", type=int, default=20, help="chunks to send (spread across the file)")
    ap.add_argument("--duration", type=float, default=None, help="stop starting new requests after this many seconds")
    ap.add_argument("--all", action="store_true", help="send the ENTIRE file sequentially (needs --i-understand-the-cost)")
    ap.add_argument("--i-understand-the-cost", action="store_true")
    ap.add_argument("--retries", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--dry-run", action="store_true", help="only build chunks and count tokens; no API calls")
    ap.add_argument("--out-dir", default=None)
    a = ap.parse_args()

    if not a.dry_run and not (a.base_url and a.api_key and a.model):
        sys.exit("give --base-url, --api-key and --model (or LLM_BASE_URL / LLM_API_KEY / LLM_MODEL), or use --dry-run")
    a.provider = a.provider or ("anthropic" if "anthropic" in a.base_url else "openai")
    if a.all and not a.i_understand_the_cost and not a.dry_run:
        sys.exit("--all sends the whole file (thousands of paid requests). Run a sample first, then add --i-understand-the-cost.")

    count, tok_name = make_counter()
    target = int(a.context * a.fill) - len(PROMPT) // 3 - a.max_output
    ch = Chunker(a.csv, target - 0, a.overlap, count)
    ch.target = target - ch.header_tokens
    print(f"file {a.csv}: {ch.size / 1e9:.2f} GB | context {a.context:,} -> ~{target:,} tokens of CSV per request "
          f"(header {ch.header_tokens} tokens repeated each time) | tokenizer: {tok_name}")
    print(f"limits: {a.rps:g} requests/s, {a.concurrency} in flight, retries {a.retries}"
          + (f", duration {a.duration:g} s" if a.duration else ""))

    chunks = ch.sequential() if a.all else ch.spread(a.max_requests)
    if a.dry_run:
        sizes, toks, rows = [], [], []
        for i, text, est, nbytes, nrows in chunks:
            sizes.append(nbytes)
            toks.append(est)
            rows.append(nrows)
            if not a.all and i + 1 >= a.max_requests:
                break
        avg_b = statistics.mean(sizes)
        total_chunks = int((ch.size - ch.data_start) / (avg_b * (1 - (a.overlap if True else 0)))) + 1
        print(f"dry run: {len(toks)} chunks built, avg {statistics.mean(toks):,.0f} tokens / {statistics.mean(rows):,.0f} rows / "
              f"{avg_b / 1e6:.2f} MB each")
        print(f"whole file would need about {total_chunks:,} requests and {total_chunks * statistics.mean(toks) / 1e9:.2f} billion input tokens")
        return

    results, lock = [], threading.Lock()
    limiter = RateLimiter(a.rps)
    sem = threading.Semaphore(a.concurrency)
    t_start = time.time()
    sent_bytes, launched = [], 0
    done_count = [0]

    def progress():
        done_count[0] += 1
        ok = sum(1 for r in results if r["status"] == 200)
        thr = sum(r["throttled"] for r in results)
        el = time.time() - t_start
        print(f"\r  done {done_count[0]}/{launched}  ok {ok}  throttled(429) {thr}  elapsed {fmt_t(el)}  "
              f"rate {done_count[0] / el:.2f} req/s", end="", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        for i, text, est, nbytes, nrows in chunks:
            if a.duration and time.time() - t_start > a.duration:
                break
            if not a.all and launched >= a.max_requests:
                break
            sem.acquire()
            launched += 1
            sent_bytes.append(nbytes)

            def run(i=i, text=text, est=est):
                try:
                    worker(a, limiter, i, text, est, results, lock, progress)
                finally:
                    sem.release()
            ex.submit(run)
    wall = time.time() - t_start
    print(file=sys.stderr)

    ok = [r for r in results if r["status"] == 200]
    lat = [r["latency_s"] for r in ok]
    tin = sum((r["input_tokens"] if r["input_tokens"] is not None else r["est_input_tokens"]) for r in ok)
    tout = sum((r["output_tokens"] or 0) for r in ok)
    throttled = sum(r["throttled"] for r in results)
    failed = [r for r in results if r["status"] != 200]
    avg_bytes = statistics.mean(sent_bytes) if sent_bytes else 0
    step = avg_bytes * (1 - a.overlap)
    total_chunks = int((ch.size - ch.data_start) / step) + 1 if step else 0
    rate = len(ok) / wall if wall else 0
    tok_per_min = (tin + tout) / wall * 60 if wall else 0
    in_per_req = tin / len(ok) if ok else 0
    projected = total_chunks / rate if rate else None

    summary = {
        "file": os.path.abspath(a.csv), "file_gb": ch.size / 1e9, "provider": a.provider, "base_url": a.base_url, "model": a.model,
        "context": a.context, "csv_tokens_per_request_target": target, "rps_limit": a.rps, "concurrency": a.concurrency,
        "requests_sent": len(results), "succeeded": len(ok), "failed": len(failed), "throttled_429": throttled,
        "wall_s": round(wall, 2), "achieved_requests_per_s": round(rate, 3), "achieved_tokens_per_min": int(tok_per_min),
        "latency_s": {"p50": pct(lat, .5), "p95": pct(lat, .95), "max": max(lat) if lat else None, "mean": round(statistics.mean(lat), 3) if lat else None},
        "input_tokens_measured": tin, "output_tokens_measured": tout, "input_tokens_per_request": round(in_per_req),
        "projection_whole_file": {"requests": total_chunks, "input_tokens": int(total_chunks * in_per_req),
                                  "output_tokens": int(total_chunks * (tout / len(ok) if ok else 0)),
                                  "time_s_at_measured_rate": round(projected, 1) if projected else None,
                                  "note": "map step only; merging the chunk answers adds more calls"},
        "errors": sorted({f"{r['status']}: {r['error'][:120]}" for r in failed})[:10],
    }
    out_dir = a.out_dir or os.path.dirname(os.path.abspath(a.csv))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    jpath = os.path.join(out_dir, f"stress_{stamp}.json")
    cpath = os.path.join(out_dir, f"stress_{stamp}_requests.csv")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    with open(cpath, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["chunk", "status", "attempts", "throttled", "latency_s", "total_s", "est_input_tokens",
                                          "input_tokens", "output_tokens", "start", "end", "error"])
        w.writeheader()
        for r in sorted(results, key=lambda r: r["chunk"]):
            w.writerow({k: r.get(k) for k in w.fieldnames})

    print(f"\nrequests {len(results)} | ok {len(ok)} | failed {len(failed)} | throttled (429) {throttled} | wall {fmt_t(wall)}")
    if lat:
        print(f"latency per call: p50 {fmt_t(pct(lat, .5))}, p95 {fmt_t(pct(lat, .95))}, max {fmt_t(max(lat))}")
    print(f"achieved: {rate:.2f} requests/s, {tok_per_min / 1e6:.2f} M tokens/min, {in_per_req:,.0f} input tokens per request")
    if projected:
        print(f"PROJECTION for the whole file: ~{total_chunks:,} requests, ~{total_chunks * in_per_req / 1e9:.2f} B input tokens, "
              f"~{fmt_t(projected)} at the measured rate (plus merge calls)")
    if failed:
        print("errors:", *summary["errors"], sep="\n  ")
    print(f"saved {jpath}\n      {cpath}")


if __name__ == "__main__":
    main()
