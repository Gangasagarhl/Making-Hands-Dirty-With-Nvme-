# pex_checker: streaming NVMe/PCIe error checker for PEX trace exports

`pex_checker` reads one CSV exported from a Teledyne LeCroy PCIe analyzer capture (`.pex`, PCIe Protocol Analysis
13.28.b.15 packet export: about 112–120 columns, one row per packet, 2–5 GB). It streams every row once, in constant
memory, and rebuilds every NVMe command from its packets: doorbell → SQE fetch → data → CQE → MSI-X → CQ head
doorbell. For each problem it reports:

- **where** it happened: packet number, CSV row and timestamp;
- **why**, in the words of the matching PEX Trace Error Atlas card, plus how to confirm it;
- its **category**: timing, timeout, reordering, protocol-state, status-code, link-layer or performance;
- the likely **fault side**: host, device, link/platform or either (from `data/fault_side.txt`);
- enough **context to reproduce it**: the exact SQE as an `nvme-cli` passthru command, the queue depth and link state
  when the command was submitted, the preceding commands, and the evidence packets with the rows around them.

Standard library only (Python ≥ 3.9). `pytest` is needed only to run the tests.

```
python -m pex_checker TRACE.csv --out OUTDIR            # run from the repository root
```

---

## 1. Exporting the capture from the analyzer

Menu names differ between software versions, so this section says *what* to export, not where the button is.

1. **Export at packet level**, one row per packet (TLP, DLLP, ordered set, link event). Transaction-level or
   NVMe-level views are not enough: the checker rebuilds NVMe itself.
2. **Populate the `Data` column.** SQE, CQE, PRP-list, MSI-X and register values are read from TLP payloads. Without
   payloads the commands are still timed from doorbells and fetches, but opcode, CID, status and PRPs are unknown
   (each such command is flagged `SQE payload not in export`). If your tool truncates payloads, at least 64 bytes per
   TLP are needed for SQEs. Up to 4 KiB per TLP is better, because Identify data is then decoded (MDTS, LBA size).
3. **Export the whole window, with nothing hidden.** Filtered views export only what is shown. If DLLPs are hidden,
   Ack/Nak and flow-control rules see nothing. If ordered sets or link events are hidden, training and L1 rules see
   nothing.
4. **Keep the timestamp column** (`Time Stamp`). `Time Delta` alone also works, because the tool accumulates it.
5. **Capture from before the driver loads** if you need NVMe decode. The capture must include BAR0 programming, the
   CC/AQA/ASQ/ACQ writes and the Create I/O CQ/SQ commands. To get them, rebind the driver or run `nvme reset` during
   the capture (CAUTION: this disrupts I/O). For a capture that starts late, see `--bar0`, `--sq` and `--cq` below.

Header names are matched case-insensitively through aliases, so `Packet`/`Packet #`, `Direction`/`Dir`,
`Time Stamp`/`Timestamp` and similar spellings all work. The duplicate `Device ID` column is handled: the first
occurrence is the TLP one. Any canonical column that cannot be mapped is listed in `summary.json → schema`, and the
rules that need it are disabled; they are never guessed.

## 2. Running

```
python -m pex_checker TRACE.csv --out OUTDIR [options]
python -m pex_checker --help
```

Progress goes to stderr: rows, rows/s, and % of the file read by bytes, with an ETA.

### Thresholds

All thresholds are **configurable defaults**, not specification values. Each one is printed in
`summary.json → config`.

