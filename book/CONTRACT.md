# Fragment authoring contract (all agents MUST follow)

You write ONE OR MORE HTML *fragments* (no <html>, <head>, <body>, <style>, <script>) into
/home/user/Making-Hands-Dirty-With-Nvme-/book/src/<prefix>-NN.html . They are concatenated in filename
order into one book. The master prompt is at
/home/user/Making-Hands-Dirty-With-Nvme-/NVMe_PCIe_10000_Line_Extreme_Master_Prompt.md — read the line
ranges assigned to you plus: lines 1-290 (mission, output reqs, spec baseline, teaching levels A-G),
3581-3790 (truth policy, zero-hallucination template), 11263-11430 (deep-dive module template),
15772-15836 (final generator contract), 2389-2425 (visual language).

## Structure
- Each chapter: `<section class="chapter" id="chNN-slug" data-part="PART X — NAME">` ... `</section>`
  (ids unique, lowercase, prefixed with your assigned prefix e.g. `p4-ch07-layers`).
- A Part opener: `<section class="part" id="part-iv"><div class="part-num">PART IV</div><h1>PCIe From Zero</h1><p class="part-intro">...</p></section>`
- Chapter heading: `<h2><span class="num">7</span> PCIe Layer Model</h2>`; sub-sections `<h3>7.1 Physical Layer</h3>`, then `<h4>`.
- Every chapter follows the CHAPTER TEMPLATE (Why this chapter exists, Learning objectives, Prerequisites
  with back-links `<a href="#id">`, Story, Analogy + where it breaks, Beginner, Engineering, Hardware,
  Specification, Protocol, Trace, Linux, Code, Worked example, Common mistakes, Debugging connection,
  Senior insight, Lab, Exercise, Quiz, Interview Qs, Teach-back, Summary, Further reading, Next) — and
  for deep-dive topics fold in the deep-dive module items (verified/derived/impl-dependent/version-dependent,
  normal/slow/failed/ambiguous synthetic traces, 5 failure patterns, mastery gate). Use judgement:
  group template sections compactly, but content must be REAL and technically dense — no placeholders,
  no "TODO", no "[insert]".

## Components (use exactly these classes)
- Callouts: `<div class="callout kid"><div class="callout-title">🧒 Kid analogy</div><p>..</p></div>`
  types: kid 🧒, analogy 🌍, hw 🔧, spec 📘, proto 📡, trace 🔍, debug 🐞, lab 🧪, exercise 🎯,
  warn ⚠️, senior 💡, remember 📌, interview 🧠, advanced 🚀, breaks (🧩 Where the analogy breaks),
  notsaid (📭 What the spec does not say), impl (🏭 Implementation-dependent)
- Claim labels inline: `<span class="tag verified">verified</span>` / `derived` / `impl` (implementation-dependent)
  / `version` (version-dependent) / `uncertain` / `synthetic` / `conceptual`.
- Objectives: `<ul class="objectives"><li>..</li></ul>`
- Tables: `<div class="table-wrap"><table><thead>..</thead><tbody>..</tbody></table></div>`
- Code: `<pre class="code" data-kind="pseudocode"><code>...</code></pre>` data-kind ∈ executable | pseudocode |
  simulation | read-only inspection | hardware-dependent. HTML-ESCAPE < > & inside code!
- Shell commands: same with data-kind="read-only inspection" and add safety in a `warn` callout when needed.
- Formulas: `<div class="formula">BW = ...</div>` followed by `<ul class="vars"><li><b>BW</b> — ...</li></ul>`
- Diagrams: prefer inline SVG inside `<figure class="diagram"><svg viewBox="..." role="img" aria-label="..">..</svg><figcaption>Figure N.M — ...</figcaption></figure>`.
  In SVG use classes for theming: `class="box"` (rect), `class="box accent"`, `class="box alt"`, `class="ln"` (line/path,
  add marker-end="url(#arrow)"), `class="lbl"` (text), `class="lbl small"`. Do NOT hard-code fill/stroke colors.
  The shell defines `<marker id="arrow">` globally. Keep SVG text >= 11px in viewBox units relative to ~800 wide.
  ASCII diagrams allowed: `<pre class="diagram">...</pre>` (escape &lt; &gt;).
- Packet / bit-field layouts: `<div class="table-wrap"><table class="bitfield">` with header row of bit/byte offsets.
- Traces: `<div class="trace" data-trace="synthetic"><div class="trace-head">Synthetic trace — normal 4 KiB read</div><div class="table-wrap"><table>...</table></div></div>`
  data-trace ∈ synthetic | reconstructed | conceptual | excerpted. Mark abnormal rows `<tr class="bad">`, first abnormal event `<tr class="first-bad">`.
  NEVER present invented analyzer output as real. Do not imitate exact vendor screen formats; describe them generically.
- Quiz: `<div class="quiz"><h4>Quiz</h4><ol><li><p>Q?</p><details class="answer"><summary>Answer</summary><p>..</p></details></li></ol></div>`
- Interview: `<div class="interview"><h4>Interview questions</h4><ol><li><span class="lvl">Beginner</span> Q <details class="answer"><summary>Model answer</summary>..</details></li></ol></div>`
- Labs: `<div class="lab"><div class="lab-head">🧪 Lab N.M — Name <span class="safety safe">SAFE</span></div>...</div>`
  safety ∈ safe | caution | expert (SAFE LAB / CAUTION LAB / EXPERT LAB). Include objective, setup, steps, observations,
  questions, solution (in <details class="answer">), extension.
