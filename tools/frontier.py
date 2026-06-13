#!/usr/bin/env python3
"""Compute the peel frontier deterministically.

This is the static-heuristics rung of the evidence ladder, as a script
— the workflow's survey agent runs it once instead of harvesting call
targets and probing gaps by hand (which costs two orders of magnitude
more in model tokens than it does here in CPU).

Method:
  1. Load the map (<rom stem>.labels.toml): coverage = union of
     [address, end) (entries without `end` cover conservatively to the
     next entry's address).
  2. One objdump pass over the whole code span in thumb, one in arm:
     harvest every bl/blx target FROM MAPPED RANGES (calls from code we
     trust) that lands in UNMAPPED space — those are guaranteed-entry
     candidates, evidence "bl-target".
  3. Gaps between consecutive mapped ranges inside the code span:
     candidate at the gap start (word-aligned), evidence "gap",
     screened by the boundary detector's pool/padding heuristics —
     gaps that are all literal-pool words or padding are dropped.
  4. Rank: bl-targets first (by call count), then gaps by size
     ascending (small gaps between functions are most likely code).

Output: JSON {codeSpan, mapped, coverageBytes, candidates: [{address,
mode, evidence}]}, capped at --max (default 24).

Usage:
  python3 tools/frontier.py --rom baserom.gba [--labels map.toml] [--max 24]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boundary
import labels_toml
from labels_toml import ROM_BASE


def leading_function(rom: Path, start: int, gap_hi: int, mode: str, objdump: str):
    """If a real function begins at `start`, return its exclusive end;
    else None. Used to rescue a function sitting at the head of a large
    data gap (jump-table targets the static call graph never reaches).
    Only thumb is probed (the boundary detector is thumb-only); a clean
    entry has no prologue warning and an end strictly inside the gap."""
    if mode != "thumb":
        return None
    try:
        rep = boundary.detect_boundary(rom, start, None, objdump)
    except Exception:
        return None
    end = rep["recommendedEnd"]
    prologue_warn = any("doesn't look like a function entry" in w for w in rep["warnings"])
    if prologue_warn or not (start < end <= gap_hi):
        return None
    return end

_BL_RE = re.compile(r"^\s*([0-9a-f]+):\s+[0-9a-f ]+?\s+(bl|blx)\s+0x([0-9a-f]+)", re.I)


def objdump_calls(rom: Path, start: int, end: int, thumb: bool, objdump: str) -> list[tuple[int, int]]:
    """All (from, to) bl/blx pairs in [start, end), one subprocess."""
    cmd = [
        objdump, "-D", "-b", "binary", "-m", "arm7tdmi", "-EL",
        f"--adjust-vma={ROM_BASE:#x}",
        f"--start-address={start:#x}", f"--stop-address={end:#x}", str(rom),
    ]
    if thumb:
        cmd.insert(6, "-Mforce-thumb")
    text = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    out = []
    for line in text.splitlines():
        m = _BL_RE.match(line)
        if m:
            out.append((int(m.group(1), 16), int(m.group(3), 16)))
    return out


def pool_or_padding(rom_bytes: bytes, start: int, end: int) -> bool:
    """True if [start, end) is plausibly all literal pool / padding."""
    off, size = start - ROM_BASE, end - start
    chunk = rom_bytes[off : off + size]
    if all(b == 0x00 for b in chunk) or all(b == 0xFF for b in chunk):
        return True
    if size % 4 == 0 and start % 4 == 0:
        words = [int.from_bytes(chunk[i : i + 4], "little") for i in range(0, size, 4)]
        if all(
            (w >> 24) in (0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08) or w == 0
            for w in words
        ):
            return True
    return False


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--rom", required=True, type=Path)
    p.add_argument("--labels", type=Path)
    p.add_argument("--max", type=int, default=24)
    p.add_argument("--objdump", default="arm-none-eabi-objdump")
    a = p.parse_args()

    lpath = a.labels or labels_toml.state_path(a.rom)
    # Adjudicated non-code: addresses confirmed data/pool live in a
    # sidecar (one "0xADDR data <reason>" line each, appended by the
    # workflow's skip audit) so the frontier converges instead of
    # re-proposing them forever.
    skips: set[int] = set()
    skips_file = Path(str(lpath).replace(".labels.toml", ".skips.txt"))
    if skips_file.exists():
        for line in skips_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    skips.add(int(line.split()[0], 16))
                except ValueError:
                    pass
    if not lpath.exists():
        print(json.dumps({"codeSpan": None, "mapped": 0, "coverageBytes": 0,
                          "candidates": [], "note": f"{lpath} missing — empty map; "
                          "seed with the header entry point"}))
        return 0
    _, fns = labels_toml.load(lpath)
    if not fns:
        print(json.dumps({"codeSpan": None, "mapped": 0, "coverageBytes": 0,
                          "candidates": []}))
        return 0

    rom_bytes = a.rom.read_bytes()
    rom_end = ROM_BASE + len(rom_bytes)

    # Coverage ranges, conservative `end` fallback = next entry start.
    entries = sorted(fns.values(), key=lambda f: f.address)
    ranges: list[tuple[int, int, str]] = []
    for i, f in enumerate(entries):
        end = f.end
        if end is None:
            end = entries[i + 1].address if i + 1 < len(entries) else f.address + 4
        ranges.append((f.address, min(end, rom_end), f.mode))
    span_lo, span_hi = ranges[0][0], max(e for _, e, _ in ranges)

    # Merge for gap detection.
    merged: list[list[int]] = []
    for s, e, _ in ranges:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    coverage = sum(e - s for s, e in merged)

    def in_map(addr: int) -> bool:
        return any(s <= addr < e for s, e in merged)

    # Call harvest: one pass per mode over the span; keep calls that
    # originate inside a mapped range of that mode and land outside the
    # map (and inside ROM).
    candidates: dict[int, dict] = {}
    for thumb in (True, False):
        mode = "thumb" if thumb else "arm"
        mode_ranges = [(s, e) for s, e, m in ranges if m == mode]
        if not mode_ranges:
            continue
        for src, dst in objdump_calls(a.rom, span_lo, span_hi, thumb, a.objdump):
            if not any(s <= src < e for s, e in mode_ranges):
                continue
            if not (ROM_BASE <= dst < rom_end) or in_map(dst):
                continue
            # A bl in thumb code reaches a thumb function (blx would
            # switch; record blx targets as the opposite mode).
            c = candidates.setdefault(dst, {
                "address": f"0x{dst:08x}", "mode": mode, "calls": 0,
                "evidence": "",
            })
            c["calls"] += 1
    for c in candidates.values():
        c["evidence"] = f"bl-target ({c['calls']} call sites in mapped code)"

    # Gaps. A gap of ANY size can begin with a function the static call
    # graph never reaches (computed-branch / jump-table targets), so a
    # large gap is NOT assumed to be one data blob — a function sitting
    # at the start of a big data region must still be found. Small gaps
    # (<= 0x4000) that aren't pool/padding are proposed on the cheap
    # screen alone; larger gaps get a boundary probe at the leading edge
    # and are proposed only when it confirms a real function entry
    # there (a coherent end inside the gap, no prologue warning). This
    # is what makes "fully mapped" actually mean every reachable
    # function, not just the statically-reachable ones.
    # A gap's leading words are often the PREVIOUS function's literal
    # pool. Once the skip audit records the head as data, the gap must
    # not go dark: advance the proposal point past every word that is
    # either already-audited data (skips) or pool-like. Anything the
    # pool heuristic misses costs one audit round-trip, lands in the
    # sidecar, and is walked past on the next survey — convergence is
    # guaranteed by the skips file, the heuristic just makes it fast.
    def advance_past_known_data(start: int, gap_hi: int) -> int:
        while start < gap_hi and (
            start in skips
            or pool_or_padding(rom_bytes, start, min(start + 4, gap_hi))
        ):
            start += 4
        return start

    gaps = []
    for (s1, e1), (s2, _) in zip(merged, merged[1:]):
        gap_lo, gap_hi = e1, s2
        size = gap_hi - gap_lo
        if size < 4:
            continue
        start = advance_past_known_data((gap_lo + 3) & ~3, gap_hi)
        if start >= gap_hi or start in candidates or in_map(start):
            continue
        prev_mode = next((m for s, e, m in reversed(ranges) if e <= gap_lo), "thumb")
        if size <= 0x4000:
            if pool_or_padding(rom_bytes, gap_lo, gap_hi):
                continue
            evidence = f"gap of {size} bytes after mapped code (not pool/padding)"
            gaps.append({
                "address": f"0x{start:08x}", "mode": prev_mode,
                "evidence": evidence, "_size": size,
            })
        else:
            # A large gap is often a long RUN of small functions reached
            # only by computed branches (a handler table). Chain through
            # it in one pass — probe the leading edge, and if it's a
            # clean function, advance past it and probe again — so a
            # single survey surfaces the whole run instead of one
            # function per drain. Stop at the first non-function (real
            # data), or when the per-gap cap is hit.
            cur = start
            found = 0
            while cur < gap_hi and found < a.max:
                probe = leading_function(a.rom, cur, gap_hi, prev_mode, a.objdump)
                if probe is None:
                    break
                gaps.append({
                    "address": f"0x{cur:08x}", "mode": prev_mode,
                    "evidence": (
                        f"function in a {size}-byte gap "
                        f"({cur:#x}..{probe:#x}; computed-branch target)"
                    ),
                    "_size": probe - cur,
                })
                found += 1
                # Skip any inter-function pool/padding before the next.
                nxt = probe
                while nxt < gap_hi and pool_or_padding(rom_bytes, nxt, min(nxt + 4, gap_hi)):
                    nxt += 4
                cur = nxt

    ordered = sorted(candidates.values(), key=lambda c: -c["calls"])
    # Gaps in ADDRESS order: a chained run through one large gap must peel
    # front-to-back so each incbin split is clean.
    ordered += sorted(gaps, key=lambda g: int(g["address"], 16))
    ordered = [c for c in ordered if int(c["address"], 16) not in skips]
    for c in ordered:
        c.pop("calls", None)
        c.pop("_size", None)

    print(json.dumps({
        "codeSpan": f"0x{span_lo:08x}..0x{span_hi:08x}",
        "mapped": len(fns),
        "coverageBytes": coverage,
        "candidates": ordered[: a.max],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