| Option | Default | Used by |
|---|---|---|
| `--io-timeout-s` / `--admin-timeout-s` | 30 / 60 (Linux driver defaults) | T-22, T-23, T-11 |
| `--pending-min-ms` | 100 | T-01 / C-08: no CQE by the end of the trace |
| `--cpl-timeout-ms` | 50 (upper end of the default range named on atlas card T-33) | T-33, L-16 |
| `--ack-latency-us` | 10 | X-20, T-37 (the spec limit depends on speed, width and MPS) |
| `--fc-stall-us` | 30 | T-38, X-22 |
| `--ltssm-episode-ms` | 10 | T-39 |
| `--l1-exit-us` | 64 (used only when neither DevCap nor LinkCap was captured) | T-45 |
| `--msix-window-us` | 1000 (+ the interrupt-coalescing time if it was set in the capture) | V-15 |
| `--host-stall-ms` / `--cq-stall-ms` | 100 / 1000 | T-32 / V-07 |
| `--outlier-k`, `--outlier-floor-us`, `--outlier-min-samples` | 3, 1000, 50 | T-47: outlier = latency > max(k × running p99, floor) |
| `--recovery-storm-n`, `--gap-ms`, `--max-retries` | 5 per s, 100, 5 | X-11/L-26, X-30, T-24 |

### Overrides, for values normally learned from the trace

`--bar0 0xF0000000`, `--dstrd`, `--mps`, `--mrrs`, `--rcb`, `--mdts-bytes`, `--lba-size`, `--page-size`;
`--sq QID:BASE:SIZE:CQID` and `--cq QID:BASE:SIZE:VECTOR` (repeatable) for queues created before the capture
started; `--time-unit` (`auto|s|ms|us|ns|ps`; by default integer timestamps are read as ns and decimals as s);
`--data-format dwords` if your Data column prints 8-hex-digit DWs MSB first; `--link N` to analyse one link of a
multi-link export.

### Other options

`--verify-data` hashes every written LBA and compares it with later reads (D-15, D-03, D-05, D-20). It is slower
and uses more memory, bounded by `verify_lbas`. `--no-context` skips pass 2. `--max-per-card` caps the findings
kept per card (default 200; info findings are capped at 20; every occurrence is still counted).
`--fail-on error|warning` makes the exit code usable in CI.

## 3. Outputs (in `--out`)

| File | Content |
|---|---|
| `findings.jsonl` | One finding per line. Fields: `card_id`, `card_name`, `visibility`, `rule`, `category` (plus `subcategory` `data-integrity` for DMA/data findings), `severity` (error/warning/info), `fault_side` with `fault_side_code`, `pkt`, `row`, `timestamp`, `what` (the measured facts), `why`, `typical_causes`, `how_to_confirm`, `spec_basis`, `test_to_provoke` and `atlas_in_trace` (all from the atlas card), `extra_evidence_needed` (for partly-visible cards), `related_cards`, `metrics`, `evidence` (role/row/pkt/ts of every packet involved), `evidence_rows` and `context_rows` (decoded CSV rows), and `link_state`. For a finding tied to an NVMe command it also has `sqid`, `cid`, `opcode`, `opcode_name`, `nsid`, `slba`, `nlb`, `bytes`, `queue_depth_at_submit` (this SQ / all queues, including the command), `link_state_at_submit`, `preceding_commands`, `command_packets`, `stage_breakdown_us` and `repro` (`nvme_cli` with the exact SQE fields, a `friendly` equivalent, a `fio_load` at the same queue depth, a safety note, `sqe_hex`, `sqe_fields`). |
| `commands.csv` | One row per NVMe command, written as each command finishes (streaming). Columns: identity (SQ, CID, opcode, NSID, SLBA, NLB, bytes, FUA), status (SCT/SC/DNR and name), row, packet and timestamp of every lifecycle packet, the seven stage latencies plus the total, queue depth, link speed/width at submit, and flags. |
| `summary.json` | File, rows, trace duration, pass-1/pass-2 wall time, rows/s, peak RSS; schema mapping and missing columns; what the decoder learned and where (BAR0, stride, page size, MPS/MRRS, MDTS, LBA size, CAP, queues, MSI-X, Identify Controller); NVMe counts; latency percentiles per opcode and per stage; PCIe TLP mix and completion latency; link summary (speed/width, recoveries, L1 episodes, DLL replay/Nak/LCRC counts, Ack latency); findings by severity, category and card; suppressed counts; coverage numbers; full config. |
| `report.html` | Self-contained (no network), light/dark aware, usable at phone width. Has summary tiles, the learned configuration, a findings-by-card table, latency tables and the link summary. Findings are grouped by category and can be filtered, and each one opens to show the evidence rows, the context rows (the finding row is highlighted), a stage timeline bar, cause, fault side, the atlas texts and the nvme-cli repro. |
| `coverage.md` | All 326 atlas cards. Each is marked implemented, covered by a sibling rule, or not checkable (with the reason), with the number of times it was seen in this trace. |

