"""All thresholds of the checker.  Every value here is a configurable DEFAULT chosen for validation work, not a
number quoted from a specification, unless the comment says where it comes from.  Each one is a CLI option
(python -m pex_checker --help) and is written to summary.json -> config."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Config:
    # ---- NVMe command timeouts (defaults follow the Linux driver defaults: nvme_io_timeout 30 s, admin 60 s)
    io_timeout_s: float = 30.0
    admin_timeout_s: float = 60.0
    pending_min_ms: float = 100.0          # end of trace: a command older than this without CQE -> T-01
    # ---- PCIe completion timeout: 50 ms = upper end of the default range named in the atlas (T-33 card)
    cpl_timeout_ms: float = 50.0
    # ---- data link layer
    ack_latency_us: float = 10.0           # X-20: TLP -> covering Ack.  Spec limits depend on speed/width/MPS
    replay_rollover: int = 4               # X-19: replays of one PSN (REPLAY_NUM is a 2-bit counter)
    nak_storm_n: int = 8                   # X-18: Naks within nak_storm_window_ms
    nak_storm_window_ms: float = 1.0
    seq_gap_resolve_us: float = 50.0       # PSN gap: a Nak within this window -> X-17, else X-30
    # ---- flow control
    fc_stall_us: float = 30.0              # T-38/X-22: credits exhausted and no UpdateFC for longer than this
    fc_resume_us: float = 5.0              # ... and the transmitter's next TLP follows the UpdateFC within this
    # ---- link
    recovery_storm_n: int = 5              # X-11: Recovery entries within recovery_storm_window_s
    recovery_storm_window_s: float = 1.0
    ltssm_episode_ms: float = 10.0         # T-39: one training / Recovery episode longer than this
    training_timeout_ms: float = 100.0     # T-40: first training ordered set -> L0
    l1_exit_us: float = 64.0               # T-45 fallback when neither LinkCap nor DevCap was captured
    wake_min_us: float = 10.0              # P-07: link wake added before a doorbell
    detect_loop_n: int = 3                 # X-12: Detect/Polling entries without reaching L0
    err_msg_storm_n: int = 20              # X-26 storm: error messages per second
    gap_ms: float = 100.0                  # X-30: no packet at all for this long while the link is in L0
    relock_window_us: float = 2.0          # X-28: physical errors this soon after an idle exit / speed change
    # ---- NVMe ordering / interrupts / host
    msix_window_us: float = 1000.0         # V-15: CQE without MSI-X for longer than this (plus coalescing time)
    msix_before_cqe_us: float = 10.0       # V-14: MSI-X followed by its CQE within this window
    host_stall_ms: float = 100.0           # T-32: MSI-X -> CQ head doorbell longer than this
    cq_stall_ms: float = 1000.0            # V-07(a): CQ non-empty, head doorbell unchanged for this long
    abort_check_s: float = 1.0             # T-09: target still outstanding this long after a successful Abort
    # ---- latency outliers (T-47)
    outlier_k: float = 3.0                 # outlier = latency > max(k * p99-so-far, floor)
    outlier_floor_us: float = 1000.0
    outlier_min_samples: int = 50
    flush_min_us: float = 5.0              # D-08 heuristic: Flush faster than this after >= 1 MiB of writes
    # ---- retries
    retry_window_s: float = 60.0
    max_retries: int = 5                   # Linux nvme_max_retries default
    ns_not_ready_s: float = 1.0            # T-18
    # ---- PCIe config timing (atlas cards T-41/T-42/T-43 name these intervals)
    crs_timeout_s: float = 1.0
    crs_loop_n: int = 50
    d3_recovery_ms: float = 10.0
    flr_wait_ms: float = 100.0
    shutdown_timeout_s: float = 10.0       # C-04 when RTD3E is unknown
    rdy_timeout_s: float = 0.0             # 0 = use CAP.TO (500 ms units) when captured
    # ---- overrides for values normally learned from the trace
    bar0: int | None = None
    dstrd: int | None = None
    mps: int | None = None
    mrrs: int | None = None
    rcb: int = 64
    mdts_bytes: int | None = None
    lba_size: int | None = None
    page_size: int | None = None
    time_unit: str = "auto"                # auto | s | ms | us | ns | ps
    link: str | None = None                # analyse only this Link column value
    data_format: str = "auto"              # auto | bytes | dwords (Data column byte order)
    # ---- features
    verify_data: bool = False              # D-13/D-14/D-15/D-16/D-18/D-20: payload comparison (slower)
    verify_lbas: int = 262144
    capture_limit: int = 16384             # max bytes captured per admin command (Identify, logs)
    # ---- output
    max_per_card: int = 200
    max_per_card_info: int = 20
    context_rows: int = 3
    context_max_rows: int = 200000
    recent_rows: int = 16                  # sliding window of raw rows (evidence when --no-context)
    recent_completed: int = 4096           # completed commands kept for V-16 / D-29
    progress_every_s: float = 2.0

    def as_dict(self):
        return asdict(self)
