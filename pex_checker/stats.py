"""Constant-memory streaming statistics: a fixed log-scale histogram (quantiles to ~9 % resolution) used for
per-(opcode, size) latency baselines, per-stage baselines and the summary percentiles."""
from __future__ import annotations

import math

# 8 buckets per octave from 10 ns to ~1100 s
_LO = 0.01            # µs
_PER_OCT = 8
_NB = _PER_OCT * 37
_LOG_LO = math.log2(_LO)


class LogHist:
    __slots__ = ("b", "n", "sum", "min", "max")

    def __init__(self):
        self.b = [0] * _NB
        self.n = 0
        self.sum = 0.0
        self.min = math.inf
        self.max = -math.inf

    @staticmethod
    def _idx(x: float) -> int:
        if x <= _LO:
            return 0
        i = int((math.log2(x) - _LOG_LO) * _PER_OCT)
        return i if i < _NB else _NB - 1

    def add(self, x: float):
        self.b[self._idx(x)] += 1
        self.n += 1
        self.sum += x
        if x < self.min:
            self.min = x
        if x > self.max:
            self.max = x

    def quantile(self, q: float) -> float:
        if not self.n:
            return math.nan
        target = q * self.n
        acc = 0
        for i, c in enumerate(self.b):
            acc += c
            if acc >= target and c:
                # upper edge of the bucket, clamped to the observed max
                return min(self.max, 2 ** (_LOG_LO + (i + 1) / _PER_OCT))
        return self.max

    def summary(self) -> dict:
        if not self.n:
            return {"n": 0}
        r = lambda v: round(v, 3)
        return {"n": self.n, "min_us": r(self.min), "p50_us": r(self.quantile(0.5)), "p90_us": r(self.quantile(0.9)),
                "p99_us": r(self.quantile(0.99)), "p999_us": r(self.quantile(0.999)), "max_us": r(self.max),
                "mean_us": r(self.sum / self.n)}


def size_bucket(nbytes: int) -> str:
    if nbytes <= 0:
        return "0"
    if nbytes <= 4096:
        return "<=4K"
    if nbytes <= 16384:
        return "<=16K"
    if nbytes <= 65536:
        return "<=64K"
    if nbytes <= 262144:
        return "<=256K"
    return ">256K"
