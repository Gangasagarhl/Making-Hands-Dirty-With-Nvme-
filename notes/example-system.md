# The course's reference lab system (synthetic)

Every synthetic trace, register dump and worked example in the course uses this one imaginary machine
unless a page says otherwise, so learners can follow the same numbers from chapter to chapter.
Label all of it "Synthetic example for teaching". None of it is an official specification example.

## Host and link
- x86-64 server, Linux. Root port at 0000:00:01.1 (Requester ID 0x0009). Root complex requests to the device
  (MMIO writes/reads by the CPU) carry the Root Port/Root Complex Requester ID; say "RC" in tables.
- NVMe SSD "Lab SSD" at Bus:Device.Function 0000:01:00.0 → Requester ID 0x0100.
- Link: PCIe Gen4 (16.0 GT/s), x4, 128b/130b encoding. (A Gen5 x4 variant is allowed for performance chapters; say so.)
- Max Payload Size (MPS) = 256 bytes; Max Read Request Size (MRRS) = 512 bytes; Read Completion Boundary (RCB) = 64 bytes.
- 10-bit tags disabled unless the page is teaching 10-bit tags; tags 0x00–0xFF.
- IOMMU enabled; addresses the device emits are I/O virtual addresses (IOVA). Say so when it matters.

## Device
- BAR0/BAR1: 64-bit, non-prefetchable memory BAR, size 16 KiB, assigned address 0xFE60_0000.
- NVMe controller registers at BAR0 + 0x0000. CAP.DSTRD = 0 → doorbell stride 4 bytes:
  SQ0TDBL = BAR0+0x1000, CQ0HDBL = BAR0+0x1004, SQ1TDBL = BAR0+0x1008, CQ1HDBL = BAR0+0x100C,
  SQ2TDBL = BAR0+0x1010 … (formula: 0x1000 + (2y × (4 << CAP.DSTRD)) for SQ y tail, +(4 << CAP.DSTRD) for CQ y head).
- MSI-X: 33 vectors (vector 0 admin, 1–32 I/O); MSI-X table at BAR0 + 0x2000, PBA at BAR0 + 0x3000 (implementation choice, found via the MSI-X capability's Table/PBA BIR and offset).
- Memory Page Size (CC.MPS) = 0 → 4 KiB host pages.
- Namespace 1 uses 512-byte LBAs (LBADS = 9) and NSZE = 0x0000_0000_7470_6DB0 (1,953,525,168 LBAs ≈ 1 TB). Namespace 2 (when needed) uses 4 KiB LBAs.
- Queues: Admin SQ/CQ 32 entries each. I/O queue pair 1 (SQ1/CQ1) 1024 entries; SQ entry 64 bytes, CQ entry 16 bytes.

## Host memory (IOVA as seen by the device)
- Admin SQ base 0x0000_0001_0000_0000, Admin CQ base 0x0000_0001_0000_1000
- I/O SQ1 base 0x0000_0001_2340_0000 (64 KiB), I/O CQ1 base 0x0000_0001_2341_0000 (16 KiB)
- Data buffers start at 0x0000_0002_0000_0000; PRP list pages at 0x0000_0001_5000_0000.
- MSI-X message address 0xFEE0_0000 range (x86 LAPIC window), data values per vector.

## Typical latencies for synthetic timelines (illustrative only, not measured)
- Doorbell MWr leaves CPU → device: hundreds of ns; device SQE fetch round trip: ~0.5–1 µs;
  NAND read (TLC): tens of µs; 4 KiB data transfer at Gen4 x4: ~1–2 µs; MSI-X write to interrupt handler: ~1–3 µs.
Always state that real values are implementation- and platform-dependent.
