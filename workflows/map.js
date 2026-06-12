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
//   maxFunctions  optional per-run peel CAP. Default UNBOUNDED: the loop
//                 runs until the frontier is genuinely dry (no functions
//                 left, only data), which is the whole point of an
//                 overnight run. Set it only to deliberately stop early.
//
// The loop is uninterrupted by design: it keeps draining the frontier
// round after round until no real function remains. The only hard stop
// other than "dry" is the workflow runtime's own agent ceiling (~1000
// agents/run); the loop is fully resumable (all state lives in the tree
// + labels TOML), so on a cartridge larger than one run's ceiling you
// just launch it again and it continues where it left off.
//
// Invariant inherited from the tools: `make check` (byte-identity
// against the user's own ROM) must be green after every step; nothing
// enters the map unverified.

export const meta = {
  name: 'gba-map',
  description: 'Resumable GBA function-mapping loop: peel, verify byte-identity, grow the labels TOML',
  whenToUse: 'Inside a gba-mapper clone with a ROM dropped in (or pointed at any byte-identity decomp tree via args.tree). Drains the frontier until no real function remains (unbounded by default; resumable across runs). Pass maxFunctions only to cap a run deliberately.',
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
// Unbounded by default — drain until the frontier is dry. A caller may
// pass a number to cap a run deliberately.
const maxFunctions = (a && a.maxFunctions) || Infinity

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

// Models are pinned cheap by design: mapping is a volume game. Since
// the tools carry the heavy lifting (frontier.py, wire.py), everything
// runs on Haiku first; a peel that comes back blocked or empty is
// retried once on Sonnet (the escalation rung: ambiguous data-vs-code
// calls, ARM boundaries, manual wiring on nonstandard trees). Each
// result records which tier it needed, so the ladder stays honest.
const JUDGE = 'sonnet'
const CHEAP = 'haiku'

phase('Survey')
const survey = await agent(
  `You are surveying a GBA mapping tree before a peel run. ${ctx}

Exactly these steps, ~6 tool calls total:
1. If the tree has no Makefile AND no ROM, fail (ok=false, reason). If it has a ROM
   but no Makefile, bootstrap: python3 ${mapper}/tools/setup.py --tree ${tree}.
   If the tree is an existing decomp with its own harness, leave it alone.
2. Run \`make check\` in the tree (pipe through tail -3). MUST pass; else ok=false.
3. Identify the ROM the harness builds against (Makefile/check script names it).
4. Cold-start seed: python3 ${mapper}/tools/seed_entry.py --tree ${tree} --rom <rom>
   On a brand-new ROM the map is empty and frontier.py would return nothing —
   this maps the entry point (following the boot chain to the first call-bearing
   function) so the frontier is productive. It is a no-op if the map already has
   functions and self-verifies \`make check\`; relay its failure as ok=false.
5. Run: python3 ${mapper}/tools/frontier.py --rom <rom>
   Its JSON is the frontier — relay codeSpan/mapped/candidates as-is. Do not
   re-derive or second-guess it; do not disassemble anything yourself.

Return ONLY the structured result.`,
  { schema: SURVEY, label: 'survey', model: CHEAP },
)

if (!survey || !survey.ok) {
  return { error: (survey && survey.reason) || 'survey failed', survey }
}
log(`map: ${survey.mappedCount} functions; frontier: ${survey.frontier.length} candidates`)

phase('Peel')
const results = []
const AUDIT = {
  type: 'object',
  required: ['verdicts'],
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        required: ['address', 'verdict', 'reasoning'],
        properties: {
          address: { type: 'string' },
          verdict: { type: 'string', enum: ['data', 'function', 'unsure'] },
          reasoning: { type: 'string' },
        },
      },
    },
  },
}

const peelPrompt = (target, escalated) => `Peel exactly ONE function in the mapping tree, fully verified. ${ctx}

Target: address ${target.address}, suspected mode ${target.mode}.
Evidence so far: ${target.evidence || 'none recorded'}
${escalated ? `
ESCALATION: a cheaper attempt at this target came back blocked, empty, or
wrongly skipped. First make the tree pristine — git status must show no peel
debris (git checkout/clean it if a repo; delete stray
asm/disasm_${target.address}.s otherwise) and \`make check\` must pass — then
take the harder path yourself (manual boundary reasoning, manual wiring) where
the tools refuse.
` : ''}
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
   There is NO SUCH THING as an expected mismatch — a red oracle is never
   committed, never rationalized.
5. Commit: if the tree is a git repository, commit the peel as one commit
   (message: "peel: <name> [<start>, <end>)"). Never name commercial titles.

