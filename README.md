# gba-mapper

Automated LLM driven dissassembly mapping for GBA cartridge images with a verifiable success oracle for pristine output, every time, any image.

Generic rom image dissassembly cannot be done programmatically in any shape or form. Thus, we have agents do it.

Launch Claude Code or another compatible harness, point the workflow at a `.gba` image. Let it run, might take overnight, and it produces a **map** in the form of `<game>.labels.toml`.

This file is a map of every function's address, boundary, and CPU mode, each one proven by reassembly, with the linker as the truth oracle. LLM hallucinations, mistakes, or other undesired behaviors are rejected by the oracle within the harness and force agents to go back and reevaluate.

That map feeds my static recompiler [gba-recomp](https://github.com/JRickey/gba-recomp) which turns the image into a fully native, portable executable.

The map is the cheap path. It contains no deocmpiled C and no meaningful names (`sub_08001234` for example). If you want to easily make modifications to the emitted C by the recomp tool after plugging in your map, you can enrich your map yourself.

If not, the map is still *complete* and *verified*, which is all the recompiler needs.

Otherwise, feel free to use the map to work on a matching decompilation, or other things. Map outputs from this tool should be licensed CC0, so everyone can use them freely. See [License](#License).

## What it produces

- `out/<sha256>.labels.toml` — the function map in the `gba-labels`
  v2 interchange format (see `docs/labels-toml.md`). Addresses and
  names only; **never bytes from the image**. Safe to publish.
- `asm/` + `build/*.o` — the disassembled function tree, reassembled
  and relinked to a byte-identical image at every step (`make check`).
  Derived from the image; stays on your machine.

## How it works

The pipeline is a generalized implementation of my automated matching decompilation workflow I used on my matching decompilation in [A GBA Decomp I did](https://github.com/JRickey/frog-adv-temple-decomp)

However, this pipeline is limited only to mapping functions within the image, not attempting matching decompilation or reference C, for use with my static recompiler.

1. **Bootstrap** — the whole image is one opaque `.incbin`; the build
   links it back together and the SHA matches by construction.
2. **Peel** — functions are split out of the blob one range at a time:
   disassemble, emit real mnemonics, reassemble, relink at the exact
   original address, `make check`. Byte-identity proves the
   disassembly (and therefore the boundary and mode) was right.
   Encoding-variant corner cases fall back to `.incbin` with the
   boundary evidence recorded.
3. **Discover** — boundary candidates come from cheap sources first:
   the cartridge header entry point, recursive traversal, literal-pool
   layout, prologue signatures, and optionally a Ghidra headless
   auto-analysis pass and runtime-recorded entry points imported from
   the recompiler (`recomp labels export`). An LLM pass (small/cheap
   models for bulk classification, a stronger model for ambiguous
   gaps) resolves what static heuristics can't: data-vs-code, jump
   tables, interleaved literal pools.
4. **Emit** — the verified function set is written as
   `gba-labels` v2 TOML.

## Running it

```sh
git clone <this repo> && cd gba-mapper
cp /path/to/your-dump.gba .            # drop the ROM in
python3 tools/setup.py                 # phase 0 — make check passes by construction
# then, in Claude Code, run the workflow:
#   Workflow {scriptPath: "workflows/map.js", args: {maxFunctions: 20}}
```

Each run peels up to `maxFunctions` verified functions and stops;
state lives in the tree (the harness + the labels TOML), so re-running
resumes exactly where the last run stopped. To continue a decomp that
already exists, preseed the same state file from its built ELF:

```sh
python3 tools/seed_from_decomp.py --elf game.elf --rom baserom.gba
```

The seed writes the identical `<rom stem>.labels.toml` the workflow
maintains — preseeding and resuming are the same mechanism. Pointing
the workflow at an external tree (its own harness, e.g. an existing
decomp project) works via `args: {tree: "...", mapper: "<this repo>"}`.

Status: tools ported and verified (the boundary detector matches the
pilot's TS implementation output-for-output; the full bootstrap → peel
→ byte-identity cycle is tested); the workflow is live. Remaining:
richer frontier heuristics, the Ghidra seed converter, ARM-mode
boundary detection.

## Relationship to gba-recomp

This repository is consumed as a submodule by the recompiler repo, but
runs standalone: its only interface to the recompiler is the labels
TOML file. The recompiler can also *seed* the mapper (its analyzer's
statically-reachable block map and its runtime fallback recorder both
export the same format), so the two tools converge on one map from
both directions.

## License

**Code: MIT** (see LICENSE). **Maps: CC0.** The `.labels.toml` files
this tool emits carry addresses and names only — never image bytes —
and are meant to be shared without friction: maps published by this
project are dedicated to the public domain under CC0-1.0, and we
recommend the same for maps you publish (a `# SPDX-License-Identifier:
CC0-1.0` comment at the top of the file is the convention).

External tools the workflow invokes (the agbcc-toolchain binutils,
Ghidra, objdump) keep their own licenses; invoking them does not
affect the license of this repository or of the maps it emits.