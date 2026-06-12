# The mapping workflow

The unit of work is one function range. The invariant, held at every
single step, is: **the tree rebuilds to a byte-identical image**
(`make check`, SHA-256 against the user's own dump). Nothing lands
without that proof.

## Phase 0 — bootstrap

`setup <image.gba>` generates the working tree:

- `baserom.gba` symlink + pinned SHA-256
- `asm/rom.s` — the entire image as one `.incbin`
- `linker.ld` — places every section at its exact original address
  (the map is address-truth; nothing is ever shifted)
- `Makefile` — assemble → link → compare

First `make check` passes trivially. From here on it must never fail.

## Phase 1 — peel loop (the overnight part)

Each iteration:

1. **pick** — choose the next unmapped range. Prefer ranges adjacent
   to known code (fall-through, branch targets) over cold gaps.
2. **probe** — disassemble the range in both modes; collect evidence:
   does one mode produce coherent control flow? Does a literal pool
   terminate it? Do known functions branch into it (and in which
   state)? Prologue/epilogue shapes?
3. **decide** — cheap heuristics first; when they disagree or stall,
   ask the model. Bulk classification (code vs data vs pool) is a
   small-model task; genuinely ambiguous gaps (jump tables, computed
   branches, compressed-data false positives) escalate to a stronger
   model with the surrounding disassembly as context.
4. **peel** — emit the function as real mnemonics in its own `.s`
   (with the original bytes retained as a comment trail), shrink the
   surrounding `.incbin`, add the linker entry in address order.
5. **verify** — `make check`. Byte-identity proves the disassembly
   round-trips: right boundary, right mode, right encodings. On
   mismatch, the diff localizes the error to the instruction; fix or
   fall back to `.incbin` for that range and record the boundary
   evidence separately (an incbin'd function is still a *mapped*
   function — the boundary is the product, the mnemonics are the
   proof).
6. **commit** — one function per commit; the loop is resumable at any
   point.

Termination: every byte of the code region is attributed (function,
pool, or data), or the remaining gaps are explicitly marked unknown.

## Phase 2 — emit

`emit` walks the tree and writes `out/<sha256>.labels.toml`
(`gba-labels` v2 — see `docs/labels-toml.md`): one `[[functions]]`
entry per mapped function with `address`, `end`, `mode`, and a `name`
when one is known (default `sub_<addr>`). The recompiler consumes this
directly (`recomp build` picks it up beside the image; `recomp labels
import` merges it into the per-user accumulator).

## Seeding (optional, recommended)

Three free sources of initial truth, all in the same TOML format:

- the recompiler's static analyzer (recursive traversal from the
  header entry, literal-pool harvesting) — exports its block map;
- the recompiler's runtime recorder (`--record-labels` during play,
  then `recomp labels export`) — entry points that were *executed*,
  including computed-branch targets static analysis can't see;
- a Ghidra headless auto-analysis pass, converted by a small script.

Seeds are hints, exactly as in the recompiler: every range still goes
through probe → peel → verify before it enters the map.

## Cost notes

The pilot project ran this class of loop with a mid-tier model and
found it cheap; mapping (no matching C required) is strictly easier.
The intent is: heuristics handle the bulk for free, a small model
(Haiku-class) classifies the long tail, and a strong model sees only
the genuinely hard residue. A full 4–32 MB cartridge overnight on one
API key should be the normal case, not the ceiling.
