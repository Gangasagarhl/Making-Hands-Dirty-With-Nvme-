# pex_checker — streaming PEX CSV error checker (work in progress)

**Status: not runnable yet.** The core modules are written, but the command-line entry point, the synthetic
trace generator, the tests and the full documentation are still to come.

Goal: stream every row of a Teledyne LeCroy `.pex` → CSV export (2–5 GB, ~112–120 columns) in constant memory.
Rebuild every NVMe command from its packets (doorbell → SQE fetch → data → CQE → MSI-X), then report where
(packet, timestamp) and why each problem happened. Each finding is classified as timing, timeout, reordering,
protocol, status code, link layer or performance, and mapped to its PEX Trace Error Atlas card. It also says who
is likely at fault and gives an nvme-cli command to reproduce it.

## Modules so far

| File | Purpose |
|---|---|
| `schema.py` | Header auto-mapping for real exports (aliases, duplicate columns, hex/time parsing) |
| `reader.py` | Streaming CSV reader that pulls only the needed columns |
| `config.py` | Configurable thresholds (I/O 30 s, admin 60 s, PCIe completion timeout 50 ms, outlier factor) |
| `nvme_defs.py` | Opcodes and the SCT/SC status table mapped to atlas S-cards |
| `link.py` | PCIe link/DLL tracking: PSN, Ack/Nak, replays, LCRC/ECRC, flow-control credits, training, L1, error messages |
| `nvme.py` | NVMe decoder: queues, doorbells, SQE/CQE, data attribution, MSI-X, controller registers, command lifecycle |
| `engine.py` | Drives one streaming pass and dispatches rows to the trackers |
| `findings.py` | Finding records (card, category, fault side, evidence, stage breakdown, repro) |
| `cards.py` | Loads `data/atlas_cards.json` and `data/fault_side.txt` |
| `stats.py` | Streaming latency percentiles for outlier detection |

`data/atlas_cards.json` holds the 326 atlas error cards. `data/fault_side.txt` gives each card's likely fault
side (H host, D device, L link/platform, B either, N expected behaviour, A analyzer artifact) and its triggering
command.
