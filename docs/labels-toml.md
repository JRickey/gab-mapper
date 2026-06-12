# gba-labels v2 — the TOML interchange format

The map gba-mapper emits, the file the recompiler consumes, and the
format any disassembly tooling (Ghidra exports, hand-curated lists)
should speak. Canonical spec lives with the consumer (gba-lib
`docs/labels.md`); this mirror is normative for what the mapper emits.

```toml
format = "gba-labels"
version = 2

[image]
sha256 = "<64 hex digits>"          # pins the file to one image

[[functions]]
name = "AgbMain"                    # optional; default sub_<addr>
address = 0x0800_01c8               # TOML integer; "0x08001234" or
                                    # "08001234" strings also accepted
end = 0x0800_0220                   # optional, exclusive
mode = "thumb"                      # "arm" | "thumb" ("a" | "t" ok)
```

Rules:

- The memory region is derived from the address: `0x08–0x0D` ROM,
  `0x03` IWRAM, `0x02` EWRAM (reserved by the recompiler today).
- A consumer must refuse a file whose `image.sha256` doesn't match the
  image it's working on, and must treat entries as *hints, not trusted
  input* — translation/disassembly derives from the image's own bytes.
- Malformed or out-of-range entries are skipped, never fatal; files
  merge by set union, so concatenating maps from many sources is safe.
- Files carry addresses and names only — **never bytes from the
  image**. Keep it that way; it is what makes maps publishable.

The recompiler also reads/writes a minimal line-based v1 format from
its runtime recorder; the formats are content-detected and
interchangeable through `recomp labels import/export`.
