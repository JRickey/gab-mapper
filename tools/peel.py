#!/usr/bin/env python3
"""Peel a byte range from the baserom into a labeled disassembly file,
and record the function in the map.

Generalized from the pilot project's peel tool (same author): the tree
and ROM are arguments, not constants, and every successful peel appends
the function to the working `<rom stem>.labels.toml` — the map IS the
workflow state.

Each invocation:
  1. Reads [start, end) from the ROM.
  2. Runs arm-none-eabi-objdump on those bytes in the requested mode.
  3. For thumb peels, fact-checks the proposed range with the boundary
     detector (tools/boundary.py); refuses on blocking issues unless
     --force-boundary.
  4. Emits `asm/disasm_0xADDR.s`: one arm/thumb_func_start block whose
     body is an `.incbin` of the original bytes (always byte-identical)
     with the disassembly above as @-comments.
  5. Appends the function to the labels TOML (--no-labels to skip).

After running, the caller still must:
  1. Shrink the surrounding INCBIN (asm/rom.s or neighbors) so the
     peeled bytes aren't included twice.
  2. Add the new .o to linker.ld in address order.
  3. `make check` — must still report an identical image.

Usage:
  python3 tools/peel.py --start 0x080000c0 --end 0x080000f0 --mode thumb
  python3 tools/peel.py --tree path/to/tree --rom baserom.gba \\
      --start 0x08000000 --end 0x08000004 --mode arm --name entry_branch
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boundary
import labels_toml

ROM_BASE = 0x08000000

_OBJDUMP_LINE = re.compile(r"^\s*([0-9a-f]+):\s+([0-9a-f ]+?)\s+(\S.*?)\s*$")


def find_rom(tree: Path, explicit: str | None) -> Path:
    if explicit:
        p = tree / explicit if not Path(explicit).is_absolute() else Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(p)
        return p
    for name in ("baserom.gba",):
        if (tree / name).is_file():
            return tree / name
    roms = sorted(tree.glob("*baserom*.gba")) or sorted(tree.glob("*.gba"))
    if len(roms) == 1:
        return roms[0]
    raise FileNotFoundError(
        f"can't identify the ROM in {tree} (found {len(roms)} candidates); pass --rom"
    )


def disassemble(rom: Path, start: int, end: int, mode: str, objdump: str) -> list[str]:
    cmd = [
        objdump, "-D", "-b", "binary", "-m", "arm7tdmi", "-EL",
        f"--start-address={start - ROM_BASE:#x}",
        f"--stop-address={end - ROM_BASE:#x}",
        str(rom),
    ]
    if mode == "thumb":
        cmd.append("-Mforce-thumb")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"objdump failed:\n{proc.stderr}")
    out = []
    for line in proc.stdout.splitlines():
        m = _OBJDUMP_LINE.match(line)
        if not m:
            continue
        rel = int(m.group(1), 16)
        rest = re.split(r"\s*;\s*", m.group(3), maxsplit=1)[0].strip()
        out.append(f"{rel + ROM_BASE:#010x}: {m.group(2).strip():<10}  {rest}")
    return out


def check_boundary(rom: Path, start: int, end: int, *, force: bool, objdump: str) -> bool:
    report = boundary.detect_boundary(rom, start, end, objdump)
    rec = report["recommendedEnd"]
    interior = [c for c in report["callTargets"] if start < c["to"] < end]
    blocking = []
    if rec != end:
        blocking.append(f"proposed end {end:#x} != detected recommended end {rec:#x}")
    for c in interior:
        blocking.append(
            f"interior bl {c['from']:#x} -> {c['to']:#x} (target inside the range — "
            f"peeling another function as part of this one)"
        )
    if not blocking:
        print("boundary check passed", file=sys.stderr)
        return True
    print(f"boundary check: {len(blocking)} blocking issue(s):", file=sys.stderr)
    for b in blocking:
        print(f"  - {b}", file=sys.stderr)
    for w in report["warnings"]:
        print(f"  warning: {w}", file=sys.stderr)
    if force:
        print("--force-boundary set: proceeding despite issues", file=sys.stderr)
        return True
    print("refusing to peel; re-run with --force-boundary after manual review",
          file=sys.stderr)
    return False


def emit(rom_name: str, start: int, end: int, mode: str, name: str,
         insns: list[str]) -> str:
    size = end - start
    macro = "thumb_func" if mode == "thumb" else "arm_func"
    body = [
        "@ Auto-emitted by tools/peel.py — do not hand-edit this header.",
        f"@ Range:  [{start:#010x}, {end:#010x})  ({size} bytes, {mode} mode)",
        f"@ Re-peel:  python3 tools/peel.py --start {start:#x} --end {end:#x} --mode {mode}",
        "",
        '        .include "asm/macros.inc"',
        "        .syntax unified",
        "",
        "@ Disassembly preview (the bytes come from the INCBIN below):",
        *[f"@   {line}" for line in insns],
        "",
        f"        {macro}_start {name}",
        f"{name}: @ {start:#010x}",
        f'        .incbin "{rom_name}", {start - ROM_BASE:#x}, {size:#x}',
        f"        {macro}_end {name}",
        "",
    ]
    return "\n".join(body)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tree", type=Path, default=Path("."),
                   help="decomp tree root (default: cwd)")
    p.add_argument("--rom", help="ROM path, relative to the tree (default: auto-detect)")
    p.add_argument("--start", required=True, type=lambda s: int(s, 0))
    p.add_argument("--end", required=True, type=lambda s: int(s, 0))
    p.add_argument("--mode", required=True, choices=["arm", "thumb"])
    p.add_argument("--name", help="symbol name (default: sub_<ADDR>)")
    p.add_argument("--out", help="output .s path (default: <tree>/asm/disasm_0xADDR.s)")
    p.add_argument("--labels", help="labels TOML to append to "
                   "(default: <rom stem>.labels.toml beside the ROM)")
    p.add_argument("--no-labels", action="store_true")
    p.add_argument("--force-boundary", action="store_true")
    p.add_argument("--no-boundary-check", action="store_true")
    p.add_argument("--objdump", default="arm-none-eabi-objdump")
    a = p.parse_args()

    tree = a.tree.resolve()
    rom = find_rom(tree, a.rom)
    rom_size = rom.stat().st_size
    if a.end <= a.start:
        print("ERROR: --end must be greater than --start", file=sys.stderr)
        return 1
    if not (ROM_BASE <= a.start and a.end <= ROM_BASE + rom_size):
        print(f"ERROR: range outside ROM [{ROM_BASE:#x}, {ROM_BASE + rom_size:#x})",
              file=sys.stderr)
        return 1

    name = a.name or f"sub_{a.start:08X}"
    out = Path(a.out) if a.out else tree / f"asm/disasm_{a.start:#010x}.s"

    if a.mode == "thumb" and not a.no_boundary_check:
        if not check_boundary(rom, a.start, a.end, force=a.force_boundary,
                              objdump=a.objdump):
            return 2

    insns = disassemble(rom, a.start, a.end, a.mode, a.objdump)
    if not insns:
        print("WARNING: objdump produced no decoded instructions", file=sys.stderr)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(emit(rom.name, a.start, a.end, a.mode, name, insns))

    labeled = ""
    if not a.no_labels:
        lpath = Path(a.labels) if a.labels else labels_toml.state_path(rom)
        fresh = labels_toml.add(
            lpath, rom,
            labels_toml.Fn(address=a.start, mode=a.mode, end=a.end, name=name),
        )
        labeled = f", {'mapped in' if fresh else 'updated'} {lpath.name}"

    try:
        shown = str(out.relative_to(tree))
    except ValueError:
        shown = str(out)
    print(f"wrote {shown}  ({a.end - a.start} bytes, {len(insns)} instrs, "
          f"name={name}{labeled})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