Return ONLY the structured result.`

// Drain-until-dry: peel rounds alternate with skip audits until a
// round produces no peels and no overturned skips. Audited-data
// addresses persist in the .skips.txt sidecar (frontier.py excludes
// them), so the frontier converges instead of re-proposing them.
const attempted = new Set()
const audits = []
let queue = survey.frontier.filter(t => !attempted.has(t.address))
// Safety backstop only — NOT a work limit. The loop's real exit is
// "frontier dry" below; this just bounds a pathological non-converging
// run. Set high so it never stops a genuine mapping early (the runtime
// agent ceiling bites first on a large cartridge, and the run resumes).
const MAX_ROUNDS = 1000
for (let round = 1; round <= MAX_ROUNDS; round++) {
  if (results.filter(r => r.status === 'peeled').length >= maxFunctions) {
    log(`peel cap (${maxFunctions}) reached — stopping early as requested`)
    break
  }
  if (queue.length === 0 && round > 1) {
    const again = await agent(
      `Refresh the peel frontier for the mapping tree. ${ctx}
The map has grown since the last survey. Two tool calls: identify the ROM, then
run python3 ${mapper}/tools/frontier.py --rom <rom> and relay its JSON as-is
(ok=true, labelsPath beside the ROM). Return ONLY the structured result.`,
      { schema: SURVEY, label: `resurvey:r${round}`, phase: 'Peel', model: CHEAP },
    )
    queue = ((again && again.ok && again.frontier) || [])
      .filter(t => !attempted.has(t.address))
  }
  if (queue.length === 0) {
    log(`round ${round}: frontier dry`)
    break
  }

  const peeledBefore = results.filter(r => r.status === 'peeled').length
  const roundSkips = []
  while (queue.length > 0) {
    if (results.filter(r => r.status === 'peeled').length >= maxFunctions) break
    const target = queue.shift()
    attempted.add(target.address)
    let r = await agent(peelPrompt(target, target.escalate === true), {
      schema: PEEL,
      label: `peel${target.escalate ? '+' : ''}:${target.address}`,
      phase: 'Peel',
      model: target.escalate ? JUDGE : CHEAP,
    })
    let tier = target.escalate ? JUDGE : CHEAP
    if (!r || r.status === 'blocked') {
      r = await agent(peelPrompt(target, true), {
        schema: PEEL, label: `peel+:${target.address}`, phase: 'Peel', model: JUDGE,
      })
      tier = JUDGE
    }
    if (!r) continue
    r.tier = tier
    r.address = r.address || target.address
    results.push(r)
    log(`${target.address}: ${r.status}${r.name ? ` (${r.name})` : ''} [${tier}]`)
    if (r.status === 'skipped') roundSkips.push({ target, r })
  }

  // Audit this round's skips on the judge tier. Wrong skips are the
  // one failure the byte-identity oracle can't catch (it gates
  // correctness, not completeness) — e.g. dismissing a bl-target for
  // lacking a push prologue when prologue-less leaf helpers exist.
  // verdict=data persists to the sidecar; verdict=function requeues
  // escalated.
  if (roundSkips.length) {
    const audit = await agent(
      `Adversarially audit ${roundSkips.length} skip decision(s) from a mapping run. ${ctx}

A cheaper tier judged these frontier candidates "not a function" (data/pool/
padding). For each, verify with the tools — objdump the area, run
${mapper}/tools/boundary.py — remembering: a bl target IS a function entry even
without a push prologue (leaf helpers); structured words after an epilogue are
usually pool. Do not modify code, asm, or the linker. For every verdict=data
address, append one line "0xADDR data <ten-word reason>" to the sidecar file
next to the labels TOML, named <same stem>.skips.txt (create it if missing) —
that file is what stops the frontier from re-proposing settled data.

${roundSkips.map(s => `- ${s.target.address}: ${s.r.detail}`).join('\n')}

Return ONLY the structured result.`,
      { schema: AUDIT, label: `skip-audit:r${round}`, phase: 'Peel', model: JUDGE },
    )
    if (audit) {
      audits.push(audit)
      for (const v of audit.verdicts || []) {
        if (v.verdict === 'function') {
          attempted.delete(v.address)
          queue.push({
            address: v.address, mode: 'thumb', escalate: true,
            evidence: `skip overturned on audit: ${v.reasoning}`,
          })
          log(`${v.address}: skip overturned — requeued escalated`)
        }
      }
    }
  }

  const roundPeeled =
    results.filter(r => r.status === 'peeled').length - peeledBefore
  if (roundPeeled === 0 && queue.length === 0) {
    log(`round ${round}: no progress — stopping`)
    break
  }
}

phase('Report')
const skipAudit = audits
const skips = results.filter(r => r.status === 'skipped')

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
  skipped: skips,
  skipAudit,
  blocked: results.filter(r => r.status === 'blocked'),
  report,
}
