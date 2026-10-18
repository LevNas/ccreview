export const meta = {
  name: 'ccreview-loop',
  description: 'Merge-time review gate: run declared gates, classify findings, auto-fix within round limits, return a verdict',
  whenToUse: 'Started by the ccreview skill, which reads REVIEW.md and .claude/review-gates.yaml and passes them as args. Not for direct use.',
  phases: [
    { title: 'Gates', detail: 'perspective / command / optional code-review gates for the round' },
    { title: 'Classify', detail: 'severity and category for findings that arrive without them' },
    { title: 'Confirm', detail: 'one read-only agent re-checks findings a reviewer marked as a repeat of a fixed one' },
    { title: 'Fix', detail: 'one agent fixes blocking findings of autofix gates and commits' },
  ],
}

// Design: docs/decisions.md. Labels in this file such as "decision 21", "spike 2" and "e2e D5" point there.
// meta.name must differ from the skill's name: plugin skills and workflows share the
// <plugin>:<name> namespace, and a workflow named 'ccreview' hid skills/ccreview (e2e session B).
// Default gates (decision 11) are four perspectives run once each; their union found all eight
// hard seeded bugs in three runs, while a single /code-review averaged six of eight.
// The script cannot read files or run commands; every input arrives through `args`
// (built by the plugin's skills/ccreview/SKILL.md):
//   { range, changedFiles, maxRounds, trust: 'trusted' | 'untrusted', diffDir,
//     gates: [{id, kind, run, effort, ref, sectionText, passes, pass, blocking, autofix}],
//     review: { severityText, categoriesText, categories, commonText },
//     commandResults: [{id, exitCode, outputTail, failures}],  // round-1 command gates, run by the skill
//     deferredGates: [{id, reason}] }  // command gates left off the fix agent's allowlist (decision 23)
//
// Decision 18: for an untrusted change the script runs no command gate (the command and its script
// come from the change itself) and never starts the fix agent (it has Bash and reads the change).
// Only the read-only reviewers run; a blocking command gate that was not run leaves the verdict
// indeterminate unless a blocking finding already fails it.
//
// Cost (measured on 2026-09-26, decision 9): every agent pays ~50k tokens of startup context, of which
// ~29k is the injected CLAUDE.md / rules. Agents therefore run as plugin types with omitClaudeMd,
// and command gates spawn no agent at all: round 1 comes from the skill's pre-flight, later rounds
// from the fix agent, which re-runs them after committing.
//
// Decisions 21, 24, 25: the fix agent is the plugin type ccreview-fixer, whose Bash and edits a
// PreToolUse hook holds to a per-run allowlist (allow or deny, never a prompt that would stall the
// background run). A command gate the fix agent may not run (it matches a settings ask rule) or did
// not report is "deferred": it does not stop the loop, and a would-be pass becomes indeterminate
// until the skill runs the gate in the main session. No other agent here has Bash.

const A = args || {}
if (!Array.isArray(A.gates) || !A.range || !A.review) {
  throw new Error('ccreview: args.range, args.gates and args.review are required (start it via the ccreview skill)')
}
// No default: a caller that forgets to decide must not get the trusted behaviour.
if (A.trust !== 'trusted' && A.trust !== 'untrusted') {
  throw new Error("ccreview: args.trust must be 'trusted' or 'untrusted' (the ccreview skill decides it in step 1)")
}
const UNTRUSTED = A.trust === 'untrusted'
const MAX_ROUNDS = A.maxRounds || 3
// Typed agents inherit the session model (Opus in the second e2e run), not
// CLAUDE_CODE_SUBAGENT_MODEL like default workflow agents, so pin the leaf model explicitly.
const LEAF_MODEL = A.leafModel || 'sonnet'
// Decision 16: reviewers and the classifier run as the plugin's agents/ccreview-reviewer.md (Read, Grep,
// Glob only; omitClaudeMd). The prompt-level ban on running code failed in the fourth e2e run.
// If the type is missing the agent call fails and the gate is indeterminate: never fall back to
// a type with Bash. Plugin types are namespaced by the plugin name (decision-21 spike).
const REVIEWER = { agentType: A.reviewerType || 'ccreview:ccreview-reviewer', model: LEAF_MODEL }
// Decision 21: without this type the hook cannot tell the fix agent apart and would not hold it.
const FIXER = { agentType: A.fixerType || 'ccreview:ccreview-fixer', model: LEAF_MODEL }
// Reviewers cannot run git, so git writes the diff to a file they Read: the skill writes
// diff-r1.patch before the workflow starts, and the fix agent of round N writes diff-r<N+1>.patch
// after its commit (`git diff <range> --output=...`). The diff used to travel as text in the agents'
// JSON, and in e2e session B the fix agent's copy came back double-escaped, so rounds 2 and 3 reviewed
// a broken diff. No model copies the diff now; verify_fixes.py compares every file with git.
// The skill passes the directory as an absolute path inside the worktree (the agents' cwd may differ,
// and Read wants an absolute path); it stays untracked and the skill removes it after the run.
const DIFF_DIR = (A.diffDir || '.ccreview').replace(/\/+$/, '')
const diffFile = round => `${DIFF_DIR}/diff-r${round}.patch`
let currentDiffFile = diffFile(1)
let staleNote = null // set when a fix agent did not write the next round's diff
// The range grows with every fix commit, so files a fix agent touched stay reviewable.
const changed = [...A.changedFiles]
const CATEGORIES = A.review.categories
const SEVERITIES = ['blocking', 'nit']

