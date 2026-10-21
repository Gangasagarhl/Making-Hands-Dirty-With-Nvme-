"""Physical / data-link layer state: PSN continuity and replays, Ack/Nak latency, LCRC/DLLP CRC, flow-control
credit accounting, LTSSM state inferred from ordered sets and Link Event rows, L1 / Recovery / training episodes,
speed / width changes, PM DLLPs.  Keeps a short list of link intervals (Recovery, L1, replay bursts, credit stalls)
so a slow NVMe command can be correlated with what the link was doing at the time."""
from __future__ import annotations

import re
from collections import deque

from . import schema as S

RATE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*GT/?s", re.I)
WIDTH_RE = re.compile(r"x(\d+)", re.I)

# Link Event text -> kind (first match wins; lower-case substring tests)
EV_RULES = [
    ("link down", "link_down"), ("surprise down", "link_down"), ("dl_down", "link_down"), ("dl down", "link_down"),
    ("hot reset", "hot_reset"), ("disabled", "disabled"), ("loopback", "loopback"), ("compliance", "compliance"),
    ("equaliz", "eq"), ("recovery exit", "recovery_exit"), ("recovery entry", "recovery"), ("recovery.", "recovery"),
    ("recovery", "recovery"), ("speed change", "speed"), ("width change", "width"),
    ("l1 entry", "l1_entry"), ("enter l1", "l1_entry"), ("l1 exit", "l1_exit"), ("exit l1", "l1_exit"),
    ("l0s entry", "l0s_entry"), ("l0s exit", "l0s_exit"), ("l2", "l23"),
    ("electrical idle entry", "eidle_entry"), ("electrical idle exit", "eidle_exit"),
    ("training start", "training"), ("detect", "detect"), ("polling", "polling"), ("configuration", "configuration"),
    ("link up", "l0"), ("dl_up", "l0"), ("dl up", "l0"), ("(l0)", "l0"), ("l0 ", "l0"),
    ("invalid symbol", "phys_symbol"), ("code violation", "phys_symbol"), ("10b", "phys_symbol"),
    ("disparity", "phys_disparity"), ("sync header", "phys_sync"), ("framing", "phys_framing"),
    ("skew", "phys_skew"), ("lane reversal", "phys_lanes"), ("polarity", "phys_lanes"),
    ("receiver error", "phys_rx"), ("elastic buffer", "phys_rx"), ("decode error", "phys_rx"), ("symbol error", "phys_symbol"),
    ("skp", "phys_skp"), ("nullified", "nullified"),
]
PHYS_CARD = {"phys_symbol": "X-01", "phys_disparity": "X-02", "phys_sync": "X-03", "phys_framing": "X-05",
             "phys_skew": "X-07", "phys_lanes": "X-08", "phys_rx": "L-01", "phys_skp": "X-10"}


def classify_event(text: str) -> str:
    t = text.lower()
    for kw, kind in EV_RULES:
        if kw in t:
            return kind
    if t.strip() == "l0":
        return "l0"
    return "other"


