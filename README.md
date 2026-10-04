# The NVMe + PCIe Engineering Journey

A free, self-contained HTML course that takes a learner from zero to senior/principal-level
NVMe and PCIe protocol engineering. It was built from the master prompt in
[`NVMe_PCIe_10000_Line_Extreme_Master_Prompt.md`](NVMe_PCIe_10000_Line_Extreme_Master_Prompt.md).

**Open it:** [`docs/index.html`](docs/index.html) in a browser (works offline, no server needed),
or turn on GitHub Pages for the `docs/` folder (Settings → Pages → Branch: `main`, folder `/docs`).
[`docs/book.html`](docs/book.html) is the whole course as a single file for printing or offline reading.

## What is inside

| Section | Pages |
|---|---|
| Chapters 1–71, Parts I–XXI | foundations, storage, PCIe from zero, TLPs, data link layer, configuration, DMA, NVMe architecture, queues, commands, PRP/SGL, end-to-end flows, interrupts, errors, reset and power, advanced PCIe, advanced NVMe, fabrics, 2026 NVMe 2.4 updates, Linux practicals |
| Course parts XXII–XL | programming labs, trace analysis laboratory, analyzer and capture-file training, debugging methodology, failure library, performance, validation, spec reading, projects, automated and AI-assisted trace analysis, senior thinking, interviews, capstone, bug reports |
| Interactive tools | TLP decoder, NVMe command decoder, queue simulator, PRP visualizer, bandwidth/latency calculators, config-space and register decoder |
| Mastery engine | the master prompt's source-grounded method turned into practice pages |
| 60 deep-dive modules | one per topic, each with labs, four traces, five failure patterns, 20 interview questions and a mastery gate |
| Reference | glossary, mental model, misconceptions, analogy library, mathematics, example data, reference matrix, study plans, final exam, final project, references |

Every page has the A–G teaching ladder (story → technical → hardware → specification → protocol → trace →
debugging), labelled synthetic traces, labs, quizzes with hidden answers and a mastery gate. The site has
search, glossary popups, progress tracking, dark mode, collapsible sections and print styles.

## Specification baseline

NVMe 2.4 specification set: Base 2.4 (ratified 2026-07-31, published 2026-08-04), NVM Command Set 1.3,
ZNS 1.5, KV 1.4, SLM 1.3, Computational Programs 1.3, PCIe Transport 1.4, RDMA 1.3, TCP 1.3,
NVMe-MI 2.2, Boot 1.4 — checked on nvmexpress.org on 2026-10-04 (see [`notes/baseline.md`](notes/baseline.md)).
Always verify current revisions at <https://nvmexpress.org/specifications/>. Traces and dumps in the
course are synthetic teaching examples, and uncertain details are labelled as such on the page.

## Editing and building

Content lives in `src/pages/*.html` (fragments) and `src/glossary/*.json`; the outline is `src/manifest.json`.
Read [`AUTHORING.md`](AUTHORING.md) for the component vocabulary, then:

```sh
python3 tools/build.py --check ch24   # validate one page
python3 tools/build.py                # rebuild docs/ (standard library only)
```

Never edit `docs/` by hand; it is generated.