const FINDING = {
  type: 'object',
  required: ['path', 'summary'],
  properties: {
    path: { type: 'string' },
    line: { type: ['integer', 'null'], description: 'Line number in the file itself, never a line number inside a diff file; null if there is none' },
    summary: { type: 'string' },
    failure_scenario: { type: ['string', 'null'] },
  },
}
const REVIEW_SCHEMA = {
  type: 'object',
  required: ['textReturned', 'findings'],
  properties: {
    textReturned: { type: 'boolean', description: 'true only if the code-review skill returned review text to you' },
    findings: { type: 'array', items: FINDING },
  },
}
// Perspective reviewers grade their own findings (decision 12): the classify agent is only
// needed for findings that arrive without a severity and a category.
// Decision 33: they also say where a problem comes from. origin is required because agents leave out
// keys that are not (e2e D4: several outputs had no repeat_of).
const ORIGINS = ['introduced', 'exposed', 'pre-existing']
const GRADED_FINDING = {
  type: 'object',
  required: [...FINDING.required, 'severity', 'category', 'origin'],
  properties: {
    ...FINDING.properties,
    severity: { type: 'string', enum: SEVERITIES },
    category: { type: 'string', enum: CATEGORIES },
    origin: { type: 'string', enum: ORIGINS, description: 'pre-existing only if reverting the change would leave the problem exactly as it is' },
    repeat_of: { type: ['integer', 'null'], description: 'Number of the previously fixed finding this one repeats, or null' },
    evidence: { type: ['string', 'null'], description: 'With repeat_of: the current file text that shows the problem, copied verbatim; otherwise null' },
  },
}
const PERSPECTIVE_SCHEMA = {
  type: 'object',
  required: ['findings'],
  properties: { findings: { type: 'array', items: GRADED_FINDING } },
}
const COMMAND_SCHEMA = {
  type: 'object',
  required: ['exitCode', 'outputTail', 'failures'],
  properties: {
    exitCode: { type: 'integer' },
    outputTail: { type: 'string' },
    failures: { type: 'array', items: FINDING, description: 'Per-file failures parsed from the output; empty if none or unparseable' },
  },
}
const CLASSIFY_SCHEMA = {
  type: 'object',
  required: ['items'],
  properties: {
    items: {
      type: 'array',
      items: {
        type: 'object',
        required: ['index', 'severity', 'category'],
        properties: {
          index: { type: 'integer' },
          severity: { type: 'string', enum: SEVERITIES },
          category: { type: 'string', enum: CATEGORIES },
        },
      },
    },
  },
}
const COMMAND_RESULT = {
  type: 'object',
  required: ['id', ...COMMAND_SCHEMA.required],
  properties: { id: { type: 'string' }, ...COMMAND_SCHEMA.properties },
}
const FIX_SCHEMA = {
  type: 'object',
  required: ['committed', 'files', 'fixed', 'skipped', 'commandResults'],
  properties: {
    committed: { type: 'boolean' },
    fixed: { type: 'array', items: { type: 'integer' }, description: 'Numbers of the findings you fixed' },
    diffWritten: { type: 'boolean', description: 'true if you ran the git diff --output command after your commit' },
    sha: { type: ['string', 'null'] },
    files: { type: 'array', items: { type: 'string' } },
    skipped: { type: 'array', items: { type: 'string' }, description: 'Findings you could not fix, with the reason' },
    commandResults: { type: 'array', items: COMMAND_RESULT, description: 'One entry per command gate, run after the commit' },
  },
}
const CONFIRM_SCHEMA = {
  type: 'object',
  required: ['items'],
  properties: {
    items: {
      type: 'array',
      items: {
        type: 'object',
        required: ['index', 'stillPresent'],
        properties: {
          index: { type: 'integer' },
          stillPresent: { type: 'boolean', description: 'true only if the problem exists in the file as it is now' },
          quote: { type: ['string', 'null'], description: 'The current lines of the file you based the answer on, copied verbatim' },
        },
      },
    },
  },
}
const COMMAND_GATES = A.gates.filter(g => g.kind === 'command')
// Gates the skill left off the fix agent's allowlist: the fix agent is not asked to run them.
const DEFERRED_BY_SKILL = new Map((A.deferredGates || []).map(d => [d.id, d.reason || 'left off the allowlist']))
const FIX_COMMAND_GATES = COMMAND_GATES.filter(g => !DEFERRED_BY_SKILL.has(g.id))

