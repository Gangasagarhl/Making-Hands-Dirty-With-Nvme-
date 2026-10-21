"""Constant-memory row reader for multi-GB CSV exports.

Reads the file in binary blocks, splits lines itself and only falls back to the csv module for lines that contain a
quote character (quoted fields, embedded commas, embedded newlines).  Yields (row_number, fields) where row_number is
the 1-based data-row index (the header is row 0).  Progress = bytes consumed / file size.
"""
from __future__ import annotations

import csv
import io
import os
import sys
import time

from . import schema

BLOCK = 8 << 20


class TraceReader:
    def __init__(self, path: str, block: int = BLOCK):
        self.path = path
        self.size = os.path.getsize(path)
        self.block = block
        self.bytes_done = 0
        self.header: list[str] = []
        self.header_line_no = 0
        self.delimiter = ","
        self.encoding = "utf-8"

    # ------------------------------------------------------------------------------------------- header detection
    def _sniff(self):
        with open(self.path, "rb") as f:
            head = f.read(1 << 20)
        if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
            self.encoding = "utf-16"
            text = head.decode("utf-16", "replace")
        else:
            text = head.decode("utf-8-sig", "replace")
        lines = text.splitlines()[:60]
        best = (-1, 0, ",")
        for delim in (",", ";", "\t"):
            for i, line in enumerate(lines):
                if delim not in line:
                    continue
                fields = next(csv.reader([line], delimiter=delim))
                sc = schema.header_score(fields)
                if sc > best[0]:
                    best = (sc, i, delim)
        _, self.header_line_no, self.delimiter = best
        self.header = next(csv.reader([lines[self.header_line_no]], delimiter=self.delimiter)) if lines else []
        self.header = [h.replace("﻿", "").strip() for h in self.header]

    # ------------------------------------------------------------------------------------------- iteration
    def rows(self):
        """Generator of (row_no, fields).  Constant memory."""
        self._sniff()
        if self.encoding == "utf-16":
            yield from self._rows_text()
            return
        delim = self.delimiter
        skip = self.header_line_no + 1
        row_no = 0
        pending = None          # accumulated text of a quoted record spanning lines
        with open(self.path, "rb") as f:
            rest = b""
            first = True
            while True:
                blk = f.read(self.block)
                if not blk:
                    if rest:
                        lines = [rest]
                        rest = b""
                    else:
                        break
                else:
                    blk = rest + blk
                    cut = blk.rfind(b"\n")
                    if cut < 0:
                        rest = blk
                        continue
                    rest = blk[cut + 1:]
                    lines = blk[:cut].split(b"\n")
                if first:
                    if lines and lines[0].startswith(b"\xef\xbb\xbf"):
                        lines[0] = lines[0][3:]
                    first = False
                self.bytes_done = f.tell() - len(rest)
                text_lines = b"\n".join(lines).decode("utf-8", "replace").split("\n")
                for line in text_lines:
                    if skip:
                        skip -= 1
                        continue
                    if line.endswith("\r"):
                        line = line[:-1]
                    if pending is not None:
                        pending += "\n" + line
                        if pending.count('"') % 2:
                            continue
                        line, pending = pending, None
                    elif '"' in line:
                        if line.count('"') % 2:
                            pending = line
                            continue
                    if not line:
                        continue
                    row_no += 1
                    if '"' in line:
                        try:
                            fields = next(csv.reader([line], delimiter=delim))
                        except Exception:
                            fields = line.split(delim)
                        yield row_no, fields
                    else:
                        yield row_no, line.split(delim)
            self.bytes_done = self.size

    def _rows_text(self):
        skip = self.header_line_no + 1
        row_no = 0
        with open(self.path, "r", encoding=self.encoding, errors="replace", newline="") as f:
            for fields in csv.reader(f, delimiter=self.delimiter):
                if skip:
                    skip -= 1
                    continue
                if not fields:
                    continue
                row_no += 1
                if row_no % 65536 == 0:
                    try:
                        self.bytes_done = min(self.size, int(f.buffer.tell())) if hasattr(f, "buffer") else 0
                    except Exception:
                        pass
                yield row_no, fields
        self.bytes_done = self.size


class Progress:
    def __init__(self, reader: TraceReader, label: str = "pass 1", every_s: float = 2.0, stream=None, quiet=False):
        self.r = reader
        self.label = label
        self.every = every_s
        self.t0 = time.time()
        self.last = self.t0
        self.stream = stream or sys.stderr
        self.quiet = quiet

    def tick(self, rows: int, force=False):
        now = time.time()
        if self.quiet or (not force and now - self.last < self.every):
            return
        self.last = now
        dt = max(1e-9, now - self.t0)
        frac = self.r.bytes_done / self.r.size if self.r.size else 1.0
        eta = (dt / frac - dt) if frac > 0.001 else 0
        self.stream.write(f"\r[{self.label}] {rows:,} rows  {rows / dt:,.0f} rows/s  {frac * 100:5.1f}% of "
                          f"{self.r.size / 1e9:.2f} GB  elapsed {dt:,.0f}s  eta {eta:,.0f}s   ")
        self.stream.flush()

    def done(self, rows: int):
        if self.quiet:
            return
        self.tick(rows, force=True)
        self.stream.write("\n")
        self.stream.flush()