- Mastery gate: `<div class="mastery"><div class="callout-title">✅ Mastery gate</div><ul class="checklist"><li>..</li></ul></div>`
- Glossary links: when using a key acronym, you may link `<a class="gl" href="#g-tlp">TLP</a>` — glossary ids are
  `g-` + lowercase acronym with non-alphanumerics removed (g-tlp, g-dllp, g-ltssm, g-msix, g-prp, g-sgl, g-aer, g-bar,
  g-mmio, g-dma, g-sq, g-cq, g-cqe, g-lba, g-zns, g-nvmemi, g-sriov, g-bdf, g-ecrc, g-lcrc, g-mps, g-mrrs, g-cc, g-csts,
  g-cap, g-aqa, g-asq, g-acq, g-iommu, g-ftl, g-nand, g-fc, g-ack, g-nak, g-ur, g-ca, g-cpl, g-cpld, g-mrd, g-mwr ...).
  Expand every acronym on first use in each chapter anyway.

## Truth rules
- Spec baseline (confirmed via press coverage: NVMe 2.4 set released Aug 4 2026; highlights Post-Quantum Cryptography,
  PCIe Exported NVM Subsystem Migration, Voltage Monitoring, Rate Limiting, Restore Manufacturing Default Settings):
  Base 2.4, NVM CS 1.3, ZNS 1.5, KV 1.4, SLM 1.3, Computational Programs 1.3, PCIe transport 1.4, RDMA 1.3, TCP 1.3,
  NVMe-MI 2.2, Boot 1.4 (individual sub-spec revision numbers: per course brief — tell learner to confirm on nvmexpress.org).
- Do NOT invent spec section numbers. Do not invent details of 2.4 features beyond their purpose.
- Well-established facts you may state (label verified): register offsets CAP 00h, VS 08h, INTMS 0Ch, INTMC 10h, CC 14h,
  CSTS 1Ch, NSSR 20h, AQA 24h, ASQ 28h, ACQ 30h, CMBLOC 38h, CMBSZ 3Ch; doorbells from 1000h, stride 4<<CAP.DSTRD;
  SQE 64 bytes, CQE 16 bytes; PRP1/PRP2 DPTR at bytes 24-39; CDW10-15; admin opcodes (Delete I/O SQ 00h, Create I/O SQ 01h,
  Get Log Page 02h, Delete I/O CQ 04h, Create I/O CQ 05h, Identify 06h, Abort 08h, Set Features 09h, Get Features 0Ah,
  Async Event Request 0Ch, Namespace Mgmt 0Dh, Firmware Commit 10h, Firmware Image Download 11h, Format NVM 80h,
  Sanitize 84h); NVM opcodes (Flush 00h, Write 01h, Read 02h, Write Uncorrectable 04h, Compare 05h, Write Zeroes 08h,
  DSM 09h, Verify 0Ch, Copy 19h); CQE DW2 SQHD/SQID, DW3 CID, phase tag, status field (SCT/SC, DNR, More);
  PCIe rates 2.5/5/8/16/32/64 GT/s (Gen1-6), 8b/10b Gen1-2, 128b/130b Gen3-5, Gen6 PAM4 + FLIT mode 1b/1b with FEC;
  Gen7 128 GT/s spec released 2025 (label version). TLP fmt/type (MRd, MWr, CplD, Cpl, CfgRd0/1, Msg), 10-bit tags
  (Gen4+ optional), 14-bit tags in Gen6 FLIT mode; DLLP Ack/Nak/UpdateFC/InitFC/PM; LTSSM states Detect, Polling,
  Configuration, L0, Recovery, L0s, L1, L2, Hot Reset, Loopback, Disabled.
- Label anything implementation-dependent or uncertain. Traces you invent: label synthetic.
- Linux commands: real ones (lspci -vvv, lspci -tv, setpci (warn: writes are dangerous), nvme list, nvme id-ctrl,
  nvme id-ns, nvme smart-log, nvme error-log, nvme get-log, nvme get-feature, dmesg, journalctl -k, /sys/bus/pci, fio,
  blktrace, perf, bpftrace, iostat). Don't fabricate exact output that looks authoritative; you may show "illustrative,
  abbreviated" output clearly labelled.

## Size & quality
- Be LONG, dense and genuinely useful: target 60–120 KB of HTML per fragment file; split into multiple files
  (<prefix>-01.html, <prefix>-02.html…) if larger; write each file with the Write tool, then continue the next.
- Make HTML valid: close every tag, escape &,<,> in text. Do not add <style>/<script>.
- Finally run: python3 -c "import html.parser,sys; ..." or `python3 /home/user/Making-Hands-Dirty-With-Nvme-/book/check.py <files>`
  to check tag balance, and fix any errors it reports.
- Do NOT git commit. Reply with: list of files, chapter ids, and approx sizes.
