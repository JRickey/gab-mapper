// gba-mapper — the mapping loop as a Claude Code workflow.
//
// Generic by design: clone this repo, drop a ROM in the repo root, run
// the workflow. No paths are hardcoded; state lives in the tree — the
// harness files plus the working `<rom stem>.labels.toml` — so any run
// resumes where the last one stopped, and a map preseeded from an
// existing decomp (tools/seed_from_decomp.py) is indistinguishable
// from one the loop built itself.
//
// args (all optional):
//   tree          target tree (default "." — the cloned repo itself)
//   mapper        gba-mapper checkout holding tools/ (default: tree)
//   maxFunctions  peel budget for this run (default 6)
//
// Invariant inherited from the tools: `make check` (byte-identity
// against the user's own ROM) must be green after every step; nothing
// enters the map unverified.

export const meta = {
  name: 'gba-map',
  description: 'Resumable GBA function-mapping loop: peel, verify byte-identity, grow the labels TOML',
  whenToUse: 'Inside a gba-mapper clone with a ROM dropped in (or pointed at any byte-identity decomp tree via args.tree). Each run peels up to maxFunctions verified functions and updates the map.',
  phases: [
    { title: 'Survey', detail: 'oracle green, locate ROM + map, find the frontier' },
    { title: 'Peel', detail: 'one verified function per step, one commit each' },
    { title: 'Report', detail: 'map stats and recompiler handoff' },
  ],
}

const tree = (args && args.tree) || '.'
const mapper = (args && args.mapper) || tree
const maxFunctions = (args && args.maxFunctions) || 6

const SURVEY = {
  type: 'object',
  required: ['ok', 'romPath', 'labelsPath', 'mappedCount', 'frontier'],
  properties: {
    ok: { type: 'boolean' },
    reason: { type: 'string', description: 'why not ok / anything notable' },
    romPath: { type: 'string' },
    labelsPath: { type: 'string' },
    mappedCount: { type: 'number' },
    codeSpan: { type: 'string', description: 'e.g. "0x08000000..0x08036000"' },
    frontier: {
      type: 'array',
      description: 'candidate function entries, best evidence first',
      items: {
        type: 'object',
        required: ['address', 'mode'],
        properties: {
          address: { type: 'string', description: '0x-hex' },
          mode: { type: 'string', enum: ['arm', 'thumb'] },
          evidence: { type: 'string' },
        },
      },
    },
  },
}

const PEEL = {
  type: 'object',
  required: ['status', 'detail'],
  properties: {
    status: { type: 'string', enum: ['peeled', 'skipped', 'blocked'] },
    name: { type: 'string' },
    address: { type: 'string' },
    end: { type: 'string' },
    detail: { type: 'string', description: 'what happened; for skipped: the data/pool evidence; for blocked: the failure' },
  },
}

const REPORT = {
  type: 'object',
  required: ['mappedCount', 'summary'],
  properties: {
    mappedCount: { type: 'number' },
    namedCount: { type: 'number' },
    coverageBytes: { type: 'number' },
    summary: { type: 'string' },
  },
}

const ctx = `Tree: ${tree}
Tools: ${mapper}/tools (peel.py, boundary.py, seed_from_decomp.py, setup.py, labels_toml.py)
Docs: ${mapper}/docs/workflow.md and ${mapper}/AGENTS.md hold the full discipline.
The working map is the gba-labels v2 TOML beside the ROM (<rom stem>.labels.toml).`

phase('Survey')
const survey = await agent(
  `You are surveying a GBA mapping tree before a peel run. ${ctx}

Steps:
1. If the tree has no Makefile AND no ROM, fail (ok=false, reason). If it has a ROM
   but no Makefile, bootstrap: python3 ${mapper}/tools/setup.py --tree ${tree}, then
   verify. If the tree is an existing decomp with its own harness, leave it alone.
2. Run \`make check\` in the tree. It MUST pass; if not, ok=false with the failure.
3. Locate the ROM (the .gba the harness builds against) and the working labels TOML
   beside it. A missing TOML is fine (empty map, mappedCount=0).
4. Build the frontier: read the TOML's [[functions]] coverage, find unmapped ranges
   that are plausibly code. Evidence ladder, best first: (a) bl/blx targets from
   already-mapped functions that land in unmapped space (disassemble a few mapped
   functions with arm-none-eabi-objdump to harvest these); (b) gaps between
   consecutive mapped functions inside the code span whose bytes disassemble
   coherently (use ${mapper}/tools/boundary.py --rom <rom> <addr> to probe);
   (c) the address right after a mapped function's end+pool. Do NOT include ranges
   that are clearly literal pools or data tables. Cap the frontier at 24 entries.
5. Report codeSpan (lowest..highest mapped address, or the header entry if empty).

Return ONLY the structured result.`,
  { schema: SURVEY, label: 'survey' },
)