// --- gate runners ------------------------------------------------------------

async function runReview(g, round) {
  // Spikes 1-3: the skill returns its text only some of the time, independent of --fix.
  // Retry once; a second silence is "indeterminate", never "no findings" (decision 8).
  for (let attempt = 1; attempt <= 2; attempt++) {
    const out = await agent([
      `ccreview round ${round}, gate "${g.id}", attempt ${attempt}.`,
      `Call the Skill tool with skill "code-review" and args "${g.effort || 'medium'} ${A.range}".`,
      'Never pass --fix, --comment, --post or ultra. Do not edit any file.',
      'Set textReturned=true only if the skill returned review text to you. If it returned only a completion notice, set textReturned=false and findings=[].',
      'List ONLY findings that appear in the returned text, copying path, line, summary and failure_scenario. Never add findings of your own.',
      'If the text says there is nothing to report, set textReturned=true and findings=[].',
    ].join('\n'), { schema: REVIEW_SCHEMA, effort: 'low', phase: 'Gates', label: `r${round}:${g.id}#${attempt}` })
    if (out && out.textReturned) return { gate: g, status: 'ok', findings: out.findings }
  }
  return { gate: g, status: 'indeterminate', findings: [] }
}

// Command outputs for the next round, keyed by gate id: the skill's pre-flight for round 1,
// the fix agent's post-commit run afterwards.
let commandOutputs = new Map((A.commandResults || []).map(r => [r.id, r]))

function commandVerdict(g, out) {
  if (!out) return { gate: g, status: 'indeterminate', findings: [] }
  if (out.exitCode === 0) return { gate: g, status: 'ok', findings: [] }
  // Pre-flight results come from the skill and skip schema validation; tolerate a missing list.
  const failures = out.failures || []
  const findings = failures.length ? failures
    : [{ path: '*', line: null, summary: out.outputTail.slice(-500), failure_scenario: null }]
  return { gate: g, status: 'failed', exitCode: out.exitCode, findings }
}

// Command gates skipped for an untrusted change (decision 18); reported, never counted as passed.
const notRun = []
// Decision 25: command gates without a result for the current state of the branch, keyed by id. The
// latest round wins: a gate reported again in a later round leaves the list.
const deferred = new Map()

async function runCommand(g, round) {
  if (UNTRUSTED) {
    if (!notRun.includes(g.id)) notRun.push(g.id)
    return { gate: g, status: g.blocking ? 'indeterminate' : 'skipped', findings: [] }
  }
  if (commandOutputs.has(g.id)) {
    deferred.delete(g.id)
    return commandVerdict(g, commandOutputs.get(g.id))
  }
  // No agent runs it here: an agent with unrestricted Bash is what decision 21 removes, and a gate
  // that matches an ask rule would stall any agent on a prompt. The skill runs it in the main session.
  const reason = round === 1 ? 'no pre-flight result from the skill'
    : DEFERRED_BY_SKILL.get(g.id) || 'the fix agent did not report it'
  deferred.set(g.id, { id: g.id, round, blocking: !!g.blocking, reason })
  log(`round ${round}: command gate "${g.id}" deferred to the main session (${reason})`)
  return { gate: g, status: 'deferred', findings: [] }
}

