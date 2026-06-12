#!/usr/bin/env python3
"""Preseed the map from an existing decomp: harvest every typed
function symbol out of the built ELF and write the working
`<rom stem>.labels.toml`.

The decomp's linker already proved these (the tree builds a
byte-identical image), so each FUNC symbol is a verified function:
address, size (when recorded), name, and CPU state (ARM ELF function
symbols carry the Thumb bit in st_value). The output is exactly the
file the mapping workflow maintains — preseeding and resuming are the
same mechanism.

Usage:
  python3 tools/seed_from_decomp.py --elf frog.elf --rom baserom.gba
  python3 tools/seed_from_decomp.py --elf frog.elf --rom baserom.gba --out map.labels.toml
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labels_toml
from labels_toml import Fn, ROM_BASE

# readelf -sW row:
#    920: 08006519   116 FUNC    LOCAL  DEFAULT    2 Name
_SYM_RE = re.compile(
    r"^\s*\d+:\s+([0-9a-f]+)\s+(\d+|0x[0-9a-f]+)\s+FUNC\s+(\w+)\s+\w+\s+\S+\s+(\S+)\s*$",
    re.I,
)


def harvest(elf: Path, readelf: str) -> list[tuple[Fn, str]]:
    text = subprocess.run(
        [readelf, "-sW", str(elf)], capture_output=True, text=True, check=True
    ).stdout
    out: list[tuple[Fn, str]] = []
    for line in text.splitlines():
        m = _SYM_RE.match(line)
        if not m:
            continue
        value = int(m.group(1), 16)
        size = int(m.group(2), 0)
        bind = m.group(3).upper()
        name = m.group(4)
        thumb = value & 1
        addr = value & ~1
        out.append((
            Fn(
                address=addr,
                mode="thumb" if thumb else "arm",
                end=addr + size if size > 0 else None,
                name=name,
            ),
            bind,
        ))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--elf", required=True, type=Path)
    p.add_argument("--rom", required=True, type=Path)
    p.add_argument("--out", type=Path,
                   help="output TOML (default: <rom stem>.labels.toml beside the ROM)")
    p.add_argument("--readelf", default="arm-none-eabi-readelf")
    a = p.parse_args()

    rom_size = a.rom.stat().st_size
    syms = harvest(a.elf, a.readelf)

    fns: dict[tuple[int, str], Fn] = {}
    kept = dropped = dupes = 0
    for fn, bind in syms:
        if not (ROM_BASE <= fn.address < ROM_BASE + rom_size):
            dropped += 1
            continue
        key = (fn.address, fn.mode)
        if key in fns:
            dupes += 1
            # Prefer the global/longer-named alias for the same entry.
            if bind == "GLOBAL" or (fns[key].name or "").startswith("sub_"):
                fns[key] = fn
            continue
        fns[key] = fn
        kept += 1

    out = a.out or labels_toml.default_path(a.rom)
    existing = 0
    if out.exists():
        sha, prev = labels_toml.load(out)
        if sha != labels_toml.rom_sha256(a.rom):
            print(f"ERROR: {out} is for a different image", file=sys.stderr)
            return 1
        existing = len(prev)
        for key, fn in prev.items():
            fns.setdefault(key, fn)

    labels_toml.save(out, labels_toml.rom_sha256(a.rom), fns)
    print(
        f"seeded {out}: {len(fns)} functions "
        f"({kept} from ELF, {existing} pre-existing, {dupes} alias dupes, "
        f"{dropped} outside ROM)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