if (!survey || !survey.ok) {
  return { error: (survey && survey.reason) || 'survey failed', survey }
}
log(`map: ${survey.mappedCount} functions; frontier: ${survey.frontier.length} candidates`)

phase('Peel')
const results = []
let queue = survey.frontier.slice()
let resurveyed = false
while (results.filter(r => r.status === 'peeled').length < maxFunctions) {
  if (queue.length === 0 && !resurveyed) {
    // The frontier moves as peels land; one refresh per run.
    resurveyed = true
    const again = await agent(
      `Refresh the peel frontier for the mapping tree. ${ctx}
The map has grown since the last survey. Re-derive candidate function entries
exactly as in the survey discipline (bl targets into unmapped space first, then
coherent gaps), excluding everything already in the TOML. Cap at 24. make check
state must be left untouched. Return ONLY the structured result.`,
      { schema: SURVEY, label: 'resurvey', phase: 'Peel' },
    )
    queue = (again && again.ok && again.frontier) || []
  }
  const target = queue.shift()
  if (!target) break

  const r = await agent(
    `Peel exactly ONE function in the mapping tree, fully verified. ${ctx}

Target: address ${target.address}, suspected mode ${target.mode}.
Evidence so far: ${target.evidence || 'none recorded'}

Discipline (AGENTS.md governs; summary):
1. Probe: for thumb run
   python3 ${mapper}/tools/boundary.py --rom <rom> ${target.address} --json
   to get the recommended end; for arm, disassemble with objdump and find the
   epilogue/pool boundary yourself. If the bytes are clearly data or a literal
   pool, return status=skipped with the evidence — that is a good outcome.
2. Peel: python3 ${mapper}/tools/peel.py --tree ${tree} --start <addr> --end <end>
   --mode <mode>  (this also records the function in the labels TOML).
3. Wire: follow THIS tree's existing conventions — shrink the covering .incbin so
   the bytes aren't duplicated, and add the new object to the linker script in
   ROM address order. Study how previously peeled functions are wired here and
   match them exactly.
4. Verify: \`make check\` MUST pass. If it fails, fix or revert everything
   (including the TOML entry — git checkout works if the tree is a repo) and
   return status=blocked with the failure detail.
5. Commit: if the tree is a git repository, commit the peel as one commit
   (message: "peel: <name> [<start>, <end>)"). Never name commercial titles.

Return ONLY the structured result.`,
    { schema: PEEL, label: `peel:${target.address}`, phase: 'Peel' },
  )
  if (!r) continue
  results.push(r)
  log(`${target.address}: ${r.status}${r.name ? ` (${r.name})` : ''}`)
  if (r.status === 'blocked') break
}

phase('Report')
const report = await agent(
  `Summarize the mapping state of the tree. ${ctx}

1. Confirm \`make check\` is green (it must be — say so explicitly).
2. Parse the labels TOML (python3 + tomllib): mappedCount, namedCount, total
   coverage bytes (sum of end-address where present), and the largest remaining
   unmapped gaps inside the code span.
3. One-paragraph summary including the recompiler handoff: the TOML is consumed
   directly by \`recomp build\` when it sits beside the image, or via
   \`recomp labels import\`.

Return ONLY the structured result.`,
  { schema: REPORT, label: 'report' },
)

return {
  survey: { mapped: survey.mappedCount, frontier: survey.frontier.length },
  peeled: results.filter(r => r.status === 'peeled'),
  skipped: results.filter(r => r.status === 'skipped'),
  blocked: results.filter(r => r.status === 'blocked'),
  report,
}