class LinkMixin:
    def _init_link(self):
        cfg = self.cfg
        self.next_psn = {"U": -1, "D": -1}
        self.psn_tab = {"U": [None] * 4096, "D": [None] * 4096}
        self.unacked = {"U": deque(), "D": deque()}
        self.dll_block = {"U": None, "D": None}        # [bad_psn, ts, held rows]
        self.last_nak = {"U": None, "D": None}         # TLP direction -> (ts, seq, row, pkt, finding)
        self.bad_lcrc_recent = {"U": None, "D": None}  # TLP direction -> (ts, finding)
        self.replay_ep = {"U": None, "D": None}        # current replay burst [t0, t_last, n, finding, kind, ev0]
        self.nak_times = {"U": deque(), "D": deque()}
        self.seq_gap_pending = deque()                 # (deadline, d, info)
        self.dll_stats = {"tlps": {"U": 0, "D": 0}, "replays": {"U": 0, "D": 0}, "naks": {"U": 0, "D": 0},
                          "bad_lcrc": {"U": 0, "D": 0}, "bad_dllp": 0, "seq_gaps": 0, "nullified": 0}
        from .stats import LogHist
        self.ack_hist = {"U": LogHist(), "D": LogHist()}
        # flow control: tx direction -> type -> state
        self.fc = {d: {t: None for t in ("P", "NP", "CPL")} for d in ("U", "D")}
        self.fc_pending = []                            # [tx, typ, update_ts, stall_ev, dur_us, update_ev]
        # LTSSM
        self.ltssm = "unknown"
        self.l0_seen = False
        self.first_l0 = None
        self.training_t0 = None
        self.training_ev = None
        self.detect_count = 0
        self.speed = 0.0
        self.width = 0
        self.speed_src = ""
        self.max_rate_seen = 0.0
        self.max_width_seen = 0
        self.ts_last_rate = 0.0
        self.ts_last_lanes = 0
        self.recovery_t0 = None
        self.recovery_ev = None
        self.recovery_cause = None
        self.recovery_times = deque()
        self.recovery_count = 0
        self.ep_eq = False
        self.ep_speed_before = 0.0
        self.l1_entry = None            # (ts, row, pkt)
        self.l1_exit_start = None
        self.l1_episodes = 0
        self.eidle_since = None
        self.last_idle_exit = None      # ts of last idle exit / speed change (re-lock window)
        self.wake = None                # last completed wake (for the next doorbell)
        self.pm_req = None
        self.last_event = ("", 0, 0)    # text, row, pkt
        self.events_seen = 0
        self.intervals = deque(maxlen=4096)   # (t0, t1, kind, row, pkt, detail)
        self.phys_ep = {}               # card -> [ts, finding]
        self.relock_pending = deque()
        self.link_events_log = deque(maxlen=400)
        self.speed_changes = []
        self.ltr_tolerance_ns = None
        self.ltr_ev = None

    # ------------------------------------------------------------------------------------------------ snapshots
    def link_snapshot(self) -> dict:
        return {"ltssm": self.ltssm, "speed_gts": self.speed or None, "width": self.width or None,
                "last_event": self.last_event[0], "last_event_pkt": self.last_event[2],
                "recoveries_so_far": self.recovery_count, "replays_so_far": sum(self.dll_stats["replays"].values())}

    def add_interval(self, t0, t1, kind, row, pkt, detail=""):
        self.intervals.append((t0, t1, kind, row, pkt, detail))

    def intervals_overlapping(self, t0, t1):
        out = []
        for iv in reversed(self.intervals):
            if iv[1] < t0:
                break
            if iv[0] <= t1:
                out.append(iv)
        # still-open episodes
        if self.recovery_t0 is not None and self.recovery_t0 <= t1:
            out.append((self.recovery_t0, t1, "recovery (open)", self.recovery_ev["row"], self.recovery_ev["pkt"], ""))
        return out

    # ------------------------------------------------------------------------------------------------ DLL: TLP side
    def dll_tlp(self, d: str, psn: int, bad: bool) -> bool:
        """Returns True when the receiver would accept this TLP (new PSN, good LCRC, not behind a bad TLP)."""
        st = self.dll_stats
        if psn < 0:
            if bad:
                st["bad_lcrc"][d] += 1
                self.on_bad_lcrc(d, psn)
            return not bad
        ts = self.now
        tab = self.psn_tab[d]
        exp = self.next_psn[d]
        blk = self.dll_block[d]
        if exp < 0 or psn == exp:
            st["tlps"][d] += 1
            self.next_psn[d] = (psn + 1) & 0xFFF
            ua = self.unacked[d]
            ua.append((psn, ts, self.row))
            if len(ua) > 4096:
                ua.popleft()
            if bad:
                tab[psn] = [ts, 1, False, self.row]
                st["bad_lcrc"][d] += 1
                self.on_bad_lcrc(d, psn)
                if blk is None:
                    self.dll_block[d] = [psn, ts, []]
                return False
            if blk is not None:
                # receiver discards everything after a bad TLP until it is replayed
                tab[psn] = [ts, 1, False, self.row]
                if ts - blk[1] > 1e-4:
                    self._dll_unblock(d)
                    tab[psn][2] = True
                    return True
                if len(blk[2]) < 64:
                    blk[2].append((self.row, self.f, ts))
                return False
            tab[psn] = [ts, 1, True, self.row]
            return True
        behind = (exp - psn) & 0xFFF
        e = tab[psn]
        if 0 < behind < 2048 and e is not None and ts - e[0] < 0.5:
            e[1] += 1
            st["replays"][d] += 1
            self.on_replay(d, psn, e)
            if bad:
                st["bad_lcrc"][d] += 1
                self.on_bad_lcrc(d, psn)
                return False
            if blk is not None and blk[0] == psn:
                self.dll_block[d] = None
                e[2] = True
                held = blk[2]
                # held TLPs will be replayed too (their table entries are not accepted yet)
                return True
            if e[2]:
                return False
            e[2] = True
            return True
        # sequence gap or out-of-order PSN
        st["tlps"][d] += 1
        st["seq_gaps"] += 1
        ahead = (psn - exp) & 0xFFF
        self.seq_gap_pending.append((ts + self.cfg.seq_gap_resolve_us * 1e-6, d, exp, psn, ahead, self.ev_here("tlp_with_unexpected_psn")))
        tab[psn] = [ts, 1, not bad, self.row]
        self.next_psn[d] = (psn + 1) & 0xFFF
        self.unacked[d].append((psn, ts, self.row))
        return not bad

    def _dll_unblock(self, d):
        blk = self.dll_block[d]
        self.dll_block[d] = None
        if not blk:
            return
        # no replay seen within 100 us: the analyzer probably flagged a TLP the receiver accepted; process held TLPs
        saved = (self.row, self.f, self.now)
        for row, f, ts in blk[2]:
            self.row, self.f, self.now = row, f, ts
            self.tlp_accepted(f, d)
        self.row, self.f, self.now = saved

    def on_bad_lcrc(self, d, psn):
        f = self.emit("X-15", "LCRC", "warning", "link-layer",
                      f"TLP {self.tlp_label()} {self.dname(d)} PSN {psn} carries a bad LCRC ({self.f[self.ix['lcrc']].strip()}); "
                      f"the receiver must discard it and Nak.", related=["L-02", "V-35"],
                      metrics={"psn": psn}, side="L")
        self.bad_lcrc_recent[d] = (self.now, f)

    def on_replay(self, d, psn, e):
        ts = self.now
        ln = self.last_nak[d]
        nak_driven = ln is not None and ln[0] >= e[0] and ts - ln[0] < 2e-5
        ep = self.replay_ep[d]
        orig = self.ev(f"original transmission of PSN {psn}", e[3], self.pkt_of_row(e[3]), e[0])
        if ep is not None and ts - ep[1] < 2e-5:
            ep[1] = ts
            ep[2] += 1
            if ep[3] is not None and len(ep[3]["evidence"]) < 30:
                ep[3]["evidence"].append(self.ev_here(f"replay of PSN {psn}"))
                ep[3]["metrics"]["replayed_tlps"] = ep[2]
        else:
            if ep is not None:
                self.add_interval(ep[0], ep[1], "replay burst", ep[5]["row"], ep[5]["pkt"], f"{ep[2]} replayed TLPs")
            if nak_driven:
                fnd = ln[4]
                if fnd is not None and len(fnd["evidence"]) < 30:
                    fnd["evidence"].append(self.ev_here(f"replay of PSN {psn} after Nak"))
                self.replay_ep[d] = [ts, ts, 1, fnd, "nak", self.ev_here("replay")]
            else:
                age_us = (ts - e[0]) * 1e6
                fnd = self.emit("T-35", "REPLAY-TIMER", "warning", "link-layer",
                                f"{self.dname(d)} transmitter re-sent PSN {psn} {age_us:.2f} us after the original with no Nak since "
                                f"the original transmission: the replay timer expired because no Ack arrived in time.",
                                evidence=[orig], related=["L-05", "V-35", "T-37"],
                                metrics={"psn": psn, "us_since_original": round(age_us, 3)}, side="L")
                self.replay_ep[d] = [ts, ts, 1, fnd, "timer", self.ev_here("replay")]
        if e[1] >= self.cfg.replay_rollover and not (len(e) > 4 and e[4]):
            e.append(True)
            self.emit("X-19", "REPLAY-NUM", "error", "link-layer",
                      f"PSN {psn} {self.dname(d)} was transmitted {e[1]} times without being acknowledged: REPLAY_NUM rolls "
                      f"over and the transmitter must retrain the link (Recovery expected next).",
                      evidence=[orig], related=["L-04", "T-36", "V-35"], metrics={"psn": psn, "transmissions": e[1]}, side="L")

    # ------------------------------------------------------------------------------------------------ DLLPs
    def on_dllp(self, f, d, text):
        k = self.dllp_cache.get(text)
        if k is None:
            k = S.classify_dllp(text)
            self.dllp_cache[text] = k
        kind = k[0]
        self.saw_dl_traffic(d)
        crc = f[self.ix["crc16"]]
        if crc and ("(" in crc or " " in crc) and S.is_bad(crc):
            self.dll_stats["bad_dllp"] += 1
            self.emit("X-16", "DLLP-CRC", "warning", "link-layer",
                      f"{text} DLLP {self.dname(d)} with a bad CRC16 ({crc.strip()}): the receiver discards it silently; a lost Ack "
                      f"or UpdateFC shows up later as a replay or a credit stall.", related=["L-03"], side="L")
            return
        if kind == S.D_ACK or kind == S.D_NAK:
            seq = S.autoint(f[self.ix["acknak"]])
            if seq < 0:
                return
            td = "U" if d == "D" else "D"
            self.on_acknak(td, seq, kind == S.D_NAK, d)
            return
        if kind == S.D_FC:
            self.on_fc(f, d, k[1], k[2], text)
            return
        if kind == S.D_PM:
            self.on_pm_dllp(d, text)

    def on_ack_fast(self, f, d):
        if self.ltssm != "L0":
            self.enter_l0()
        crc = f[self.i_crc16]
        if crc and ("(" in crc or " " in crc) and S.is_bad(crc):
            self.on_dllp(f, d, "Ack")
            return
        s = f[self.i_acknak]
        try:
            seq = int(s)
        except ValueError:
            seq = S.autoint(s)
            if seq < 0:
                return
        td = "U" if d == "D" else "D"
        ua = self.unacked[td]
        # common case: the Ack covers exactly the one outstanding TLP, which was transmitted once
        if len(ua) == 1 and ua[0][0] == seq:
            e = self.psn_tab[td][seq]
            if e is not None and e[1] == 1:
                lat_us = (self.now - ua[0][1]) * 1e6
                if lat_us <= self.cfg.ack_latency_us:
                    ua.popleft()
                    self.ack_hist[td].add(lat_us)
                    return
        self.on_acknak(td, seq, False, d)

    def on_acknak(self, td, seq, is_nak, sender):
        ts = self.now
        ua = self.unacked[td]
        last_sent = (self.next_psn[td] - 1) & 0xFFF if self.next_psn[td] >= 0 else -1
        if last_sent >= 0:
            ahead = (seq - last_sent) & 0xFFF
            if 0 < ahead < 2048:
                self.emit("X-30", "ACK-UNSEEN", "info", "link-layer",
                          f"{'Nak' if is_nak else 'Ack'} {self.dname(sender)} covers PSN {seq}, but the last {self.dname(td)} TLP in the trace "
                          f"had PSN {last_sent}: {ahead} TLP(s) were not captured (capture gap or filter), or the receiver acknowledged "
                          f"a sequence number that was never sent (X-17).", related=["X-17", "X-28"], metrics={"ack_seq": seq, "last_psn": last_sent})
        oldest = None
        replayed = False
        tab = self.psn_tab[td]
        while ua and ((seq - ua[0][0]) & 0xFFF) < 2048:
            ent = ua.popleft()
            if oldest is None:
                oldest = ent
            e = tab[ent[0]]
            if e is not None and e[1] > 1:
                replayed = True
        if is_nak:
            self.dll_stats["naks"][td] += 1
            nt = self.nak_times[td]
            nt.append(ts)
            win = self.cfg.nak_storm_window_ms * 1e-3
            while nt and ts - nt[0] > win:
                nt.popleft()
            br = self.bad_lcrc_recent[td]
            fnd = None
            if br is not None and ts - br[0] < 5e-5 and br[1] is not None:
                fnd = br[1]
                fnd["evidence"].append(self.ev_here(f"Nak (last good PSN {seq})"))
            elif br is None or ts - br[0] >= 5e-5:
                fnd = self.emit("L-02", "NAK", "warning", "link-layer",
                                f"Nak {self.dname(sender)} with AckNak_Seq_Num {seq}: the receiver rejected a {self.dname(td)} TLP "
                                f"(bad LCRC or sequence error) although the analyzer did not flag an LCRC error; PSN {(seq + 1) & 0xFFF}+ must be replayed.",
                                related=["X-15", "X-17", "V-35"], metrics={"ack_seq": seq}, side="L")
            if len(nt) >= self.cfg.nak_storm_n and not getattr(self, "_nak_storm_flag", {}).get(td):
                self.__dict__.setdefault("_nak_storm_flag", {})[td] = True
                self.emit("X-18", "NAK-STORM", "warning", "link-layer",
                          f"{len(nt)} Naks for {self.dname(td)} TLPs within {self.cfg.nak_storm_window_ms} ms (threshold "
                          f"{self.cfg.nak_storm_n}, configurable): the link is retrying constantly without failing outright.",
                          related=["V-35", "L-02"], metrics={"naks_in_window": len(nt)}, side="L")
            elif len(nt) < self.cfg.nak_storm_n // 2:
                self.__dict__.setdefault("_nak_storm_flag", {})[td] = False
            self.last_nak[td] = (ts, seq, self.row, self.pkt(), fnd)
            # a pending sequence gap followed by a Nak is a real sequence error
            for i, g in enumerate(self.seq_gap_pending):
                if g[1] == td:
                    del self.seq_gap_pending[i]
                    self.emit("X-17", "SEQ-GAP", "warning", "link-layer",
                              f"{self.dname(td)} TLP PSN {g[3]} arrived while PSN {g[2]} was expected, and the receiver answered with a "
                              f"Nak: a real sequence error (lost or out-of-order TLP).", at=g[5],
                              evidence=[self.ev_here("Nak")], related=["L-10", "V-35"], side="L")
                    break
            return
        if oldest is not None:
            lat_us = (ts - oldest[1]) * 1e6
            self.ack_hist[td].add(lat_us)
            if lat_us > self.cfg.ack_latency_us:
                side = "H" if sender == "D" else "D"
                ev0 = self.ev("oldest TLP covered by this Ack", oldest[2], self.pkt_of_row(oldest[2]), oldest[1])
                if replayed:
                    self.emit("T-37", "ACK-LATE-REPLAY", "warning", "link-layer",
                              f"Ack {self.dname(sender)} for PSN {seq} arrived {lat_us:.2f} us after the oldest TLP it covers, after the "
                              f"transmitter had already replayed: the slow Ack caused unnecessary replays.", evidence=[ev0],
                              related=["X-20", "T-35"], metrics={"ack_latency_us": round(lat_us, 3)}, side=side)
                else:
                    self.emit("X-20", "ACK-LATENCY", "warning", "link-layer",
                              f"Ack {self.dname(sender)} for PSN {seq} came {lat_us:.2f} us after the oldest TLP it acknowledges "
                              f"(threshold {self.cfg.ack_latency_us} us, configurable; the spec limit depends on speed, width and MPS).",
                              evidence=[ev0], related=["T-37"], metrics={"ack_latency_us": round(lat_us, 3)}, side=side)

    # ------------------------------------------------------------------------------------------------ flow control
    def on_fc(self, f, d, typ, is_init, text):
        if typ not in ("P", "NP", "CPL"):
            return
        tx = "U" if d == "D" else "D"
        hdr = S.autoint(f[self.ix["hdrfc"]])
        dat = S.autoint(f[self.ix["datafc"]])
        hs = S.autoint(f[self.ix["hdrscale"]])
        ds = S.autoint(f[self.ix["datascale"]])
        if hs > 0:
            hdr <<= {1: 0, 2: 2, 3: 4}.get(hs, 0)
        if ds > 0:
            dat <<= {1: 0, 2: 2, 3: 4}.get(ds, 0)
        hmod = 0x100 if hs <= 1 else (0x400 if hs == 2 else 0x1000)
        dmod = 0x1000 if ds <= 1 else (0x4000 if ds == 2 else 0x10000)
        st = self.fc[tx][typ]
        ts = self.now
        if is_init:
            if st is None or (st["synced"] and st["hc"] == 0 and st["dc"] == 0 and st["init"]) or not st["synced"]:
                self.fc[tx][typ] = {"hl": hdr, "dl": dat, "hc": 0, "dc": 0, "hinf": hdr == 0, "dinf": dat == 0,
                                    "synced": True, "init": True, "stall": None, "last_upd": ts, "hmod": hmod, "dmod": dmod,
                                    "upd_n": 0}
            return
        if st is None:
            # capture started after InitFC: limits known, consumption unknown -> no credit checks for this type
            self.fc[tx][typ] = {"hl": hdr, "dl": dat, "hc": 0, "dc": 0, "hinf": False, "dinf": False, "synced": False,
                                "init": False, "stall": None, "last_upd": ts, "hmod": hmod, "dmod": dmod, "upd_n": 1}
            return
        st["upd_n"] += 1
        if not st["synced"]:
            st["hl"], st["dl"], st["last_upd"] = hdr, dat, ts
            return
        old_dl, old_hl = st["dl"], st["hl"]
        if not st["hinf"] and hdr >= 0:
            st["hl"] = hdr
        if not st["dinf"] and dat >= 0:
            st["dl"] = dat
        hm, dm = st["hmod"], st["dmod"]
        if (not st["dinf"] and ((st["dl"] - st["dc"]) % dm) > dm // 2) or (not st["hinf"] and ((st["hl"] - st["hc"]) % hm) > hm // 2):
            self.emit("L-13", "FC-OVERFLOW", "warning", "link-layer",
                      f"{text} {self.dname(d)} advertises a credit limit (hdr {st['hl']}, data {st['dl']}) that leaves more outstanding "
                      f"credits than the field allows (consumed hdr {st['hc']}, data {st['dc']}): flow-control protocol error.",
                      related=["X-21"], side="D" if d == "U" else "H")
        stall = st["stall"]
        if stall is not None:
            dur_us = (ts - stall["ts"]) * 1e6
            avail_d = (st["dl"] - st["dc"]) % dm if not st["dinf"] else 1 << 30
            avail_h = (st["hl"] - st["hc"]) % hm if not st["hinf"] else 1 << 30
            released = avail_d >= stall["need_d"] and avail_h >= 1
            if released:
                st["stall"] = None
                if dur_us > self.cfg.fc_stall_us:
                    self.fc_pending.append([tx, typ, ts, stall, dur_us, self.ev_here(f"{text} releasing credits")])
                    self.add_interval(stall["ts"], ts, "credit stall", stall["row"], stall["pkt"], f"{typ} credits, {dur_us:.1f} us")
            elif dur_us > self.cfg.fc_stall_us and not stall.get("x22") and st["dl"] == old_dl and st["hl"] == old_hl:
                stall["x22"] = True
                self.emit("X-22", "FC-STUCK", "warning", "link-layer",
                          f"{text} {self.dname(d)} repeats the same credit limit while the {self.dname(tx)} transmitter has been out of "
                          f"{typ} credits for {dur_us:.1f} us: UpdateFC is stuck.",
                          at=stall["ev"], evidence=[self.ev_here(text)], related=["T-38"], metrics={"stall_us": round(dur_us, 2)},
                          side="H" if d == "D" else "D")
        st["last_upd"] = ts

    def fc_consume(self, d, typ, nbytes):
        st = self.fc[d][typ]
        if st is None or not st["synced"]:
            return
        if self.fc_pending:
            self._fc_resume_check(d, typ)
        hm, dm = st["hmod"], st["dmod"]
        need = (nbytes + 15) >> 4
        if not st["hinf"]:
            st["hc"] = (st["hc"] + 1) % hm
        if not st["dinf"] and need:
            st["dc"] = (st["dc"] + need) % dm
        over_h = (not st["hinf"]) and ((st["hl"] - st["hc"]) % hm) > hm // 2
        over_d = (not st["dinf"]) and ((st["dl"] - st["dc"]) % dm) > dm // 2
        if over_h or over_d:
            self.emit("X-21", "FC-VIOLATION", "error", "link-layer",
                      f"{self.dname(d)} {typ} TLP sent without enough flow-control credits (limit hdr {st['hl']}/data {st['dl']}, "
                      f"consumed hdr {st['hc']}/data {st['dc']} after this TLP): the receiver may overflow.",
                      related=["L-17", "L-13"], side="D" if d == "U" else "H")
            st["synced"] = False
            return
        if st["stall"] is None:
            avail_d = (st["dl"] - st["dc"]) % dm if not st["dinf"] else 1 << 30
            avail_h = (st["hl"] - st["hc"]) % hm if not st["hinf"] else 1 << 30
            mps = self.mps or 256
            need_d = (mps + 15) >> 4 if typ != "NP" else 0
            if avail_h < 1 or avail_d < need_d:
                st["stall"] = {"ts": self.now, "row": self.row, "pkt": self.pkt(), "need_d": max(1, need_d) if typ != "NP" else 0,
                               "ev": self.ev_here(f"{typ} credits exhausted (hdr left {avail_h if avail_h < 1 << 29 else 'inf'}, "
                                                  f"data left {avail_d if avail_d < 1 << 29 else 'inf'})")}

    def _fc_resume_check(self, d, typ):
        keep = []
        for p in self.fc_pending:
            tx, ptyp, upd_ts, stall, dur_us, upd_ev = p
            if tx == d and ptyp == typ:
                gap_us = (self.now - upd_ts) * 1e6
                if gap_us <= self.cfg.fc_resume_us:
                    self.emit("T-38", "FC-STARVATION", "warning", "link-layer",
                              f"{self.dname(d)} transmitter ran out of {typ} credits and waited {dur_us:.1f} us for the next "
                              f"UpdateFC (threshold {self.cfg.fc_stall_us} us, configurable); its next {typ} TLP followed the "
                              f"UpdateFC after {gap_us:.2f} us, so traffic was blocked by credits.",
                              at=stall["ev"], evidence=[upd_ev, self.ev_here(f"first {typ} TLP after the UpdateFC")],
                              related=["X-22"], metrics={"stall_us": round(dur_us, 2), "resume_gap_us": round(gap_us, 3)},
                              side="H" if d == "U" else "D")
                continue
            if self.now - upd_ts < 1e-3:
                keep.append(p)
        self.fc_pending = keep

    # ------------------------------------------------------------------------------------------------ LTSSM
    def saw_dl_traffic(self, d):
        """A DLLP or TLP was seen: the link is in L0 (closes training / recovery / L1 exit episodes)."""
        if self.ltssm == "L0":
            return
        self.enter_l0()

    def enter_l0(self, ev_text=""):
        ts = self.now
        prev = self.ltssm
        self.ltssm = "L0"
        here = self.ev_here(ev_text or "link back in L0")
        if not self.l0_seen:
            self.l0_seen = True
            self.first_l0 = (ts, self.row, self.pkt())
            if self.training_t0 is not None:
                dur_ms = (ts - self.training_t0) * 1e3
                self.add_interval(self.training_t0, ts, "link training", self.training_ev["row"], self.training_ev["pkt"], f"{dur_ms:.3f} ms")
                if dur_ms > self.cfg.training_timeout_ms:
                    self.emit("T-40", "LINK-UP-SLOW", "warning", "timeout",
                              f"Link training took {dur_ms:.2f} ms from its first training event to L0 (threshold "
                              f"{self.cfg.training_timeout_ms} ms, configurable).", at=self.training_ev, evidence=[here],
                              related=["T-39", "L-39"], metrics={"training_ms": round(dur_ms, 3)})
            if self.speed == 0 and self.ts_last_rate:
                self._set_speed(self.ts_last_rate, "TS ordered sets")
        if self.recovery_t0 is not None:
            dur_ms = (ts - self.recovery_t0) * 1e3
            rev = self.recovery_ev
            self.add_interval(self.recovery_t0, ts, "recovery", rev["row"], rev["pkt"], f"{dur_ms * 1e3:.1f} us")
            if self.ep_eq and self.speed and self.ep_speed_before and self.speed < self.ep_speed_before:
                self.emit("X-13", "EQ-FALLBACK", "error", "link-layer",
                          f"Equalization ran during this Recovery and the link came back at {self.speed:g} GT/s instead of "
                          f"{self.ep_speed_before:g} GT/s: equalization failed and the link fell back.", at=rev, evidence=[here],
                          related=["T-39", "L-25"])
            if dur_ms > self.cfg.ltssm_episode_ms:
                self.emit("T-39", "LTSSM-SLOW", "warning", "link-layer",
                          f"Recovery episode lasted {dur_ms:.3f} ms (threshold {self.cfg.ltssm_episode_ms} ms, configurable): "
                          f"the LTSSM spent a long time out of L0.", at=rev, evidence=[here], related=["X-11"],
                          metrics={"recovery_ms": round(dur_ms, 4)})
            self.recovery_t0 = None
            self.ep_eq = False
        if self.l1_exit_start is not None:
            t0, row, pkt = self.l1_exit_start
            exit_us = (ts - t0) * 1e6
            self.add_interval(self.l1_entry[0] if self.l1_entry else t0, ts, "L1 + exit", row, pkt, f"exit {exit_us:.1f} us")
            self.wake = {"exit_start_ts": t0, "l0_ts": ts, "exit_us": exit_us, "row": row, "pkt": pkt, "l0_row": self.row,
                         "l0_pkt": self.pkt(), "residency_ms": round((t0 - self.l1_entry[0]) * 1e3, 3) if self.l1_entry else None}
            self.check_l1_exit(exit_us, self.ev("L1 exit start", row, pkt, t0), here)
            self.l1_exit_start = None
            self.l1_entry = None
        self.eidle_since = None
        if prev in ("idle", "L1", "recovery"):
            self.last_idle_exit = ts

    def check_l1_exit(self, exit_us, ev_start, ev_end):
        cap_us, cap_txt = self.link_cap_l1_exit_us()
        acc_us, acc_txt = self.dev_l1_acceptable_us()
        limit, basis = None, ""
        if acc_us is not None:
            limit, basis = acc_us, f"Device Capabilities L1 acceptable latency {acc_txt}"
        elif cap_us is not None:
            limit, basis = cap_us, f"Link Capabilities L1 exit latency {cap_txt}"
        else:
            limit, basis = self.cfg.l1_exit_us, f"configurable default {self.cfg.l1_exit_us} us (no capability register captured)"
        metrics = {"l1_exit_us": round(exit_us, 3), "limit_us": limit}
        if cap_us is not None and exit_us > cap_us:
            metrics["exceeds_advertised_link_cap"] = cap_txt
        if limit is not None and exit_us > limit:
            self.emit("T-45", "L1-EXIT", "warning", "timing",
                      f"L1 exit took {exit_us:.2f} us (L1 exit start to L0); limit {limit:g} us from {basis}."
                      + (f" It also exceeds what Link Capabilities advertises ({cap_txt})." if "exceeds_advertised_link_cap" in metrics else ""),
                      at=ev_start, evidence=[ev_end], related=["T-46", "P-07", "L-37"], metrics=metrics)
        if self.ltr_tolerance_ns is not None and exit_us * 1e3 > self.ltr_tolerance_ns:
            self.emit("T-46", "LTR-VS-EXIT", "warning", "timing",
                      f"The device's LTR message allowed {self.ltr_tolerance_ns / 1e3:.2f} us of latency, but the L1 exit took "
                      f"{exit_us:.2f} us. L1 substate timing itself (L1.2, CLKREQ#) is sideband and not in the trace.",
                      at=ev_start, evidence=[ev_end] + ([self.ltr_ev] if self.ltr_ev else []), related=["T-45", "P-07"],
                      metrics={"ltr_tolerance_us": round(self.ltr_tolerance_ns / 1e3, 3), "l1_exit_us": round(exit_us, 3)})

    def _set_speed(self, rate, src):
        if rate and rate != self.speed:
            self.speed = rate
            self.speed_src = src
        if rate > self.max_rate_seen:
            self.max_rate_seen = rate

    def _set_width(self, w):
        if w:
            self.width = w
            if w > self.max_width_seen:
                self.max_width_seen = w

    def start_recovery(self, ev_text, cause=None):
        if self.recovery_t0 is not None:
            return
        ts = self.now
        self.ltssm = "recovery"
        self.recovery_t0 = ts
        self.recovery_ev = self.ev_here(ev_text or "Recovery entry")
        self.recovery_count += 1
        self.ep_speed_before = self.speed
        self.ep_eq = False
        rt = self.recovery_times
        if self.nvme_active:
            rt.append(ts)
        win = self.cfg.recovery_storm_window_s
        while rt and ts - rt[0] > win:
            rt.popleft()
        if len(rt) >= self.cfg.recovery_storm_n and self.nvme_active and not getattr(self, "_rec_storm", False):
            self._rec_storm = True
            physical = any(k for k in self.phys_ep)
            card = "L-26" if physical else "X-11"
            self.emit(card, "RECOVERY-STORM", "error", "link-layer",
                      f"{len(rt)} Recovery entries within {win:g} s (threshold {self.cfg.recovery_storm_n}, configurable) while NVMe "
                      f"traffic is running" + (" , with physical-layer errors before them" if physical else "") + ".",
                      related=["X-11", "L-26", "L-04"], metrics={"recoveries_in_window": len(rt)})
        elif len(rt) < max(1, self.cfg.recovery_storm_n // 2):
            self._rec_storm = False

    def on_os(self, f, d, text):
        t = text.strip().upper()
        ix = self.ix
        if "TS1" in t or "TS2" in t:
            rate = S.parse_rate(f[ix["ts_rate"]])
            lanes = S.parse_lanes(f[ix["ts_lane"]])
            if rate:
                self.ts_last_rate = rate
                if rate > self.max_rate_seen:
                    self.max_rate_seen = rate
            if lanes:
                self.ts_last_lanes = lanes
                if lanes > self.max_width_seen:
                    self.max_width_seen = lanes
                if self.ltssm != "L0" and "TS2" in t:
                    self._set_width(lanes)
            eqc = f[ix["ts_eq"]]
            if eqc:
                self.ep_eq = True
            if self.ltssm == "L0":
                self.start_recovery(f"{t} in L0 (Recovery inferred from ordered sets)")
            elif self.ltssm == "unknown" and self.training_t0 is None:
                self.training_t0 = self.now
                self.training_ev = self.ev_here(f"first {t}")
                self.ltssm = "training"
            elif self.ltssm in ("idle", "L1") and self.l1_entry is not None and self.l1_exit_start is None:
                self.l1_exit_start = (self.now, self.row, self.pkt())
        elif "EIOS" in t or "ELECTRICAL IDLE" in t:
            if self.ltssm == "L0":
                self.ltssm = "idle"
                self.eidle_since = self.now
        elif "EIEOS" in t or "FTS" in t:
            if self.ltssm in ("idle", "L1") and self.l1_entry is not None and self.l1_exit_start is None:
                self.l1_exit_start = (self.now, self.row, self.pkt())
        bad = f[ix["lcrc"]] or f[ix["crc16"]]
        if (bad and S.is_bad(bad)) or "BAD" in t or "INVALID" in t or "MALFORMED" in t:
            self.emit("X-04", "OS-MALFORMED", "warning", "link-layer",
                      f"Ordered set {text.strip()} {self.dname(d)} flagged as malformed/invalid by the analyzer.", side="L")

    def on_event(self, f, d, text):
        k = classify_event(text)
        ts = self.now
        self.events_seen += 1
        self.last_event = (text.strip(), self.row, self.pkt())
        self.link_events_log.append({"row": self.row, "pkt": self.pkt(), "ts": ts, "text": text.strip()})
        if k == "recovery":
            if self.ltssm == "L0" or self.ltssm in ("idle", "L1"):
                if self.l1_entry is not None and self.l1_exit_start is None:
                    self.l1_exit_start = (ts, self.row, self.pkt())
                self.start_recovery(text.strip())
            elif self.ltssm == "unknown":
                self.start_recovery(text.strip())
            else:
                if self.recovery_t0 is None and self.l0_seen:
                    self.start_recovery(text.strip())
        elif k == "recovery_exit" or k == "l0":
            self.enter_l0(text.strip())
        elif k == "eq":
            self.ep_eq = True
            if "fail" in text.lower():
                self.emit("X-13", "EQ-FAIL", "error", "link-layer", f"Link event '{text.strip()}': equalization failure reported.",
                          related=["T-39", "L-25"])
        elif k == "speed":
            rates = [float(x) for x in RATE_RE.findall(text)]
            if rates:
                new = rates[-1]
                old = rates[0] if len(rates) > 1 else self.speed
                self._set_speed(new, "Link Event")
                self.speed_changes.append({"row": self.row, "pkt": self.pkt(), "ts": ts, "from": old, "to": new})
                if self.nvme_active and old and new != old:
                    down = new < old
                    self.emit("X-09", "SPEED-CHANGE", "error" if down else "warning", "link-layer",
                              f"Link speed changed {old:g} -> {new:g} GT/s during NVMe operation" + (" (downshift: half the bandwidth or less from here on)" if down else "") + ".",
                              related=["L-25", "P-11", "T-39"], metrics={"from_gts": old, "to_gts": new})
                self.last_idle_exit = ts
                self.add_interval(ts, ts, "speed change", self.row, self.pkt(), f"{old:g}->{new:g} GT/s")
        elif k == "width":
            ws = [int(x) for x in WIDTH_RE.findall(text)]
            if ws:
                old = ws[0] if len(ws) > 1 else self.width
                new = ws[-1]
                self._set_width(new)
                if self.nvme_active and old and new != old:
                    self.emit("X-09", "WIDTH-CHANGE", "error" if new < old else "warning", "link-layer",
                              f"Link width changed x{old} -> x{new} during NVMe operation.", related=["L-25", "P-11", "X-08"],
                              metrics={"from_width": old, "to_width": new})
        elif k == "l1_entry":
            self.l1_entry = (ts, self.row, self.pkt())
            self.ltssm = "L1"
            self.l1_episodes += 1
        elif k == "l1_exit" or k == "eidle_exit":
            if self.ltssm in ("L1", "idle") and self.l1_entry is not None and self.l1_exit_start is None:
                self.l1_exit_start = (ts, self.row, self.pkt())
            self.last_idle_exit = ts
        elif k == "eidle_entry":
            if self.ltssm == "L0":
                self.ltssm = "idle"
            self.eidle_since = ts
        elif k in ("training", "detect", "polling", "configuration"):
            if k == "training" or k == "detect":
                self.detect_count += 1
                if self.training_t0 is None or self.l0_seen:
                    self.training_t0 = ts
                    self.training_ev = self.ev_here(text.strip())
            if self.l0_seen and self.ltssm == "L0" and k in ("detect", "training"):
                self.link_down(text.strip())
            self.ltssm = "training"
            if self.detect_count >= self.cfg.detect_loop_n and not self.l0_seen and not getattr(self, "_loop_flag", False):
                self._loop_flag = True
                self.emit("X-12", "LTSSM-LOOP", "error", "link-layer",
                          f"{self.detect_count} Detect/training starts without ever reaching L0: the LTSSM is looping.",
                          at=self.training_ev, related=["T-39", "L-39", "T-40"])
        elif k == "compliance":
            self.emit("X-12", "COMPLIANCE", "error", "link-layer",
                      f"Link event '{text.strip()}': the LTSSM entered Polling.Compliance instead of training.", related=["L-39"])
        elif k in ("link_down", "hot_reset", "disabled"):
            self.link_down(text.strip(), k)
        elif k.startswith("phys_"):
            self.on_phys_error(k, text.strip(), d)
        elif k == "nullified":
            self.dll_stats["nullified"] += 1
            self.emit("X-06", "NULLIFIED", "info", "link-layer",
                      f"Nullified TLP ({text.strip()}): legal for cut-through switching; a problem only if it is not followed by the "
                      f"correct retransmission.", side="L")

    def on_phys_error(self, kind, text, d):
        card = PHYS_CARD[kind]
        if kind == "phys_symbol" and self.speed >= 8.0:
            card = "L-01"
        ts = self.now
        ep = self.phys_ep.get(card)
        if ep is not None and ts - ep[0] < 1e-5 and ep[1] is not None:
            ep[0] = ts
            if len(ep[1]["evidence"]) < 30:
                ep[1]["evidence"].append(self.ev_here(text))
            ep[1]["metrics"]["events"] = ep[1]["metrics"].get("events", 1) + 1
            return
        relock = self.last_idle_exit is not None and (ts - self.last_idle_exit) * 1e6 < self.cfg.relock_window_us
        if relock:
            self.relock_pending.append((ts + 2e-5, card, text, d, self.ev_here(text), self.dll_stats["naks"]["U"] + self.dll_stats["naks"]["D"],
                                        self.recovery_count))
            self.phys_ep[card] = [ts, None]
            return
        names = {"X-01": "invalid 10b symbol", "X-02": "running-disparity error", "X-03": "invalid sync header",
                 "X-05": "framing error", "X-07": "lane-to-lane skew", "X-08": "lane reversal / polarity problem",
                 "L-01": "receiver error", "X-10": "SKP ordered-set error"}
        fnd = self.emit(card, "PHY-ERROR", "warning", "link-layer",
                        f"Physical-layer error on the {self.dname(d)} lanes: '{text}' ({names.get(card, card)}) at "
                        f"{self.speed or '?'} GT/s.", related=["L-01", "X-11"] if card != "L-01" else ["X-03", "X-11"], side="L")
        self.phys_ep[card] = [ts, fnd]

    def check_relock(self):
        while self.relock_pending and self.relock_pending[0][0] <= self.now:
            dl, card, text, d, ev0, naks0, rec0 = self.relock_pending.popleft()
            naks = self.dll_stats["naks"]["U"] + self.dll_stats["naks"]["D"]
            if naks > naks0 or self.recovery_count > rec0:
                self.emit("X-14", "IDLE-EXIT-ERRORS", "warning", "link-layer",
                          f"Physical error '{text}' right after an electrical-idle exit / speed change, followed by a Nak or Recovery: "
                          f"a real L0s/L1 exit problem.", at=ev0, related=[card, "X-28"])
            else:
                self.emit("X-28", "RELOCK", "info", "link-layer",
                          f"Physical error '{text}' within {self.cfg.relock_window_us} us of an electrical-idle exit or speed change, "
                          f"with no Nak, replay or Recovery afterwards: most likely the analyzer re-locking, not a link error.",
                          at=ev0, related=["X-14", card], side="A")

    def link_down(self, text, kind="link_down"):
        ts = self.now
        out = self.outstanding_count()
        if self.l0_seen:
            msg_err = self.last_uncorr_msg is not None and ts - self.last_uncorr_msg[0] < 0.01
            if kind == "disabled" and msg_err:
                self.emit("L-28", "DPC", "error", "link-layer",
                          f"Link disabled ('{text}') {(ts - self.last_uncorr_msg[0]) * 1e3:.3f} ms after an uncorrectable error message: "
                          f"consistent with Downstream Port Containment cutting the link.", evidence=[self.last_uncorr_msg[1]],
                          related=["L-11", "T-44", "C-18"], metrics={"outstanding_commands": out})
            elif out:
                self.emit("T-44", "LINK-DOWN-UNDER-IO", "error", "link-layer",
                          f"Link went down ('{text}') with {out} NVMe command(s) and {len(self.np)} PCIe request(s) outstanding.",
                          related=["L-11", "L-27", "L-29", "C-05"], metrics={"outstanding_commands": out, "outstanding_np": len(self.np)})
        self.ltssm = "down" if kind != "hot_reset" else "training"
        self.l0_seen_since_down = False
        for d in ("U", "D"):
            for t in ("P", "NP", "CPL"):
                self.fc[d][t] = None
            self.next_psn[d] = -1
            self.unacked[d].clear()
            self.dll_block[d] = None
        self.l0_seen = self.l0_seen   # keep the "has trained once" flag

    def on_pm_dllp(self, d, text):
        t = text.lower()
        if "request_ack" in t or "request ack" in t:
            if self.pm_req is not None:
                self.pm_req = None
            return
        if "l1" in t:
            self.pm_req = (self.now, self.row, self.pkt(), text)
        if "l23" in t:
            self.pm_req = None

    def check_pm_req(self):
        p = self.pm_req
        if p is not None and self.now - p[0] > 1e-3:
            self.pm_req = None
            if self.ltssm == "L0":
                self.emit("X-14", "L1-ENTRY-FAILED", "info", "link-layer",
                          f"{p[3]} was not acknowledged with PM_Request_Ack within 1 ms and the link stayed in L0: L1 entry attempt "
                          f"failed or was rejected.", at=self.ev("L1 request", p[1], p[2], p[0]), related=["T-45"])