## 4. Design

### 4.1 Data sources rebuilt from the rows

| # | Source | How |
|---|---|---|
| 1 | Config space | Each CfgRd/CfgWr is paired by (Requester ID, Tag) with its Cpl/CplD. The tool tracks BAR sizing and assignment (0x10–0x24, 64-bit BARs), walks the capability list (0x34 → PCIe cap, MSI-X, PM; extended caps → AER), and decodes DevCap (MPS supported, L1 acceptable latency), DevCtl (MPS, MRRS, RO, ext tags, Initiate FLR), DevSta, LinkCap (speed, width, L1 exit latency), LinkSta, DevCtl2 (CTO disable, 10-bit tags), MSI-X control/table/PBA, PMCSR (D3hot→D0) and the AER status registers. |
| 2 | NVMe registers | Downstream MWr/MRd to BAR0 + 0..0xFFF. CAP (MQES, CQR, TO, DSTRD, MPSMIN), CC (EN, SHN, MPS), CSTS read completions (RDY, CFS, SHST, NSSRO, PP, all-ones), AQA/ASQ/ACQ (admin queues are created at CC.EN 0→1), NSSR, CRTO. |
| 3 | Admin commands | Create/Delete I/O CQ/SQ (base, size, PC, IEN, vector, SQ→CQ map, applied only on a successful CQE), Set Features (Number of Queues, Interrupt Coalescing, Power Management, HMB), Abort, AER results, Firmware Commit, Format, Doorbell Buffer Config. Identify Controller/Namespace and log pages whose data DMA is in the trace give MDTS, SGLS, VWC, ACL, MTFA, RTD3E, power-state EXLAT, LBA size, DLFEAT, NGUID/EUI-64, SMART critical warnings, self-test and sanitize results. |
| 4 | Doorbells | Downstream MWr to BAR0 + 0x1000 + (2·qid)·(4<<DSTRD) for the SQ tail and +(2·qid+1)·(4<<DSTRD) for the CQ head. Without a BAR0 write in the capture, BAR0 is inferred from a cluster of 4-byte downstream writes and reported as derived. |
| 5 | SQE fetch | Upstream MRd into an SQ ring, mapped to slot = (addr − base)/64 (multi-SQE fetches are split). It is completed by one or more CplDs, reassembled using Byte Count and Lower Address, into 64-byte SQEs (opcode, FUSE, PSDT, CID, NSID, MPTR, PRP1/PRP2 or SGL1, CDW10–15). Fetches of slots the host never rang are counted as prefetch ("ghost") fetches, not commands. |
| 6 | Data | An address index maps each 4 KiB page to the regions of SQ rings, CQ rings, PRP lists, SGL segments, DSM range lists and data buffers. PRP lists and SGL segments are themselves read from their fetch completions, with list chaining. Upstream MWr is read data; upstream MRd plus downstream CplD is write data. Each is attributed to its command by address. |
| 7 | CQE | Upstream 16-byte MWr into a CQ ring → DW0, SQHD, SQID, CID, phase, status (SC, SCT, CRD, More, DNR). |
| 8 | MSI-X | Upstream MWr to 0xFEE0_0000–0xFEEF_FFFF. It is matched to a vector through the MSI-X table writes seen in the capture, or, if there is no table, through the CQ with the oldest unannounced CQE (learned per message data). |
| 9 | Link / DLL | PSN continuity and replays (a re-sent PSN), Ack/Nak sequence, LCRC/ECRC/CRC16 marked bad, InitFC/UpdateFC credit accounting (header/data, P/NP/Cpl, scaled), TS1/TS2 rate and lanes, Link Event text (Recovery, speed/width change, L1 entry/exit, electrical idle, training, physical errors), PM DLLPs, Msg ERR_COR/ERR_NONFATAL/ERR_FATAL and LTR, Cpl status UR/CA/CRS, EP (poisoned). |

