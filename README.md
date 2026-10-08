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
