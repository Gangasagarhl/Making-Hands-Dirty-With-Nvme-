# Authoring guide — The NVMe + PCIe Engineering Journey

This repository builds a static, self-contained HTML course from content fragments.
Anyone adding or editing a page should read this file first.

```
src/manifest.json        course outline: every page, its id, title, kind and the master-prompt lines it covers
src/pages/<id>.html      the content fragment for one page (no <html>, <head>, <body> or <h1>)
src/glossary/*.json      glossary entries (core.json wins on duplicate keys; others are merged)
src/assets/              academy.css and academy.js, copied to docs/assets
tools/build.py           validates fragments and writes docs/ (one HTML page per manifest entry + docs/book.html)
docs/                    the built site (GitHub Pages serves this folder). Never edit docs/ by hand.
```

Build and check:

```sh
python3 tools/build.py --check ch04 ch05   # validate specific fragments
python3 tools/build.py                     # build the whole site into docs/
```

Every page links to every other page by file name: `<a href="ch24.html#cap">CAP register</a>`.
All pages live flat in `docs/`, so links never contain directories.

---

## 1. Page skeleton

A fragment is a series of `<section>` blocks, each starting with an `<h2>`. The build numbers
headings (Chapter 24 → 24.1, 24.2 …; module 07 → M07.1 …), builds the on-page table of contents,
and makes each section collapsible. Do **not** type numbers into headings.

```html
<p class="lead">One or two sentences saying what this page gives the learner.</p>

<section id="why">
  <h2>Why this chapter exists</h2>
  <p>…</p>
  <h3>A sub-topic</h3>
  <p>…</p>
</section>
```

Rules:
- Start at `<h2>`. Never use `<h1>` (the build adds the title from the manifest).
- Give every `<section>` a short, unique, lowercase `id` (letters, digits, hyphens).
- Use `<h4>` freely for small labelled blocks; only `<h2>`/`<h3>` are numbered.
- Expand every acronym on first use on each page: `Transaction Layer Packet (TLP)`.

## 2. Callouts (the visual language)

```html
<aside class="callout kid"><p>…</p></aside>
<aside class="callout insight" data-title="Why seniors look here first"><p>…</p></aside>
```

Types and default titles:

| type | title | use for |
|---|---|---|
| `kid` | 🧒 Kid analogy | the Level A story |
| `analogy` | 🌍 Real-world analogy | an adult analogy |
| `breaks` | 🧩 Where the analogy breaks | always follow an analogy with this |
| `hw` | 🔧 Hardware view | |
| `spec` | 📘 Specification view | |
| `proto` | 📡 Protocol view | |
| `trace` | 🔍 Trace view | prose about traces (the trace itself uses `.trace`, below) |
| `linux` | 🐧 Linux view | |
| `debug` | 🐞 Debugging | |
| `lab` | 🧪 Lab | |
| `exercise` | 🎯 Exercise | |
| `warn` | ⚠️ Warning | |
| `insight` | 💡 Senior insight | |
| `remember` | 📌 Remember | |
| `interview` | 🧠 Interview | |
| `advanced` | 🚀 Advanced | |
| `verified` | ✅ Verified against the specification | |
| `derived` | 🧮 Derived (reasoned, not quoted) | |
| `impl` | ⚙️ Implementation-dependent | |
| `version` | 🗓️ Version-dependent | |
| `notsaid` | 🚫 What the specification does not say | |
| `mastery` | 🏁 Mastery gate | put a `<ul class="checklist">` inside |
| `prereq` | ↩️ Prerequisite reminder | link backward to the chapter that taught it |
| `safety` | 🛡️ Safety | hardware/data safety level |

`data-title` replaces the default title text but keeps the icon.

## 3. Inline labels

```html
<span class="badge verified">Verified</span>      <!-- stated by the cited spec -->
<span class="badge derived">Derived</span>        <!-- reasoned from verified facts -->
<span class="badge impl">Implementation-dependent</span>
<span class="badge version">Version-dependent</span>
<span class="badge uncertain">Uncertain</span>
<span class="badge normative">Normative</span>  <span class="badge informative">Informative</span>  <span class="badge optional">Optional</span>
<span class="badge safe">Safe lab</span>  <span class="badge caution">Caution lab</span>  <span class="badge expert">Expert lab</span>
<cite class="spec">NVMe Base Specification 2.4 — Controller Registers</cite>
```