// Each pass is an independent review; findings are unioned (decision 11, passes defaults to 1).
async function runPerspective(g, round) {
  const passes = Math.max(1, g.passes || 1)
  const outs = await parallel(Array.from({ length: passes }, (_, i) => () => agent([
    `ccreview round ${round}, gate "${g.id}", pass ${i + 1} — review through this perspective from REVIEW.md:`,
    '',
    g.sectionText,
    '',
    '## Rules for every perspective', A.review.commonText || '',
    '',
    // e2e D7 and D8: the one reviewer that tagged base lines "introduced" had not read the diff, and the
    // diff instruction came after the one to read the changed files (decision 40)
    `## Diff (range ${A.range})`,
    `Read \`${currentDiffFile}\` before any other file, in parts if it is long: git wrote it with \`git diff ${A.range}\`. It is not part of the change; do not review it.`,
    'Only if that file does not exist, review from the changed files alone.',
    // e2e D2: three of four perspectives gave diff-file line numbers (client.py:65 for line 11).
    'A finding\'s line is the line number in the file itself, not in the diff file: after you find a problem in the diff, Read that file and take the line number Read shows (null if the file or the line no longer exists, e.g. a removed line).',
    ...(staleNote ? [staleNote] : []),
    '',
    `Changed files (range ${A.range}): ${changed.join(' ')}`,
    'After the diff, Read the changed files and the files they reference or link to. You have no shell: judge from the text only.',
    '',
    '## Severity definitions', A.review.severityText,
    '', '## Category vocabulary', A.review.categoriesText,
    '', '## Origin of each finding',
    `Set origin on every finding by one question: if the change (everything in the diff of ${A.range}) were reverted, would this problem still be there exactly as it is?`,
    '- yes: origin="pre-existing" (the base already had it, e.g. an old mismatch in a file the change also touches). It is reported to the human but neither judged nor fixed.',
    '- no, the change added it: origin="introduced". When there is a diff file, set it only after you have found in it the + or - line that causes the problem.',
    '- no, the base was right and the change made it wrong (e.g. text in an unchanged line that still names something the change renamed): origin="exposed".',
    'If you are not sure the answer is yes, do not use "pre-existing".',
    ...(claimedFixed.length ? [
      '', '## Findings reported as fixed in the previous round',
      ...claimedFixed.map((f, n) => `${n + 1}. [${f.category}] ${f.path}:${f.line ?? '-'} — ${f.summary}`),
      '',
      'If a finding of yours is the same problem as one of these (the fix did not hold), set repeat_of to its number.',
      'A different problem in the same file or category is not a repeat: set repeat_of to null.',
      'When you set repeat_of, copy into evidence the lines of the file as you read them now that show the problem, verbatim.',
      'Judge from the file you read, not from the wording above: that list describes the file before the fix.',
    ] : []),
    '',
    'Give every finding exactly one severity and one category from the lists. Return findings=[] if there is nothing.',
  ].join('\n'), { schema: PERSPECTIVE_SCHEMA, effort: 'medium', phase: 'Gates', label: `r${round}:${g.id}#${i + 1}`, ...REVIEWER })))
  const got = outs.filter(Boolean)
  if (!got.length) return { gate: g, status: 'indeterminate', findings: [] }
  return { gate: g, status: 'ok', findings: got.flatMap(o => o.findings) }
}

const RUNNERS = { review: runReview, command: runCommand, perspective: runPerspective }

// --- classification (decision 7) --------------------------------------------

// The worktree root, from the absolute diffDir the skill passes (<toplevel>/.ccreview); null when the
// diffDir is relative.
const WORKTREE_ROOT = /^\/.+\/\.ccreview$/.test(DIFF_DIR) ? DIFF_DIR.slice(0, -'/.ccreview'.length) : null