### 4.2 Data structures (constant memory)

Memory is bounded by the number of queues, the queue depth, the outstanding tags and fixed windows, never by file
size.

- **Queue table**: per qid, `SQ{base, size, cqid, tail, fetch head, last SQHD, pending slots}` and
  `CQ{base, size, head, tail, expected phase, vector, IEN, posted-not-released commands, CQEs awaiting MSI-X}`.
- **Address index**: a dict from 4 KiB page to a list of `Region(start, end, kind, owner)`. A command's regions are
  marked dead when the host releases its CQE. Dead regions are kept for the last `recent_completed` (4096) commands,
  so late DMA can be detected (V-16), and are then removed.
- **Outstanding non-posted requests**: `(Requester ID, Tag) → {kind, addr, bytes, remaining, next address, ts, row,
  purpose, owner}`. Used for completion timeout, split completions, Byte Count/Lower Address/RCB checks, unexpected
  completions and tag reuse. Requests reported as timed out are kept in a small table so that a late completion is
  recognised (L-16).
- **Command lifecycle**: `(sqid, cid) → Cmd` with the row, packet and timestamp of the doorbell, SQE fetch, SQE
  completion, first and last data, CQE, MSI-X and CQ head doorbell; bytes moved; status; queue depth at submit; link
  state; the 6 preceding commands; flags. Before the CID is known, a pending table `(sqid, slot) → doorbell info`
  holds the command.
- **Statistics**: fixed log-scale histograms (8 buckets per octave, from 10 ns to about 1000 s) per (opcode, size
  bucket) and per stage. The running p99 drives the outlier rule, and the histograms give the summary percentiles.
- **Link intervals**: a deque of the last 4096 intervals (Recovery, L1 + exit, replay bursts, credit stalls, speed
  changes) for correlating slow commands with link activity.