Cite documents and section **names**, never invented section **numbers**.

## 4. The teaching ladder (Levels A–G)

```html
<div class="ladder">
  <div class="rung" data-rung="A"><p>Story…</p></div>
  <div class="rung" data-rung="B"><p>Simple technical…</p></div>
  <div class="rung" data-rung="C"><p>Hardware…</p></div>
  <div class="rung" data-rung="D"><p>Specification…</p></div>
  <div class="rung" data-rung="E"><p>Protocol…</p></div>
  <div class="rung" data-rung="F"><p>Trace…</p></div>
  <div class="rung" data-rung="G"><p>Debugging…</p></div>
</div>
```

## 5. Code and commands

Every code block declares what it is (`data-kind` is required):
`executable`, `pseudocode`, `simulation`, `read-only`, `hardware-dependent`.

```html
<pre class="code" data-kind="read-only" data-lang="sh"><code>sudo lspci -vvv -s 01:00.0</code></pre>
<pre class="code" data-kind="executable" data-lang="python" data-title="tlp_decode.py"><code>…</code></pre>
```

Escape `<`, `>` and `&` inside code (`&lt;` `&gt;` `&amp;`). Use `<span class="cm">` for comments
if you want them dimmed. Never invent command output that looks authoritative; when you show
output, mark it as illustrative in the surrounding text and keep values plausible.

## 6. Traces

Every trace states its provenance (`data-kind` is required): `synthetic`, `real`, `excerpted`,
`reconstructed`, `conceptual`. Everything written for this course is `synthetic` or `conceptual`.

```html
<div class="trace" data-kind="synthetic" data-title="Normal 4 KiB READ, Gen4 x4">
  <table class="trace-table">
    <thead><tr><th>#</th><th>Dir</th><th>Time (ns)</th><th>Type</th><th>Fields</th><th>Meaning</th></tr></thead>
    <tbody>
      <tr class="dir-down"><td>1</td><td>Host→Dev</td><td>0</td><td>MWr32</td><td>Addr=BAR0+0x1008 Len=1 Data=0x00000005</td><td>SQ1 tail doorbell = 5</td></tr>
      <tr class="dir-up"><td>2</td><td>Dev→Host</td><td>820</td><td>MRd64</td><td>Addr=0x1_2340_0100 Len=16 Tag=0x21</td><td>Controller fetches the 64-byte SQE</td></tr>
      <tr class="abnormal"><td>9</td><td>Dev→Host</td><td>…</td><td>Cpl</td><td>Status=UR</td><td>First abnormal event</td></tr>
      <tr class="note"><td colspan="6">Commentary rows use class="note".</td></tr>
    </tbody>
  </table>
  <p>Optional explanation under the table.</p>
</div>
```

Row classes: `dir-down` (host→device), `dir-up` (device→host), `abnormal`, `note`.

## 7. Diagrams

**Sequence diagram** (rendered to SVG at build time). Actors are separated by `|`. Each `li`
is an arrow (`data-from`, `data-to`) or a note (`data-note="Actor"` or `"ActorA|ActorB"`).
Optional: `data-t="120 ns"` timestamp, `class="dashed"` (responses), `class="bad"` (errors).
Keep labels short (under ~40 characters).

```html
<figure>
  <ol class="seq" data-actors="Driver (CPU)|Host memory|NVMe controller">
    <li data-from="Driver (CPU)" data-to="Host memory">write 64-byte SQE at SQ tail</li>
    <li data-from="Driver (CPU)" data-to="NVMe controller" data-t="0 ns">MWr: SQ1 tail doorbell</li>
    <li data-from="NVMe controller" data-to="Host memory">MRd: fetch SQE</li>
    <li data-from="Host memory" data-to="NVMe controller" class="dashed">CplD: 64 bytes</li>
    <li data-note="NVMe controller">execute command</li>
  </ol>
  <figcaption>Conceptual sequence of a command submission.</figcaption>
</figure>
```

**Queue ring** (rendered to SVG):

```html
<div class="ring" data-label="I/O Submission Queue 1 (8 entries)" data-size="8" data-head="2" data-tail="5"></div>
```

Optional `data-head-label` / `data-tail-label`.

**Vertical flow / stack**:

