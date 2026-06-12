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

// Tolerate args arriving as a JSON-encoded string (some callers
// stringify); a bad parse just means defaults.
let a = args
if (typeof a === 'string') {
  try { a = JSON.parse(a) } catch { a = null }
}
const tree = (a && a.tree) || '.'
const mapper = (a && a.mapper) || tree
const maxFunctions = (a && a.maxFunctions) || 6

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
Tools: ${mapper}/tools (peel.py, boundary.py, frontier.py, wire.py, seed_from_decomp.py, setup.py)
Docs: ${mapper}/docs/workflow.md and ${mapper}/AGENTS.md hold the full discipline.
The working map is the gba-labels v2 TOML beside the ROM (<rom stem>.labels.toml).
Token discipline: the tools do the heavy lifting — run them and read their output.
Do not hand-roll disassembly sweeps, coverage math, or convention archaeology.`

// Models are pinned cheap by design: mapping is a volume game. Sonnet
// carries the judgment steps (survey, peel); Haiku writes the report.
const JUDGE = 'sonnet'
const CHEAP = 'haiku'

phase('Survey')
const survey = await agent(
  `You are surveying a GBA mapping tree before a peel run. ${ctx}

Exactly these steps, ~5 tool calls total:
1. If the tree has no Makefile AND no ROM, fail (ok=false, reason). If it has a ROM
   but no Makefile, bootstrap: python3 ${mapper}/tools/setup.py --tree ${tree}.
   If the tree is an existing decomp with its own harness, leave it alone.
2. Run \`make check\` in the tree (pipe through tail -3). MUST pass; else ok=false.
3. Identify the ROM the harness builds against (Makefile/check script names it).
4. Run: python3 ${mapper}/tools/frontier.py --rom <rom>
   Its JSON is the frontier — relay codeSpan/mapped/candidates as-is. Do not
   re-derive or second-guess it; do not disassemble anything yourself.

Return ONLY the structured result.`,
  { schema: SURVEY, label: 'survey', model: JUDGE },
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
The map has grown since the last survey. Two tool calls: identify the ROM, then
run python3 ${mapper}/tools/frontier.py --rom <rom> and relay its JSON as-is
(ok=true, labelsPath beside the ROM). Return ONLY the structured result.`,
      { schema: SURVEY, label: 'resurvey', phase: 'Peel', model: JUDGE },
    )
    queue = (again && again.ok && again.frontier) || []
  }
  const target = queue.shift()
  if (!target) break

  const r = await agent(
    `Peel exactly ONE function in the mapping tree, fully verified. ${ctx}

Target: address ${target.address}, suspected mode ${target.mode}.
Evidence so far: ${target.evidence || 'none recorded'}

Discipline (AGENTS.md governs; target ~8 tool calls):
1. Probe: for thumb run
   python3 ${mapper}/tools/boundary.py --rom <rom> ${target.address} --json
   to get the recommended end; for arm, one objdump of the area to find the
   epilogue/pool boundary. If the bytes are clearly data or a literal pool,
   return status=skipped with the evidence — that is a good outcome.
2. Peel: python3 ${mapper}/tools/peel.py --tree ${tree} --start <addr> --end <end>
   --mode <mode>  (this also records the function in the labels TOML).
3. Wire: python3 ${mapper}/tools/wire.py --tree ${tree} --start <addr> --end <end>
   On exit 3 only (it refuses when the tree doesn't match its model), wire by
   hand following this tree's existing conventions: shrink the covering .incbin,
   add the object to the linker script in ROM address order.
4. Verify: \`make check\` (tail -3) MUST pass. If it fails, revert everything
   (git checkout/clean if a repo) and return status=blocked with the detail.
5. Commit: if the tree is a git repository, commit the peel as one commit
   (message: "peel: <name> [<start>, <end>)"). Never name commercial titles.

Return ONLY the structured result.`,
    { schema: PEEL, label: `peel:${target.address}`, phase: 'Peel', model: JUDGE },
  )
  if (!r) continue
  results.push(r)
  log(`${target.address}: ${r.status}${r.name ? ` (${r.name})` : ''}`)
  if (r.status === 'blocked') break
}

phase('Report')
const report = await agent(
  `Summarize the mapping state of the tree. ${ctx}

1. Confirm \`make check\` is green (tail -3; it must be — say so explicitly).
2. Run python3 ${mapper}/tools/frontier.py --rom <rom> — its JSON carries
   mapped/coverageBytes and the remaining candidates. namedCount = one
   python3 -c tomllib count of entries with a name.
3. One-paragraph summary including the recompiler handoff: the TOML is consumed
   directly by \`recomp build\` when it sits beside the image, or via
   \`recomp labels import\`.

Return ONLY the structured result.`,
  { schema: REPORT, label: 'report', model: CHEAP },
)

return {
  survey: { mapped: survey.mappedCount, frontier: survey.frontier.length },
  peeled: results.filter(r => r.status === 'peeled'),
  skipped: results.filter(r => r.status === 'skipped'),
  blocked: results.filter(r => r.status === 'blocked'),
  report,
}
