#!/usr/bin/env python3
"""gba-labels v2 TOML: the mapper's working state.

One file per image, `<rom stem>.labels.toml` beside the ROM. The
workflow appends to it after every verified peel; the seed script
writes the same file from an existing decomp; the recompiler consumes
it directly. Load → mutate → save round-trips through here so every
producer emits the identical, sorted, hex-addressed form.

Maps are meant to be shared: files are emitted with a CC0 SPDX tag.
They contain addresses and names only — never image bytes.
"""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROM_BASE = 0x08000000


@dataclass(frozen=True)
class Fn:
    address: int          # even; the thumb bit lives in `mode`
    mode: str             # "arm" | "thumb"
    end: int | None = None
    name: str | None = None


def rom_sha256(rom_path: Path) -> str:
    return hashlib.sha256(rom_path.read_bytes()).hexdigest()


def load(path: Path) -> tuple[str, dict[tuple[int, str], Fn]]:
    """Return (sha256, functions keyed by (address, mode))."""
    doc = tomllib.loads(path.read_text())
    if doc.get("format") != "gba-labels" or doc.get("version") != 2:
        raise ValueError(f"{path}: not a gba-labels v2 file")
    sha = doc.get("image", {}).get("sha256")
    if not isinstance(sha, str) or len(sha) != 64:
        raise ValueError(f"{path}: missing/malformed image.sha256")
    fns: dict[tuple[int, str], Fn] = {}
    for f in doc.get("functions", []):
        addr = f.get("address")
        if isinstance(addr, str):
            addr = int(addr.removeprefix("0x").removeprefix("0X"), 16)
        mode = {"a": "arm", "t": "thumb"}.get(f.get("mode"), f.get("mode"))
        if not isinstance(addr, int) or mode not in ("arm", "thumb"):
            continue
        end = f.get("end")
        if isinstance(end, str):
            end = int(end.removeprefix("0x").removeprefix("0X"), 16)
        fns[(addr, mode)] = Fn(addr, mode, end, f.get("name"))
    return sha.lower(), fns


def save(path: Path, sha: str, fns: dict[tuple[int, str], Fn]) -> None:
    lines = [
        "# SPDX-License-Identifier: CC0-1.0",
        "# Function map (gba-labels v2) — addresses and names only, no image bytes.",
        'format = "gba-labels"',
        "version = 2",
        "",
        "[image]",
        f'sha256 = "{sha}"',
    ]
    for fn in sorted(fns.values(), key=lambda f: (f.address, f.mode)):
        lines.append("")
        lines.append("[[functions]]")
        if fn.name:
            esc = fn.name.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'name = "{esc}"')
        lines.append(f"address = 0x{fn.address:08x}")
        if fn.end is not None:
            lines.append(f"end = 0x{fn.end:08x}")
        lines.append(f'mode = "{fn.mode}"')
    path.write_text("\n".join(lines) + "\n")


def add(path: Path, rom_path: Path, fn: Fn) -> bool:
    """Append one function (creating the file if needed). Returns False
    if an entry at that (address, mode) already existed."""
    sha = rom_sha256(rom_path)
    if path.exists():
        fsha, fns = load(path)
        if fsha != sha:
            raise ValueError(f"{path} is for image {fsha[:8]}…, not {sha[:8]}…")
    else:
        fns = {}
    key = (fn.address, fn.mode)
    fresh = key not in fns
    if fresh:
        fns[key] = fn
    else:
        old = fns[key]
        fns[key] = Fn(fn.address, fn.mode, fn.end or old.end, fn.name or old.name)
    save(path, sha, fns)
    return fresh


def default_path(rom_path: Path) -> Path:
    stem = rom_path.name.removesuffix(".gba")
    return rom_path.with_name(f"{stem}.labels.toml")
