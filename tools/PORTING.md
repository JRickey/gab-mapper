# Porting checklist (from the pilot matching-decomp project)

The pilot repo (private; a full-cartridge LLM-driven matching decomp)
already runs the peel loop this project needs. Mapping needs the
*subset below*, generalized to take any image instead of one
hard-coded title. Per-file notes:

- [ ] `tools/disasm/peel.py` — the core. Extracts a byte range via
  `arm-none-eabi-objdump`, emits a `.s` with `arm_func_start`/
  `thumb_func_start` wrappers. Port as-is, then add the mnemonic-emit
  mode (the pilot emits `.incbin` + comments; the mapper wants real
  mnemonics so reassembly proves the disassembly — keep `.incbin` as
  the fallback path).
- [ ] `tools/agent/ts/cmds/detect-fn-boundary.ts` — interior `bl`
  target detection for Thumb. Port the logic; consider folding into
  peel.py to drop the Node dependency for the minimal loop.
- [ ] `tools/agent/pick_target.py` — next-range selection. Generalize:
  no title-specific layout; walk the residual `.incbin` gaps.
- [ ] `tools/agent/progress.py` — per-range byte-diff against the
  baserom keyed off the `.map` file. Port nearly as-is; this is the
  mismatch localizer in the verify step.
- [ ] `Makefile` + `linker.ld` generation — the pilot's are
  hand-written for one title; `setup` must generate them from the
  image (size, header entry). Templates in `templates/`.
- [ ] Prompts — the pilot's decomp prompts don't apply; the mapper
  needs two much smaller ones: bulk code/data/pool classification
  (small model) and hard-gap adjudication (strong model, fed the
  probe evidence from both decode modes).
- [ ] Seed converters — `recomp labels export` output is already the
  target format; add the Ghidra headless post-script (functions →
  labels TOML).

Explicitly NOT ported: m2c, the permuter, agbcc, objdiff, charmap/
preproc — those serve matching C decompilation, which is out of scope
here (and they carry licenses this repo doesn't want to inherit).
The toolchain dependency floor is: python3, binutils for ARM
(`arm-none-eabi-{as,ld,objdump,objcopy}`), `sha256sum`.