```html
<div class="flow"><div>Application<small>read()</small></div><div class="hl">NVMe driver</div><div>Controller</div></div>
```

**ASCII diagrams**: `<figure><pre class="diagram">…</pre><figcaption>…</figcaption></figure>`.

**Inline SVG** is welcome for anything else. Use `class="svg-diagram"` on a wrapping `<div>`;
inside the SVG use `currentColor` or the classes `muted`, `mono`; avoid hard-coded black/white so it
works in dark mode.

## 8. Packet and register layouts

Use a 32-column bit table for DWORD layouts (`table.packet`). Columns are bits 31→0; use
`colspan` for field width. `td.rsvd` greys reserved bits; `td.hl` highlights a field.

```html
<table class="packet">
  <thead><tr><th class="row-label">DW</th><th colspan="8">31…24</th><th colspan="8">23…16</th><th colspan="8">15…8</th><th colspan="8">7…0</th></tr></thead>
  <tbody>
    <tr><td class="row-label">DW0</td><td colspan="3">Fmt</td><td colspan="5">Type</td><td colspan="24">…</td></tr>
  </tbody>
</table>
```

Ordinary field tables: a plain `<table>` with `<caption>` and `<thead>`. Columns such as
*Offset · Bits · Field · Owner · Purpose · What breaks if wrong* work well.

## 9. Formulas

```html
<div class="formula">
  <div class="eq"><var>L</var> = <var>λ</var> × <var>W</var></div>
  <dl class="vars">
    <dt>L</dt><dd>average number of commands outstanding (queue depth actually in flight)</dd>
    <dt>λ</dt><dd>throughput in commands per second (IOPS)</dd>
    <dt>W</dt><dd>average time each command spends in the system, in seconds</dd>
  </dl>
</div>
```

Every variable must be defined.

## 10. Quizzes, answers, hints and solutions

```html
<ol class="quiz">
  <li>
    <p>Which agent writes the SQ tail doorbell?</p>
    <ul class="options" data-answer="1">
      <li>The host driver</li>
      <li>The NVMe controller</li>
      <li>The root complex</li>
    </ul>
    <details class="answer"><summary>Answer and reasoning</summary><p>…why…</p></details>
  </li>
  <li><p>Open question…</p><details class="answer"><summary>Model answer</summary><p>…</p></details></li>
</ol>
<details class="hint"><summary>Hint</summary><p>…</p></details>
<details class="solution"><summary>Solution</summary><p>…</p></details>
<details class="expand"><summary>Go deeper: …</summary><p>…</p></details>
```

`data-answer` (1-based) makes options clickable; omit it for open questions.

## 11. Mastery gates

```html
<aside class="callout mastery">
  <p>Do not move on until you can:</p>
  <ul class="checklist"><li>explain …</li><li>draw …</li><li>find … in a trace</li></ul>
</aside>
```

Checklist ticks are saved in the reader's browser.

## 12. Glossary

Mark the first important use of a term on a page:
`<span class="g" data-term="tlp">TLP</span>` → becomes a hover popup and a link into the glossary.
Keys are lowercase. If the key is missing, add it to a glossary JSON file:

```json
[{"key": "tlp", "term": "TLP", "exp": "Transaction Layer Packet",
  "def": "The PCIe packet that carries a request or completion between two devices.", "see": ["ch10", "dd11"]}]
```

## 13. Truth policy (non-negotiable)

- Baseline: NVMe 2.4 specification set (Base 2.4 ratified 2026-07-31, published 2026-08-04;
  NVM Command Set 1.3, ZNS 1.5, KV 1.4, SLM 1.3, Computational Programs 1.3, PCIe Transport 1.4,
  RDMA Transport 1.3, TCP Transport 1.3, NVMe-MI 2.2, Boot 1.4). See `notes/baseline.md`.
- Never invent section numbers, opcodes, offsets, bit positions or status codes. If you are not
  certain, describe the field by name and tell the reader to confirm it in the specification,
  with a `badge uncertain` or a `callout notsaid`.
- Separate normative requirements from your interpretation (badges `normative` / `derived`).
- Label implementation-dependent and version-dependent behaviour.
- Every trace has provenance; every code block has a kind; every formula defines its variables;
  every hardware procedure states its safety level.
- Analogies are always followed by where they break.