// Gates report paths from different roots (a lint over a docs tree may print paths relative to that tree, code-review
// repo-relative ones, reviewers absolute ones). Map a path to the changed file it names, so the
// same file yields the same key across gates and rounds (found in the first e2e run). An unmatched
// path is out of the range, so a miss here drops a finding (e2e D1: seven blocking findings with
// worktree-absolute paths); a wrong match only keeps one in.
function normalizePath(p) {
  let clean = String(p || '').replace(/^\.\//, '')
  if (WORKTREE_ROOT && clean.startsWith(`${WORKTREE_ROOT}/`)) clean = clean.slice(WORKTREE_ROOT.length + 1)
  if (changed.includes(clean)) return clean
  // git's a/ and b/ prefixes, as in the diff files the reviewers read (decision 26; this step is decision 28, item 2)
  const unprefixed = clean.replace(/^[ab]\//, '')
  if (changed.includes(unprefixed)) return unprefixed
  // A shorter path (relative to a docs tree): the changed file it is a '/'-bounded suffix of.
  const shorter = changed.filter(c => c.endsWith(`/${clean}`))
  if (shorter.length === 1) return shorter[0]
  // An absolute path from another root (the main checkout, a relative diffDir): the longest changed
  // file it ends with. Not for relative paths, where docs/x.md and x.md are different files.
  if (clean.startsWith('/')) {
    const longer = changed.filter(c => clean.endsWith(`/${c}`)).sort((x, y) => y.length - x.length)
    if (longer.length) return longer[0]
  }
  return clean
}

// The built-in /code-review reviewed files outside the given range in the second e2e run, and the
// fix agent then edited them. Findings on files outside changedFiles never reach the verdict or the
// fix agent; they are only reported. Command gates are exempt: a failing command fails the gate
// whatever path it names, and dropping its findings would turn that failure into a pass.
const outOfRange = []
function inRange(gate, f) {
  if (gate.kind === 'command' || changed.includes(f.path)) return true
  outOfRange.push({ gate: gate.id, ...f })
  return false
}

// Decision 33: in e2e D4 old problems in files the change touched entered the verdict (a README
// mismatch failed round 3) and the fix (round 2 rewrote code nobody asked about). Only problems the
// change introduced or made wrong are judged. A reviewer says which by origin; a finding is left out
// only when origin is exactly 'pre-existing', so a missing or unknown value is judged. Left-out
// findings are reported, and verify_fixes.py checks them against the lines the change touched.
const preExisting = []
// Decision 36 (open point I): the other direction. In e2e D7 a reviewer that had not read the diff called
// base problems introduced, and they were judged. Introduced findings stay judged (decision 33 fails
// toward judging); they are returned with their round so verify_fixes.py can report those on lines
// the change did not touch.
const introduced = []
function judged(gate, f, round) {
  if (gate.kind !== 'perspective') return true
  // round and gate come last: a reviewer's output must not overwrite the values verify_fixes.py uses.
  if (f.origin === 'introduced') introduced.push({ ...f, round, gate: gate.id })
  if (f.origin !== 'pre-existing') return true
  preExisting.push({ ...f, round, gate: gate.id })
  return false
}

const isGraded = f => SEVERITIES.includes(f.severity) && CATEGORIES.includes(f.category)

async function classify(results, round) {
  const all = results.flatMap(r => r.findings
    .map(f => ({ gate: r.gate, f: { ...f, path: normalizePath(f.path) } }))
    .filter(x => inRange(x.gate, x.f) && judged(x.gate, x.f, round)))
  if (preExisting.length) log(`round ${round}: ${preExisting.length} pre-existing finding(s) so far, not judged`)
  if (outOfRange.length) log(`round ${round}: ${outOfRange.length} finding(s) outside the range so far, excluded`)
  // Command findings are always re-graded: a failing command is blocking by definition below.
  const graded = all.filter(x => x.gate.kind !== 'command' && isGraded(x.f))
    .map(x => ({ gate: x.gate.id, ...x.f }))
  const flat = all.filter(x => x.gate.kind === 'command' || !isGraded(x.f))
  if (!flat.length) return graded
  const listing = flat.map((x, i) => `${i}. [${x.gate.id}] ${x.f.path}:${x.f.line ?? '-'} — ${x.f.summary}`).join('\n')
  const out = await agent([
    `ccreview round ${round}: assign a severity and a category to every finding below, following REVIEW.md. Do not call any tool.`,
    '',
    '## Severity definitions', A.review.severityText,
    '', '## Category vocabulary', A.review.categoriesText,
    '', '## Findings', listing,
    '', 'Return one item per finding index. Use only the allowed values.',
  ].join('\n'), { schema: CLASSIFY_SCHEMA, effort: 'low', phase: 'Classify', label: `r${round}:classify`, ...REVIEWER })
    .catch(() => null) // a missing reviewer type leaves findings unclassified, which fail safe to blocking
  const byIndex = new Map((out ? out.items : []).map(it => [it.index, it]))
  return graded.concat(flat.map((x, i) => {
    const c = byIndex.get(i)
    // A failing command gate is blocking by definition; unclassified findings fail safe to blocking.
    const severity = x.gate.kind === 'command' ? 'blocking' : (c ? c.severity : 'blocking')
    return { gate: x.gate.id, ...x.f, severity, category: c ? c.category : 'correctness' }
  }))
}

// --- verdict helpers (decision 6) --------------------------------------------

function gateFails(g, result, findings) {
  const own = findings.filter(f => f.gate === g.id)
  if (g.pass === 'exit0') return result.status === 'failed'
  if (g.pass === 'no-blocking') return own.some(f => f.severity === 'blocking')
  const m = /^max-findings:(\d+)$/.exec(g.pass || '')
  if (m) return own.length > Number(m[1])
  throw new Error(`ccreview: unknown pass rule "${g.pass}" on gate ${g.id}`)
}

const keyOf = f => `${f.gate}|${f.path}|${f.category}` // no line numbers: fixes move lines

// Decision 15: a key collides whenever one file has two problems of the same category (the fourth
// e2e run stopped on such a false "same reason twice"). Perspective reviewers therefore say which
// previously fixed finding a finding repeats; command and review findings still match by key.
let claimedFixed = []
let touched = []
// The fix agent in the fifth e2e run staged a session capture that a hook had written to the
// working tree. Files a fix commits beyond the range and the fixed findings are reported, not judged:
// a fix may rightly edit a document the change made stale.
const unexpectedFiles = []
// Decision 35 (open point J): a number in range was enough, so a reviewer who put a fixed finding's
// number on a problem in another file stopped the loop with "fix did not hold" on a file nobody
// tried to fix. The finding the number points at must be in the same file. Only the path is
// compared: the fixed list is shared by every perspective (so not the gate), each reviewer grades
// its own category, and fixes move lines. A mismatch is a new finding, reported in repeatMismatches.
const repeatMismatches = []
function claimedRepeat(f) {
  const g = A.gates.find(x => x.id === f.gate)
  if (!g || g.kind !== 'perspective') return null
  if (!(Number.isInteger(f.repeat_of) && f.repeat_of >= 1 && f.repeat_of <= claimedFixed.length)) return null
  return claimedFixed[f.repeat_of - 1]
}
function isRepeat(f) {
  const g = A.gates.find(x => x.id === f.gate)
  if (g && g.kind === 'perspective') return claimedRepeat(f)?.path === f.path
  return claimedFixed.some(c => keyOf(c) === keyOf(f))
}

// Decision 22: in the sixth e2e run a reviewer read the fixed knowledge-base entry and still reported the old
// link with repeat_of, led by the wording of the fixed list. One reviewer's repeat_of must not stop
// the loop on its own. Before stopping, a fresh read-only agent that never sees the fixed list
// re-checks each claimed repeat against the current file. A repeat it finds absent is "disputed":
// dropped from the verdict and the fix, and reported for the human who approves the merge. No
// answer for a finding keeps it, so a missing confirmer stops as before.
const disputed = []
// Decision 37 (D7 observation 2): only "absent" answers used to leave a trace, so a stop the
// confirmer checked read the same as one it never answered. Every claim sent to the confirmer is
// reported with its answer: present, absent or none (no answer for that number).
const confirmations = []
async function confirmRepeats(failing, round) {
  const claims = failing.filter(f => A.gates.find(x => x.id === f.gate)?.kind === 'perspective' && isRepeat(f))
  if (!claims.length) return new Set()
  const out = await agent([
    `ccreview round ${round}: check each problem below against the file as it is now.`,
    'Another reviewer says each problem is present in the current file. The description and the quote may',
    'describe an older version of the file: do not take them as true. Read the file now, and Grep for the quoted text.',
    'Set stillPresent=true only if the problem exists in the current text. In quote, copy the current lines you',
    'based the answer on, verbatim. You have no shell: judge from the text only.',
    '',
    '## Problems',
    ...claims.map((f, i) => [
      `${i + 1}. [${f.category}] ${f.path}:${f.line ?? '-'} — ${f.summary}`,
      ...(f.evidence ? [`   quoted by the reviewer as the current text: ${f.evidence}`] : []),
    ].join('\n')),
    '',
    'Return one item per problem number.',
  ].join('\n'), { schema: CONFIRM_SCHEMA, effort: 'medium', phase: 'Confirm', label: `r${round}:confirm`, ...REVIEWER })
    .catch(() => null)
  const byIndex = new Map((out ? out.items : []).map(it => [it.index, it]))
  const dropped = new Set()
  claims.forEach((f, i) => {
    const v = byIndex.get(i + 1)
    const answer = !v ? 'none' : v.stillPresent === false ? 'absent' : 'present'
    confirmations.push({ round, gate: f.gate, path: f.path, line: f.line ?? null, summary: f.summary, repeat_of: f.repeat_of, answer, quote: v?.quote ?? null })
    if (answer !== 'absent') return
    dropped.add(f)
    disputed.push({ round, ...f, confirmerQuote: v.quote ?? null })
  })
  if (dropped.size) log(`round ${round}: ${dropped.size} claimed repeat(s) not found in the current files, disputed`)
  return dropped
}

// --- loop --------------------------------------------------------------------

const rounds = []
let verdict = null
let stopReason = null

for (let round = 1; round <= MAX_ROUNDS && !verdict; round++) {
  const results = (await parallel(A.gates.map(g => () => RUNNERS[g.kind](g, round))))
    .map((r, i) => r || { gate: A.gates[i], status: 'indeterminate', findings: [] })
  const findings = await classify(results, round)

  const failingGates = A.gates.filter((g, i) => g.blocking && gateFails(g, results[i], findings))
  const indeterminate = results.filter(r => r.status === 'indeterminate').map(r => r.gate.id)
  const blockingNow = findings.filter(f => failingGates.some(g => g.id === f.gate) && f.severity === 'blocking')
  const mismatched = blockingNow.filter(f => claimedRepeat(f) && !isRepeat(f))
  for (const f of mismatched) {
    repeatMismatches.push({ round, gate: f.gate, path: f.path, line: f.line ?? null, summary: f.summary, repeat_of: f.repeat_of, claimedPath: claimedRepeat(f).path })
  }
  if (mismatched.length) log(`round ${round}: ${mismatched.length} repeat_of number(s) point at a fixed finding in another file, taken as new findings`)
  // Only after a fix, and only when the round can still be decided (decision 22).
  const dropped = claimedFixed.length && !indeterminate.length ? await confirmRepeats(blockingNow, round) : new Set()
  const failing = blockingNow.filter(f => !dropped.has(f))
  const keys = [...new Set(failing.map(keyOf))].sort()
  rounds.push({ round, keys, indeterminate, findings: findings.filter(f => !dropped.has(f)) })
  log(`round ${round}: failing keys=${keys.length}, findings=${findings.length}, indeterminate=${indeterminate.join(',') || 'none'}`)

  // A blocking finding fails the change whatever the gates that did not run would have said.
  if (UNTRUSTED && keys.length) { verdict = 'fail'; stopReason = 'blocking findings; auto-fix and command gates are off for an untrusted change'; break }
  if (indeterminate.length) {
    const skipped = indeterminate.filter(id => notRun.includes(id))
    const silent = indeterminate.filter(id => !notRun.includes(id))
    verdict = 'indeterminate'
    stopReason = [
      silent.length ? `no result from gate(s): ${silent.join(', ')}` : null,
      skipped.length ? `command gate(s) not run for an untrusted change: ${skipped.join(', ')}` : null,
    ].filter(Boolean).join('; ')
    break
  }
  if (!keys.length) { verdict = 'pass'; break }
  if (claimedFixed.length) {
    const repeats = failing.filter(isRepeat)
    const repeated = [...new Set(repeats.map(keyOf))].sort()
    if (repeated.length) {
      verdict = 'fail'
      // Decision 37: name the fixed finding each perspective repeat points at, and say how many
      // the confirmer found still present and how many it left unanswered.
      const keyText = k => {
        const nos = [...new Set(repeats.filter(f => keyOf(f) === k && claimedRepeat(f)).map(f => f.repeat_of))].sort((a, b) => a - b)
        return nos.length ? `${k} (repeat of ${nos.map(n => `#${n}`).join(', ')})` : k
      }
      const count = answer => confirmations.filter(c => c.round === round && c.answer === answer).length
      const [present, none] = [count('present'), count('none')]
      const confirmer = present + none ? `; confirmer: ${present} still present, ${none} no answer` : ''
      stopReason = `same reason failed twice (fix did not hold): ${repeated.map(keyText).join(', ')}${confirmer}`
      break
    }
    // New failures only count as churn in files the fix touched; late finds elsewhere are progress.
    const churn = [...new Set(failing.filter(f => touched.includes(f.path)).map(keyOf))]
    if (churn.length >= claimedFixed.length) { verdict = 'fail'; stopReason = `fixes introduced as many new failures as they removed: ${churn.sort().join(', ')}`; break }
  }
  if (round === MAX_ROUNDS) { verdict = 'fail'; stopReason = `reached max rounds (${MAX_ROUNDS})`; break }

  const fixable = failing.filter(f => A.gates.find(g => g.id === f.gate).autofix === 'agent')
  if (!fixable.length) { verdict = 'fail'; stopReason = 'blocking findings on gates without autofix'; break }

  const fix = await agent([
    `ccreview round ${round}: fix exactly these blocking findings on the current branch, nothing else.`,
    ...fixable.map((f, i) => `${i + 1}. [${f.gate}/${f.category}] ${f.path}:${f.line ?? '-'} — ${f.summary}${f.failure_scenario ? ` (scenario: ${f.failure_scenario})` : ''}`),
    '',
    'Change only what each finding requires. Do not add input validation, error handling, refactors or other behavior that no finding asks for (the first e2e run added an unrequested ValueError guard).',
    `You may edit only files already changed in the range: ${changed.join(' ')}. Do not push.`,
    'Stage only the files you edited, by explicit path (git add -- <path>...). Other modified files in git status, such as session captures written by hooks, are not yours: leave them unstaged.',
    'Commit with the repository settings as they are: do not pass --no-gpg-sign or --no-verify.',
    `Commit with exactly: git commit -m "ccreview: round ${round} fixes". Report the commit sha, changed files, the numbers of the findings you fixed, and any finding you could not fix.`,
    'Run one command per Bash call, as written here; a hook denies anything else, and a denied call must not be retried.',
    '',
    `After the commit, run \`git diff ${A.range} --output=${diffFile(round + 1)}\` exactly as written, and set diffWritten. The next reviewers read that file (they cannot run git). Do not stage it.`,
    ...(FIX_COMMAND_GATES.length ? [
      '',
      'After the commit, run each command below from the repository root and report it in commandResults',
      '(id, exit code, last ~40 lines of output, per-file failures). Do not change any file after running them;',
      'a failure here is reported to the next round, not fixed by you.',
      ...FIX_COMMAND_GATES.map(g => `- ${g.id}: ${g.run}`),
    ] : []),
  ].join('\n'), { schema: FIX_SCHEMA, effort: 'medium', phase: 'Fix', label: `r${round}:fix`, ...FIXER })
  rounds[rounds.length - 1].fix = fix
  if (!fix || !fix.committed) { verdict = 'fail'; stopReason = 'fix agent made no commit'; break }
  commandOutputs = new Map((fix.commandResults || []).map(r => [r.id, r]))
  const expected = new Set([...changed, ...fixable.map(f => f.path)])
  for (const f of (fix.files || []).map(normalizePath)) if (!expected.has(f)) unexpectedFiles.push({ round, path: f })
  if (unexpectedFiles.length) log(`round ${round}: fix committed ${unexpectedFiles.length} file(s) outside the range and findings so far`)
  for (const f of (fix.files || []).map(normalizePath)) if (!changed.includes(f)) changed.push(f)
  // An explicit empty list means nothing was fixed; only a missing list defaults to "all".
  const fixedNos = Array.isArray(fix.fixed) ? fix.fixed : fixable.map((_, i) => i + 1)
  claimedFixed = fixedNos.filter(n => n >= 1 && n <= fixable.length).map(n => fixable[n - 1])
  // Decision 34: what the fix agent says it fixed, with each reviewer's path and line (as read before the
  // fix), so the skill need not take them from a diff file (e2e D5). A claim, not a check: the next
  // round's reviewers and verify_fixes.py are the check.
  rounds[rounds.length - 1].fixedFindings = claimedFixed.map(f => ({ gate: f.gate, path: f.path, line: f.line ?? null, category: f.category, summary: f.summary }))
  if (!claimedFixed.length) { verdict = 'fail'; stopReason = 'fix agent reported no fixed finding'; break }
  touched = (fix.files || []).map(normalizePath)
  if (fix.diffWritten) {
    currentDiffFile = diffFile(round + 1)
    staleNote = null
  } else {
    staleNote = `That diff predates the round ${round} fixes: for files the fixes changed (${touched.join(' ') || 'unknown'}), the changed file itself, which you read after the diff, is the current text.`
  }
}

// Decision 25: a pass is not a pass while a blocking command gate has no result for the final state.
// Returning indeterminate keeps a skill that forgets to run the gate from reporting a pass.
const deferredList = [...deferred.values()]
const blockingDeferred = deferredList.filter(d => d.blocking).map(d => d.id)
if (verdict === 'pass' && blockingDeferred.length) {
  verdict = 'indeterminate'
  stopReason = `command gate(s) deferred to the main session: ${blockingDeferred.join(', ')}`
}

const last = rounds[rounds.length - 1]
return {
  verdict,
  stopReason,
  trust: A.trust,
  notRun,
  deferred: deferredList,
  rounds: rounds.map(r => ({ round: r.round, keys: r.keys, indeterminate: r.indeterminate, fix: r.fix || null, fixedFindings: r.fixedFindings || [] })),
  remaining: last ? last.findings : [],
  outOfRange,
  // Out-of-range findings that a blocking gate graded blocking. They never change the verdict
  // (decision 10; this count is decision 28, item 3); the skill puts the count at the top of the comment draft so a path the
  // normalization missed cannot pass silently. A missed path repeats every round, so a finding is
  // counted once per gate, path and summary. /code-review findings leave the range before the
  // classify agent grades them, so they are never counted.
  outOfRangeBlocking: new Set(outOfRange
    .filter(f => f.severity === 'blocking' && A.gates.some(g => g.id === f.gate && g.blocking))
    .map(f => `${f.gate}|${f.path}|${f.summary}`)).size,
  // Decision 33: findings the reviewers called pre-existing, with the round they came from (the
  // skill's verify_fixes.py reads it to pick the file state). Never in the verdict or the fix; the
  // count of blocking ones of blocking gates is taken the same way as outOfRangeBlocking.
  preExisting,
  preExistingBlocking: new Set(preExisting
    .filter(f => f.severity === 'blocking' && A.gates.some(g => g.id === f.gate && g.blocking))
    .map(f => `${f.gate}|${f.path}|${f.summary}`)).size,
  // Decision 36: judged findings the reviewers called introduced, with their round, for verify_fixes.py.
  introduced,
  unexpectedFiles,
  disputed,
  confirmations,
  repeatMismatches,
}
