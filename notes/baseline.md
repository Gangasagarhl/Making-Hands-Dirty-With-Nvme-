# Specification baseline (verified 2026-10-04)

Checked on nvmexpress.org on 2026-10-04:

- nvmexpress.org/specifications: "The latest versions in the NVMe set of specifications were released on August 4, 2026" (the NVMe 2.4 set).
- nvmexpress.org/specification/nvm-express-base-specification: "NVM Express Base Specification, Revision 2.4 Ratified 2026.07.31".
- Current revisions listed there: NVM Command Set 1.3; Zoned Namespaces (ZNS) 1.5; Key Value (KV) 1.4;
  Subsystem Local Memory (SLM) 1.3; Computational Programs 1.3; NVMe over PCIe Transport 1.4;
  NVMe over RDMA Transport 1.3; NVMe over TCP Transport 1.3; NVMe Management Interface 2.2; NVMe Boot 1.4.
- The Revision Changes page links "NVM Express Revision Changes 2026.07.30" (PDF) as the authoritative change list.

2026 release features as reported by press coverage of the release (EE Times, 2026-08; xenospectrum, 2026-08).
These are secondary sources: teach the purpose, and send readers to the spec and the Revision Changes document for field-level detail.

- Post-quantum cryptography: asymmetric key-exchange algorithms updated to prepare for PQC; coverage names
  NIST FIPS 203 (ML-KEM), FIPS 204 (ML-DSA) and FIPS 205 (SLH-DSA), finalized by NIST in August 2024.
- PCIe Exported NVM Subsystem Migration: lets an SSD expose Exported NVM Subsystems (virtual SSDs within a
  physical SSD) to VMs and migrate them live. A configuration template ("blueprint") defines the features,
  identifiers and behaviours an Exported NVM Subsystem exposes and is identified by a 128-bit UUID. Reported flow:
  create Exported NVM Subsystem on destination; recreate exported controller and namespace with matching
  identifiers; apply the same configuration template; capture source runtime state; transfer and apply it;
  migrate the VM; clean up the source.
- TDISP information package: controllers can report detailed state information (used when validating controller state, e.g. during migration).
- Lockdown improvements: lockdown settings can persist across reboots; separate lockdown controls for virtualized environments.
- Rate limiting: controls for allocating PCIe bandwidth and IOPS among multiple controllers (controller-enforced caps).
- Voltage monitoring: detect discrepancies between the voltage the platform supplies and what the device receives;
  over/undervoltage thresholds with alerts and historical logs.
- Idle I/O exit latency limits: hosts can specify latency thresholds that must be maintained.
- Restore manufacturing default settings ("manufacturing reset"): return an NVM subsystem to its as-shipped state.
- All eleven specification documents were revised together.

Do not state opcodes, log page identifiers, feature identifiers or field offsets for these 2.4 features:
we have not verified them. Say they are defined in the Base 2.4 / relevant spec and must be checked there.