- **Pass 2**: the target rows (each finding's evidence rows ±1 and the finding row ±`context_rows`) form a set of
  integers. The file is re-read, and only target rows are split.

### 4.3 Command stages and "why it took longer"

`doorbell → fetch` (the device's fetch latency) · `fetch → SQE completion` (the host as completer) · `SQE → first
data` (device processing / media) · `data transfer` · `last data → CQE` (device) · `CQE → MSI-X` · `MSI-X → CQ head
doorbell` (host processing).

When a command's latency exceeds max(k × running p99, floor) for its (opcode, size bucket), the T-47 finding:

1. subtracts each stage's running median and names the **dominant stage**;
2. lists every **overlapping link interval** (Recovery, L1 exit, replay burst, credit stall, speed change);
3. notes whether the doorbell was the first traffic after an **L1 exit** (P-07);
4. attributes the delay accordingly. If the delay is in a device stage and nothing on the link overlapped, it is
   attributed to the device (firmware, media retry, GC or thermal: T-48, P-05, T-49). Slow host completions point to
   the host, and link events point to the link. Flush and FUA writes map to T-51.

### 4.4 Classification and fault side

Every finding has one of the seven categories. The atlas card ID comes from the rule. The fault side comes from
`data/fault_side.txt` (H/D/L/B/N/A), but a rule overrides it when the packet direction proves it. For example, a UR
completion to the device's DMA read is reported as `either` (IOMMU or bad address), and a credit stall is charged to
the receiver that withheld UpdateFC.

### 4.5 Rules

243 cards have their own rule, 34 are reported through a sibling rule and 49 are not checkable. The full list, with
reasons, is in `coverage.md`.

- **Status codes**: every non-zero CQE status maps to its S-card through the complete SCT/SC table of atlas chapter
  II.5, including libnvme/QEMU extras, vendor-specific and reserved SCTs, and codes that cannot occur on PCIe. More=1
  maps to S-71. AER results map to S-78…S-85. SMART critical warnings, self-test, sanitize and firmware-slot log
  data map to S-72…S-77, S-88, S-89 and S-92. CSTS.NSSRO maps to S-91.
- **Timeouts**: T-01/C-08 (no CQE or SQE never fetched by the end of the trace, with the stage it is stuck in),
  T-22/T-23/T-11 (beyond the host timeout), T-33 (completion timeout, late completion), T-34 (CTO disabled), T-02,
  C-02, C-03, C-04, C-12, C-16 (register timing), T-09 (Abort that did not abort), T-18, T-24 (retries), T-21
  (polled completion), V-15, V-07 (CQ stall), T-40, T-41, L-30, T-42, T-43 (reset, D3 and FLR timing).
- **Queue protocol**: V-01…V-11 (doorbell range, missing queue, partial doorbell, SQ overrun or backwards move, CQ head
  past tail, CQ overflow, CID reuse, unknown CID, wrong SQID or SQHD, phase), D-29, V-12.
- **Ordering**: V-13 (CQE before data), V-14 (MSI-X before CQE), V-16 (DMA after completion), V-33 (RO bit on a CQE
  or MSI-X).
- **Command format**: V-17…V-25 (PRP alignment, PRP2 offset, PRP list chaining, MDTS, reserved fields, FUSE, admin
  opcode on an I/O queue, SGL rules, queue-creation parameters against CAP and the granted queue count).
- **PCIe transaction layer**: V-26…V-32 (MPS, MRRS, 4 KiB crossing, RCB, Byte Count/Lower Address, unexpected
  completion, tag reuse), L-18 (Length vs payload), L-12/V-37/L-38 (poison, and poisoned data accepted), L-15/L-20
  (CA/UR, including UR to the device's DMA), V-34 (ECRC), L-32/P-12 (MPS vs capability), L-40 (tags vs
  capability), L-34 (UR to an ATS request), D-31/D-32 (DMA outside the buffer, DSM range over-read), C-26 (upper
  address bits dropped).
- **Data link layer**: X-15, L-02, X-16, X-17, X-18, X-19, T-35, T-37, X-20, X-21, X-22, T-38, L-13, X-30 (sequence
  gaps without Nak, Acks for PSNs never seen, timestamp gaps or backwards steps).
- **Physical layer / LTSSM**: X-01, X-02, X-03, X-05, X-07, X-08, X-10, L-01, X-04, X-06, X-09, X-11/L-26, X-12,
  X-13, T-39, T-40, L-39, L-25, T-44, L-28, X-14, X-28 (analyzer re-lock heuristic), T-45, T-46 (LTR vs exit), X-26
  (error messages and storms). The AER status registers (L-01…L-23) are reported only if software reads them over
  this link.
- **Partly visible records**: D-01 (deallocate ranges decoded from the range-list DMA; the finding says a read-back
  is needed), D-05, D-07, D-08, D-26, P-13, P-14, T-16. With `--verify-data`, D-15/D-03/D-05/D-20 compare payloads.

### 4.6 Robustness to real exports

The reader works on 8 MB binary blocks. It decodes and splits lines itself and only uses the `csv` module for lines
that contain quotes, so quoted fields, embedded commas and newlines work. It accepts a leading title line before
the header, `,` `;` or tab delimiters, a UTF-8 BOM or UTF-16 files, and short rows. Hex is read with or without
`0x`, with `_` or `:` separators or a trailing `h`. Tags and lengths may be decimal or hex. Timestamps can be decimal
s, integer ns, or carry unit suffixes and digit grouping (`12.345 678 901 s`, `1,234,567 ns`). Direction values
`Upstream`/`Up`/`Downstream`/`Down`/`Dn` and blank cells are all handled. An LCRC value such as `0x6446BADA` is not
mistaken for "bad".

## 5. Performance (measured on this 2-core box)

A SYNTHETIC export of 1.05 GB (2,047,498 rows, 112 columns, about 513 bytes per row because most rows carry a payload):

| | |
|---|---|
| Pass 1 (full analysis) | 20.2 s → **101k rows/s, 52 MB/s** (other runs 94–98k rows/s; this box is shared) |
| Pass 2 (evidence rows) | 2.7 s |
| End to end, including report | 23.0 s |
| Peak RSS | 175 MB for the whole CLI. The engine alone plateaus at about 133–150 MB once the recently-completed table is full, and stays flat to the end of the file. |

A 5 GB export of the same shape takes about 2 minutes. Exports with fewer payload bytes per row run faster.

## 6. Synthetic generator and tests

`python -m pex_checker.synth --out DIR --rows 150000 [--clean] [--size-gb 1.0]` writes a **SYNTHETIC** export in the
112-column layout. It covers link training (Gen1 x1 → Gen4 x4 with equalization), enumeration with real capability
values, controller enable, admin setup (Identify Controller/Namespace data, Set Features, Create I/O CQ/SQ ×8, MSI-X
table) and mixed I/O: reads up to 128 KiB with PRP lists, writes, batched submissions, flushes and deallocates. It
includes Acks, credit updates and SKPs. Fault mode injects 54 faults, at least one per rule family. Ground truth
(expected card and packet) is written to `*_truth.json`. Clean mode injects nothing.

```
python -m pytest -q pex_checker/tests      # 71 tests, about 15 s
```

The tests check that:
- every injected fault is found with the right card at the exact packet, with its explanation, evidence rows and
  context;
- the clean trace has no errors or warnings, and every command has a full lifecycle;
- the learned configuration is right;
- the output files are present and self-contained;
- coverage adds up to 326 cards;
- a variant export (renamed headers, all fields quoted, `Up`/`Down`, integer-ns timestamps, hex without `0x`, hex
  tags, a dropped column) gives the same findings, and the rule whose column was dropped is disabled;
- a capture that starts after initialisation gives no false errors and decodes with `--bar0/--sq/--cq`;
- the parsers and the status table behave as specified.

No large files are kept in the repository: the tests generate their traces in a temporary directory.

## 7. Limits: what a single-link trace cannot prove

- Only what crossed the **probed link** while the analyzer was recording and decoding is visible. Device internals
  (media, FTL, caches, whether a deallocate unmapped LBAs, whether a flush reached NAND), host memory that never
  crosses the link, host-side timers, logs and decisions, the Root Port's own AER registers, and other links are not.
  For these, the findings say what extra evidence is needed (read-back, logs, SMART, power-loss test, a second
  probe).
- **Timeouts are absence plus time.** A capture gap, buffer wrap or filter can fake a missing completion. Check for
  X-30 findings (PSN jumps without Nak, Acks for unseen PSNs, silence in L0) before trusting one.
- **Decode depends on initialisation being in the capture.** Without it, BAR0 is inferred, queue rules stay silent
  unless `--sq/--cq` are given, and nothing is reported about queues the tool does not know.
- **Ordering** findings show the order in which the device sent TLPs on this link. What the CPU observed also
  depends on the root complex.
- Some rules use **heuristics**, which the finding text says: V-23 (admin-looking opcode), D-08 (suspiciously fast
  flush), X-28 (analyzer re-lock), P-13/P-14 (workload-level).
- **Not modelled**: Flit mode (PCIe 6.x; there are no Flit columns in this export), legacy MSI, IDE, ATS beyond UR
  completions, ZNS zone state, PI recomputation. The 49 cards that are not checkable are listed with reasons in
  `coverage.md`.
- Spec wording quoted in findings comes from the atlas cards. The tool adds no specification section numbers.
