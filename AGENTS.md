# Agent instructions

You are running the mapping loop described in `docs/workflow.md`.
Ground rules:

- `make check` green is the invariant. Never commit with it red.
  One function (or one explicitly-marked gap) per commit.
- The image and everything derived from its bytes (`baserom.gba`,
  `asm/`, `build/`) is local-only and gitignored. Committed content
  is tooling, docs, and the address-only map. Never commit image
  bytes, and never name commercial titles in committed content —
  images are referenced by SHA-256.
- Boundary decisions follow the evidence ladder: static heuristics →
  small-model classification → strong-model adjudication. Record the
  deciding evidence in the function's `.s` header comment so a human
  can audit any boundary later.
- When reassembly can't reproduce the original encoding, fall back to
  `.incbin` for that range, keep the boundary in the map, and note the
  variance — do not fight the assembler, and do not drop the function.
- The loop must be resumable: state lives in the tree (peeled `.s`
  files + linker entries), not in your context.
