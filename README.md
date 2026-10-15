

# The NVMe + PCIe Engineering Journey

**Open `NVMe_PCIe_Engineering_Journey.html` in any browser.** It is a single self-contained file (no internet needed):
150 chapters across Parts I–LXXV, generated from `NVMe_PCIe_10000_Line_Extreme_Master_Prompt.md`.

Includes 110 figures, 17 of them interactive step-through diagrams (click a step to see who does what).
Features: sticky searchable table of contents (press `/`), dark/light theme, per-chapter "completed" tracking
(saved in your browser), collapsible chapters, hidden quiz/interview answers ("Show answers" reveals all),
glossary hover pop-ups, copy buttons on code, print-to-PDF styling (answers expand when printing).

Spec baseline: NVM Express 2.4 specification family (released 4 Aug 2026). All traces are labelled synthetic;
uncertain / implementation- / version-dependent claims are tagged — always confirm against nvmexpress.org and PCI-SIG.

## Rebuilding
Chapters live as HTML fragments in `book/src/`; `book/shell.html` holds the page styling and script.
`python3 book/build.py` reassembles the book; `python3 book/check.py book/src/*.html` validates fragments.

# PEX Trace Error Atlas

**Open `PEX_Trace_Error_Atlas.html`.** A catalogue of 326 NVMe and PCIe failure modes and, for each one, whether a
PEX trace (Teledyne LeCroy PCIe analyzer capture) can show it: Visible, Partly visible or Not visible — and what
other evidence proves it. Covers status codes, timeouts and time limits, silent failures (e.g. a trim that completes
successfully but deallocates nothing), PCIe AER and physical/link-layer errors, controller and host failures, trace
rule violations (with a checker script for exported traces), performance failures, and using AI on trace exports.

Grounded in the Linux kernel v7.2 NVMe/PCI source, QEMU's NVMe controller model and libnvme; the NVMe/PCIe specs
themselves could not be downloaded here, so spec wording is paraphrased and unconfirmed items are tagged.
Rebuild: `python3 atlas/build.py` (fragments in `atlas/src/`).



# Commands to run

pip install requests tiktoken
python llm_stress_test.py --csv D:\traces\export.csv --base-url <URL> --api-key <KEY> --model <MODEL> --context 128000 --rps 5 --concurrency 20 --max-requests 20
