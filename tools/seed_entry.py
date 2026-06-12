#!/usr/bin/env python3
"""Cold-start seed: map the ROM's entry point when the map is empty.

frontier.py grows the map by harvesting bl/blx targets FROM already-
mapped code, so on a brand-new ROM (empty or absent labels TOML) it has
nothing to chew on and the workflow stalls before the first peel. This
script gives the loop its first foothold.

A GBA cartridge boots through a fixed chain: an ARM branch at
0x08000000 jumps over the 0xC0 header to a small ARM entry stub; the
stub sets up the stacks and then transfers to the game's real entry —
almost always via an indirect `bx rN`, where rN was loaded from a
literal-pool word holding the entry address (with bit0 set for THUMB).
The boot stub itself issues no direct bl/blx, so peeling it yields no
frontier; the productive seed is the function that pointer targets —
typically the THUMB main entry, which is full of bl immediates.

This tool follows that chain deterministically, then peels the one
target function through the normal verified path (peel.py -> wire.py ->
make check) so the map and the asm tree stay consistent. After it runs,
frontier.py returns the entry's call graph and the loop self-propagates.

No-op (exit 0) if the map already holds any function, so it is safe to
run unconditionally at the top of every run.

Usage:
  python3 tools/seed_entry.py [--tree .] [--rom NAME.gba]
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
from peel import find_rom

ROM_BASE = 0x08000000
HERE = Path(__file__).resolve().parent


def u32(b: bytes, off: int) -> int:
    return int.from_bytes(b[off:off + 4], "little")


def header_branch_target(rom: bytes) -> int:
    """Decode the ARM `b` at 0x08000000 -> entry stub address."""
    w = u32(rom, 0)
    if (w >> 24) != 0xEA:  # cond=AL, ARM unconditional branch
        raise SystemExit(f"word0 {w:#010x} is not an ARM branch (top byte != 0xea); "
                         "non-standard header — seed by hand")
    imm = w & 0xFFFFFF
    if imm & 0x800000:
        imm -= 0x1000000
    return ROM_BASE + 8 + (imm << 2)


def objdump_arm(rom: Path, start: int, end: int, objdump: str) -> str:
    return subprocess.run(
        [objdump, "-D", "-b", "binary", "-m", "arm7tdmi", "-EL",
         f"--adjust-vma={ROM_BASE:#x}",
         f"--start-address={start:#x}", f"--stop-address={end:#x}", str(rom)],
        capture_output=True, text=True, check=True,
    ).stdout


_LDR_PC = re.compile(r"\bldr\s+(r\d+|sp|lr|fp|ip),\s*\[pc.*?@\s*0x([0-9a-f]+)", re.I)
_BX = re.compile(r"\bbx\s+(r\d+|lr|fp|ip)\b", re.I)
_MOVPC = re.compile(r"\bmov\s+pc,\s*(r\d+|lr)\b", re.I)
_BIMM = re.compile(r"\b(b|bl)\s+0x([0-9a-f]+)\b", re.I)


def resolve_entry(rom_bytes: bytes, rom: Path, objdump: str) -> tuple[int, str, str]:
    """Follow header -> stub -> indirect pointer to the real entry.

    Returns (address, mode, how) where address has the thumb bit masked
    off and mode is 'arm'/'thumb'."""
    rom_end = ROM_BASE + len(rom_bytes)
    stub = header_branch_target(rom_bytes)
    if not (ROM_BASE <= stub < rom_end):
        raise SystemExit(f"header branch target {stub:#x} outside ROM — seed by hand")

    # Straight-line emulate the stub: track pc-relative loads, resolve
    # the first transfer of control (bx rN / mov pc,rN) through them.
    text = objdump_arm(rom, stub, min(stub + 0x400, rom_end), objdump)
    regs: dict[str, int] = {}
    for line in text.splitlines():
        m = _LDR_PC.search(line)
        if m:
            pool = int(m.group(2), 16)
            if ROM_BASE <= pool < rom_end - 3:
                regs[m.group(1).lower()] = u32(rom_bytes, pool - ROM_BASE)
            continue
        m = _BX.search(line) or _MOVPC.search(line)
        if m:
            reg = m.group(1).lower()
            if reg in regs:
                ptr = regs[reg]
                if ROM_BASE <= (ptr & ~1) < rom_end:
                    mode = "thumb" if ptr & 1 else "arm"
                    return ptr & ~1, mode, f"stub bx {reg} -> pool ptr {ptr:#010x}"
            continue
        m = _BIMM.search(line)
        if m:  # stub branches straight into the entry (no indirection)
            tgt = int(m.group(2), 16)
            if ROM_BASE <= tgt < rom_end and tgt != stub:
                return tgt, "arm", f"stub {m.group(1)} {tgt:#010x}"

    # Fallback: scan the boot region's pool for an odd ROM pointer whose
    # target opens with a THUMB push {..,lr} (0xb5xx) — the entry shape.
    for off in range(stub - ROM_BASE, min(stub - ROM_BASE + 0x400, len(rom_bytes) - 3), 4):
        v = u32(rom_bytes, off)
        t = v & ~1
        if (v & 1) and ROM_BASE <= t < rom_end:
            toff = t - ROM_BASE
            if toff + 1 < len(rom_bytes) and rom_bytes[toff + 1] == 0xB5:
                return t, "thumb", f"pool scan -> {v:#010x} (thumb push prologue)"

    raise SystemExit("could not resolve the entry transfer from the boot stub — "
                     "non-standard crt0; seed the entry by hand with peel.py")


def arm_end(rom: Path, start: int, objdump: str) -> int:
    """Conservative ARM function end: first epilogue (bx lr / ldm ..pc /
    unconditional b) + 4. Byte-identity holds regardless via incbin; the
    loop's audit refines if needed. Only used for the rare ARM entry."""
    text = objdump_arm(rom, start, start + 0x2000, objdump)
    last = None
    for line in text.splitlines():
        m = re.match(r"\s*([0-9a-f]+):\s+[0-9a-f]{8}\s+(\S+)\s*(.*)$", line, re.I)
        if not m:
            continue
        addr, mn, ops = int(m.group(1), 16), m.group(2).lower(), m.group(3).lower()
        if mn == "bx" and "lr" in ops:
            return addr + 4
        if mn in ("b",) and "0x" in ops:
            return addr + 4
        if mn.startswith("ldm") and "pc" in ops:
            return addr + 4
        last = addr
    return (last + 4) if last else start + 4


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, **kw)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tree", type=Path, default=Path("."))
    p.add_argument("--rom", help="ROM file name (default: auto-detect)")
    p.add_argument("--objdump", default="arm-none-eabi-objdump")
    p.add_argument("--name", help="symbol for the seeded entry (default: sub_<addr>)")
    a = p.parse_args()

    tree = a.tree.resolve()
    rom = find_rom(tree, a.rom)
    lpath = labels_toml.state_path(rom)

    # Idempotent: never disturb a map that already has content.
    if lpath.exists():
        try:
            _, fns = labels_toml.load(lpath)
        except Exception:
            fns = {}
        if fns:
            print(f"seed: {lpath.name} already holds {len(fns)} function(s) — nothing to do")
            return 0

    rom_bytes = rom.read_bytes()
    addr, mode, how = resolve_entry(rom_bytes, rom, a.objdump)
    end = (boundary.detect_boundary(rom, addr, objdump=a.objdump)["recommendedEnd"]
           if mode == "thumb" else arm_end(rom, addr, a.objdump))
    if end <= addr:
        raise SystemExit(f"resolved a non-positive range [{addr:#x}, {end:#x})")
    print(f"seed: entry {addr:#010x} ({mode}, end {end:#010x}, {end - addr} bytes) "
          f"via {how}")

    # Seed cleanly or not at all: any failure past the first edit must
    # leave no debris, or the partial state poisons the next run (a map
    # with one entry makes this tool no-op while the tree stays red).
    def revert(reason: str) -> "int":
        if (tree / ".git").is_dir():
            run(["git", "checkout", "--", "."], cwd=tree, capture_output=True)
            run(["git", "clean", "-fdq", "asm"], cwd=tree, capture_output=True)
        if lpath.exists():
            lpath.unlink()
        sys.stderr.write(reason + "\n")
        return 1

    name = a.name or f"sub_{addr:08X}"
    peel = run([sys.executable, str(HERE / "peel.py"), "--tree", str(tree),
                "--rom", rom.name, "--start", hex(addr), "--end", hex(end),
                "--mode", mode, "--name", name])
    if peel.returncode != 0:
        return revert(f"peel.py failed (exit {peel.returncode})")
    wire = run([sys.executable, str(HERE / "wire.py"), "--tree", str(tree),
                "--rom", rom.name, "--start", hex(addr), "--end", hex(end)])
    if wire.returncode != 0:
        return revert(f"wire.py failed (exit {wire.returncode}) — seed by hand")
    chk = run(["make", "-C", str(tree), "check"], capture_output=True)
    sys.stdout.write(chk.stdout)
    if chk.returncode != 0:
        sys.stderr.write(chk.stderr)
        return revert("make check RED after seed — reverted; seed the entry by hand")
    print(f"seed: mapped {name} and verified byte-identity — frontier is now productive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
