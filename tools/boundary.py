#!/usr/bin/env python3
"""detect-fn-boundary — walk Thumb forward from a start address and
report candidate function ends. Flags peel-boundary risks before a
range is committed.

Python port of the pilot project's TypeScript detector (same author),
folded into the mapper to drop the Node dependency. Heuristics:

 - Epilogue detection: `pop {…, pc}`, `bx`, unconditional `b` kill
   fall-through; the last one before pool/padding/next-prologue is the
   candidate end.
 - Literal-pool detection: after an epilogue, 4-byte-aligned words
   whose top byte is a GBA memory region (0x02–0x08) belong to the
   function above.
 - Alignment padding: 0x0000 and 0x46c0 two-byte nops.
 - Next-prologue detection: `push {…, lr}` (or a `svc` BIOS-wrapper
   entry) after pool + padding is the next function.
 - Interior-bl: every `bl` target strictly inside the range means the
   range swallows another function — the strongest boundary signal.

Usage:
  python3 tools/boundary.py --rom baserom.gba 0x080002a4
  python3 tools/boundary.py --rom baserom.gba 0x080002a4 --proposed-end 0x080004c4 --json

Exit codes: 0 clean, 2 = report carries warnings (mirrors the pilot).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROM_BASE = 0x08000000
MAX_WALK_BYTES = 8 * 1024
GBA_REGION_TOPS = {0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08}

# Byte groups are exact: one 8-hex word, or one/two 4-hex halfwords.
# Anything looser eats mnemonics that are themselves hex digits (`add`).
_LINE_RE = re.compile(
    r"^\s*([0-9a-f]+):\s+([0-9a-f]{8}|[0-9a-f]{4}(?:\s+[0-9a-f]{4})?)\s+(\S+)(?:\s+(.*?))?\s*$",
    re.I,
)


def objdump_thumb(rom: Path, start: int, end: int, objdump: str) -> str:
    return subprocess.run(
        [
            objdump, "-D", "-b", "binary", "-m", "arm7tdmi", "-Mforce-thumb",
            f"--adjust-vma={ROM_BASE:#x}",
            f"--start-address={start:#x}",
            f"--stop-address={end:#x}",
            str(rom),
        ],
        capture_output=True, text=True, check=True,
    ).stdout


def parse_disasm(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        raw_bytes = m.group(2)
        out.append({
            "addr": int(m.group(1), 16),
            "raw_bytes": raw_bytes,
            "bytes_num": int(raw_bytes.replace(" ", ""), 16),
            "mnemonic": m.group(3).lower(),
            "operands": (m.group(4) or "").strip(),
        })
    return out


def is_epilogue(i: dict) -> bool:
    if i["mnemonic"] == "pop" and re.search(r"\bpc\b", i["operands"]):
        return True
    if i["mnemonic"] == "bx" and re.search(r"\b(lr|r\d+)\b", i["operands"]):
        return True
    return i["mnemonic"] in ("b", "b.n", "b.w")


def call_target(i: dict) -> int | None:
    if i["mnemonic"] in ("bl", "blx"):
        m = re.search(r"0x([0-9a-f]+)", i["operands"], re.I)
        if m:
            return int(m.group(1), 16)
    return None


def is_push_lr(i: dict) -> bool:
    return i["mnemonic"] == "push" and re.search(r"\blr\b", i["operands"]) is not None


def is_svc_start(i: dict) -> bool:
    # 4-byte `svc N; bx lr` BIOS wrappers have no push frame; treat the
    # svc as a prologue equivalent so wrapper tables don't fuse.
    return i["mnemonic"] in ("svc", "swi")


def is_alignment_pad(i: dict) -> bool:
    return i["bytes_num"] in (0x0000, 0x46C0)


def is_likely_pool_word(addr: int, lines: list[dict]) -> bool:
    idx = next((k for k, l in enumerate(lines) if l["addr"] == addr), None)
    if idx is None:
        return False
    first = lines[idx]
    if len(first["raw_bytes"]) == 8:
        return (first["bytes_num"] >> 24) & 0xFF in GBA_REGION_TOPS
    if idx + 1 >= len(lines) or lines[idx + 1]["addr"] != addr + 2:
        return False
    word = (lines[idx + 1]["bytes_num"] << 16) | first["bytes_num"]
    return (word >> 24) & 0xFF in GBA_REGION_TOPS


def detect_boundary(
    rom: Path, start: int, proposed_end: int | None = None,
    objdump: str = "arm-none-eabi-objdump",
) -> dict:
    walk_end = start + MAX_WALK_BYTES
    lines = [
        l for l in parse_disasm(objdump_thumb(rom, start, walk_end, objdump))
        if start <= l["addr"] < walk_end
    ]
    if not lines:
        raise RuntimeError(f"no instructions disassembled in [{start:#x}, {walk_end:#x})")

    warnings: list[str] = []
    first = lines[0]
    if (
        not is_push_lr(first)
        and first["mnemonic"] not in ("push", "sub", "mov")
        and not is_svc_start(first)
    ):
        warnings.append(
            f"start {start:#x} doesn't look like a function entry "
            f'(first insn: "{first["mnemonic"]} {first["operands"]}")'
        )

    call_targets: list[dict] = []
    candidates: list[dict] = []
    for i, insn in enumerate(lines):
        to = call_target(insn)
        if to is not None:
            call_targets.append({"from": insn["addr"], "to": to})
        if not is_epilogue(insn):
            continue
        # Scan past pool words and padding for the next prologue.
        j = i + 1
        while j < len(lines):
            probe = lines[j]
            if is_likely_pool_word(probe["addr"], lines):
                j += 1
                if (
                    len(probe["raw_bytes"]) != 8
                    and j < len(lines)
                    and lines[j]["addr"] == probe["addr"] + 2
                ):
                    j += 1
                continue
            if is_alignment_pad(probe):
                j += 1
                continue
            if is_push_lr(probe) or is_svc_start(probe):
                candidates.append({
                    "end": probe["addr"],
                    "epilogueAddr": insn["addr"],
                    "reason": (
                        f"epilogue at {insn['addr']:#x}, pool + padding, then "
                        f"next-entry signal at {probe['addr']:#x}"
                    ),
                })
            break  # more code => mid-function return; resume the walk

    if candidates:
        first_pass_end = candidates[0]["end"]
    else:
        last_epi = next((l for l in reversed(lines) if is_epilogue(l)), None)
        if last_epi:
            first_pass_end = last_epi["addr"] + (2 if len(last_epi["raw_bytes"]) == 4 else 4)
            warnings.append(
                f"no clean next-prologue boundary within {MAX_WALK_BYTES} bytes; "
                f"falling back to last epilogue end {first_pass_end:#x} — verify manually"
            )
        else:
            first_pass_end = walk_end
            warnings.append(
                f"no epilogue or next prologue within {MAX_WALK_BYTES} bytes of "
                f"{start:#x}; wrong mode, unusual control flow, or wrong start"
            )

    interior = [c for c in call_targets if start < c["to"] < first_pass_end]
    recommended_end = first_pass_end
    if interior:
        earliest = min(interior, key=lambda c: c["to"])
        recommended_end = earliest["to"]
        warnings.append(
            f"INTERIOR CALL: bl {earliest['from']:#x} -> {earliest['to']:#x} is a "
            f"function entry inside the range; recommended end revised "
            f"{first_pass_end:#x} -> {recommended_end:#x}"
        )

    if proposed_end is not None and proposed_end != recommended_end:
        rel = "past" if proposed_end > recommended_end else "short of"
        warnings.append(
            f"proposed end {proposed_end:#x} is {rel} detected boundary "
            f"{recommended_end:#x} by {abs(proposed_end - recommended_end)} bytes"
        )

    return {
        "start": start,
        "recommendedEnd": recommended_end,
        "candidates": candidates,
        "callTargets": call_targets,
        "warnings": warnings,
        "proposedEnd": proposed_end,
    }


def _addr(s: str) -> int:
    return int(s, 16) if not s.lower().startswith("0x") else int(s, 0)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("start", type=_addr)
    p.add_argument("--rom", required=True, type=Path)
    p.add_argument("--proposed-end", type=_addr)
    p.add_argument("--json", action="store_true")
    p.add_argument("--objdump", default="arm-none-eabi-objdump")
    a = p.parse_args()

    report = detect_boundary(a.rom, a.start, a.proposed_end, a.objdump)
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"start            {report['start']:#010x}")
        print(
            f"recommended end  {report['recommendedEnd']:#010x}  "
            f"(size {report['recommendedEnd'] - report['start']})"
        )
        interior = [
            c for c in report["callTargets"]
            if report["start"] < c["to"] < report["recommendedEnd"]
        ]
        for c in interior:
            print(f"  interior bl {c['from']:#010x} -> {c['to']:#010x}")
        for w in report["warnings"]:
            print(f"  warning: {w}")
    return 2 if report["warnings"] else 0


if __name__ == "__main__":
    sys.exit(main())
