// Simulation test for workflows/ccreview-loop.js: replaces agent() with scripted
// responses and checks the verdict / stop reason of the round loop.
// Run from the plugin root: node tests/loop-sim.mjs
import { readFileSync } from 'node:fs'
import assert from 'node:assert/strict'

const src = readFileSync(new URL('../workflows/ccreview-loop.js', import.meta.url), 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
const script = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'log', 'phase', src)

const baseArgs = {
  range: 'origin/main...HEAD',
  changedFiles: ['a.py', '.claude/knowledge/entries/x.md'],
  maxRounds: 3,
  trust: 'trusted',
  review: { severityText: 's', categoriesText: 'c', categories: ['correctness', 'links', 'cleanup'] },
  gates: [
    { id: 'code-review', kind: 'review', effort: 'medium', pass: 'no-blocking', blocking: true, autofix: 'agent' },
    { id: 'kb-lint', kind: 'command', run: 'lint x.md', pass: 'exit0', blocking: true, autofix: 'agent' },
  ],
  commandResults: [{ id: 'kb-lint', exitCode: 0, outputTail: '0 finding(s)', failures: [] }],
}

// responder(label, prompt) -> value; labels look like "r1:code-review#1", "r1:classify", "r1:fix"
async function run(responder, args = baseArgs) {
  const calls = []
  const types = {}
  const models = {}
  const schemas = {}
  const agent = async (prompt, opts) => {
    calls.push(opts.label)
    types[opts.label] = opts.agentType || 'default'
    models[opts.label] = opts.model || 'inherit'
    schemas[opts.label] = opts.schema
    return responder(opts.label, prompt)
  }
  const parallel = thunks => Promise.all(thunks.map(t => t().catch(() => null)))
  const result = await script(args, agent, parallel, null, () => {}, () => {})
  return { result, calls, types, models, schemas }
}

const bug = { path: 'a.py', line: 4, summary: 'off-by-one', failure_scenario: null }
const lintOk = { exitCode: 0, outputTail: '0 finding(s)', failures: [] }
const lintFixed = [{ id: 'kb-lint', ...lintOk }] // the fix agent's post-commit report
const lintBroken = [{ id: 'kb-lint', exitCode: 1, outputTail: 'broken link', failures: [{ path: 'x.md', line: 3, summary: 'broken link' }] }]
const classifyAll = (severity, category) => (_, prompt) => ({
  items: [...prompt.matchAll(/^(\d+)\. /gm)].map(m => ({ index: Number(m[1]), severity, category })),
})

// 1. clean on round 1
{
  const { result, calls } = await run(label =>
    label.includes('code-review') ? { textReturned: true, findings: [] } : lintOk)
  assert.equal(result.verdict, 'pass')
  assert.equal(result.rounds.length, 1)
  assert.ok(!calls.some(l => l.endsWith(':classify')), 'no classify call without findings')
}

// 2. fail -> fix -> pass
{
  const { result, calls } = await run((label, prompt) => {
    if (label === 'r1:code-review#1') return { textReturned: true, findings: [bug] }
    if (label.startsWith('r2:code-review')) return { textReturned: true, findings: [] }
    if (label.includes('kb-lint')) return lintOk
    if (label.endsWith(':classify')) return classifyAll('blocking', 'correctness')(label, prompt)
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], skipped: [], commandResults: lintFixed }
    throw new Error(`unexpected ${label}`)
  })
  assert.equal(result.verdict, 'pass')
  assert.deepEqual(result.rounds[0].keys, ['code-review|a.py|correctness'])
  assert.ok(calls.includes('r1:fix'))
}

// 3. review text never returned -> retried once -> indeterminate (never "pass")
{
  const { result, calls } = await run(label =>
    label.includes('code-review') ? { textReturned: false, findings: [] } : lintOk)
  assert.equal(result.verdict, 'indeterminate')
  assert.deepEqual(calls.filter(l => l.includes('code-review')), ['r1:code-review#1', 'r1:code-review#2'])
}

// 4. same key fails in two consecutive rounds -> fail
{
  const { result, calls } = await run((label, prompt) => {
    if (label.includes('code-review')) return { textReturned: true, findings: [bug] }
    if (label.includes('kb-lint')) return lintOk
    if (label.endsWith(':classify')) return classifyAll('blocking', 'correctness')(label, prompt)
    if (label.endsWith(':fix')) return { committed: true, sha: 'abc', files: ['a.py'], skipped: [] }
    throw new Error(`unexpected ${label}`)
  })
  assert.equal(result.verdict, 'fail')
  assert.match(result.stopReason, /same reason failed twice/)
  assert.equal(result.rounds.length, 2)
  assert.ok(!calls.some(l => l.endsWith(':confirm')), 'key-based repeats of review and command gates are not re-checked')
  assert.doesNotMatch(result.stopReason, /repeat of|confirmer/, 'no number and no confirmer for key-based repeats')
  assert.deepEqual(result.confirmations, [])
}

// 5. failing command gate is blocking even if the classifier says nit; nits alone pass
{
  const { result } = await run((label, prompt) => {
    if (label.includes('code-review')) return { textReturned: true, findings: [bug] }
    if (label.endsWith(':classify')) return classifyAll('nit', 'links')(label, prompt)
    if (label.endsWith(':fix')) return { committed: false, sha: null, files: [], skipped: ['x.md'] }
    throw new Error(`unexpected ${label}`)
  }, { ...baseArgs, commandResults: lintBroken })
  assert.equal(result.verdict, 'fail')
  assert.deepEqual(result.rounds[0].keys, ['kb-lint|.claude/knowledge/entries/x.md|links'], 'code-review nit does not fail; lint failure does (path normalized)')
  assert.match(result.stopReason, /no commit/)
}

// 6. new keys replace old ones but the failing set does not shrink -> fail (regression guard)
{
  const other = { path: 'b.py', line: 9, summary: 'regression', failure_scenario: null }
  const third = { path: 'c.py', line: 2, summary: 'regression', failure_scenario: null }
  const { result } = await run((label, prompt) => {
    if (label === 'r1:code-review#1') return { textReturned: true, findings: [bug] }
    if (label.startsWith('r2:code-review')) return { textReturned: true, findings: [other, third] }
    if (label.includes('kb-lint')) return lintOk
    if (label.endsWith(':classify')) return classifyAll('blocking', 'correctness')(label, prompt)
    if (label.endsWith(':fix')) return { committed: true, sha: 'abc', files: ['a.py', 'b.py', 'c.py'], skipped: [] }
    throw new Error(`unexpected ${label}`)
  })
  assert.equal(result.verdict, 'fail')
  assert.match(result.stopReason, /introduced as many new failures/, 'new failures in files the fix touched count as churn')
}

// 7. gates report the same file from different roots -> keys share the normalized path
{
  const { result } = await run((label, prompt) => {
    if (label.includes('code-review')) return { textReturned: true, findings: [{ path: '.claude/knowledge/entries/x.md', line: 3, summary: 'broken link', failure_scenario: null }] }
    if (label.endsWith(':classify')) return classifyAll('blocking', 'links')(label, prompt)
    if (label.endsWith(':fix')) return { committed: false, sha: null, files: [], skipped: [] }
    throw new Error(`unexpected ${label}`)
  }, { ...baseArgs, commandResults: lintBroken })
  assert.deepEqual(result.rounds[0].keys, [
    'code-review|.claude/knowledge/entries/x.md|links',
    'kb-lint|.claude/knowledge/entries/x.md|links',
  ])
}

// 8. command gates reported by the skill's pre-flight (round 1) and the fix agent (round 2)
//    spawn no agent; reviewers and the classifier run as the read-only plugin type, the fix agent as the
//    hook-held plugin type (decisions 16, 21), and only the code-review wrapper keeps the default type
{
  const lintFail = { id: 'kb-lint', exitCode: 1, outputTail: 'broken link', failures: [{ path: 'x.md', line: 3, summary: 'broken link' }] }
  const args = {
    ...baseArgs,
    gates: [...baseArgs.gates, { id: 'review-scope', kind: 'perspective', sectionText: 'scope', pass: 'no-blocking', blocking: false, autofix: 'none' }],
    commandResults: [lintFail],
  }
  let fixPrompt = ''
  const { result, calls, types, models } = await run((label, prompt) => {
    if (label.includes('code-review')) return { textReturned: true, findings: [] }
    if (label.includes('review-scope')) return { findings: [] }
    if (label.endsWith(':classify')) return classifyAll('blocking', 'links')(label, prompt)
    if (label === 'r1:fix') {
      fixPrompt = prompt
      return { committed: true, sha: 'abc', files: ['x.md'], skipped: [], commandResults: [{ id: 'kb-lint', ...lintOk }] }
    }
    throw new Error(`unexpected ${label}`)
  }, args)
  assert.equal(result.verdict, 'pass')
  assert.deepEqual(result.rounds[0].keys, ['kb-lint|.claude/knowledge/entries/x.md|links'])
  assert.ok(!calls.some(l => l.includes('kb-lint')), 'no agent runs a command gate')
  assert.match(fixPrompt, /- kb-lint: lint x\.md/, 'fix agent is told to re-run the command gate')
  assert.equal(types['r1:classify'], 'ccreview:ccreview-reviewer', 'the classifier needs no tools')
  assert.equal(types['r1:review-scope#1'], 'ccreview:ccreview-reviewer', 'reviewers run as the read-only type (decision 16)')
  assert.equal(types['r1:code-review#1'], 'default')
  assert.equal(types['r1:fix'], 'ccreview:ccreview-fixer', 'the hook recognises the fix agent by its type (decision 21)')
  assert.equal(models['r1:classify'], 'sonnet', 'typed agents pin the leaf model instead of inheriting the session model')
  assert.equal(models['r1:fix'], 'sonnet')
  assert.match(fixPrompt, /You may edit only files already changed in the range: a\.py \.claude\/knowledge\/entries\/x\.md/)
  assert.match(fixPrompt, /git commit -m "ccreview: round 1 fixes"/, 'the exact message the allowlist permits')
  assert.deepEqual(result.deferred, [])
  assert.equal(models['r1:code-review#1'], 'inherit')
}

// 9. fix agent omits a command result -> the gate is deferred to the main session: no agent runs it
//    (decision 25) and the would-be pass is indeterminate
{
  const { result, calls } = await run((label, prompt) => {
    if (label === 'r1:code-review#1') return { textReturned: true, findings: [bug] }
    if (label.startsWith('r2:code-review')) return { textReturned: true, findings: [] }
    if (label.endsWith(':classify')) return classifyAll('blocking', 'correctness')(label, prompt)
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  })
  assert.equal(result.verdict, 'indeterminate')
  assert.equal(result.stopReason, 'command gate(s) deferred to the main session: kb-lint')
  assert.deepEqual(result.deferred, [{ id: 'kb-lint', round: 2, blocking: true, reason: 'the fix agent did not report it' }])
  assert.ok(!calls.some(l => l.includes('kb-lint')), 'no agent runs a command gate')
  assert.deepEqual(result.rounds[1].indeterminate, [], 'a deferred gate is not an indeterminate one: the loop goes on')
}

// 10. a review finding on a file outside the range is excluded from the verdict and the fix
//     (the second e2e run fixed ccreview.js itself on a seed branch that never touched it)
{
  const outside = { path: '.claude/workflows/ccreview.js', line: 1, summary: 'unrelated', failure_scenario: null }
  let fixPrompt = ''
  const { result } = await run((label, prompt) => {
    if (label === 'r1:code-review#1') return { textReturned: true, findings: [bug, outside] }
    if (label.startsWith('r2:code-review')) return { textReturned: true, findings: [outside] }
    if (label.includes('kb-lint')) return lintOk
    if (label.endsWith(':classify')) return classifyAll('blocking', 'correctness')(label, prompt)
    if (label === 'r1:fix') { fixPrompt = prompt; return { committed: true, sha: 'abc', files: ['a.py'], skipped: [], commandResults: lintFixed } }
    throw new Error(`unexpected ${label}`)
  })
  assert.equal(result.verdict, 'pass')
  assert.deepEqual(result.rounds[0].keys, ['code-review|a.py|correctness'])
  assert.doesNotMatch(fixPrompt, /ccreview\.js/, 'fix agent never sees the out-of-range finding')
  assert.equal(result.outOfRange.length, 2)
}

// 11. a failing command gate stays failing even if its path is outside the range,
//     and a pre-flight result without a failures list does not crash the loop
{
  const { result } = await run((label, prompt) => {
    if (label.includes('code-review')) return { textReturned: true, findings: [] }
    if (label.endsWith(':classify')) return classifyAll('blocking', 'links')(label, prompt)
    if (label.endsWith(':fix')) return { committed: false, sha: null, files: [], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, { ...baseArgs, commandResults: [{ id: 'kb-lint', exitCode: 2, outputTail: 'other.md: broken' }] })
  assert.equal(result.verdict, 'fail')
  assert.equal(result.rounds[0].keys.length, 1)
  assert.equal(result.outOfRange.length, 0)
}

// 12. perspective reviewers grade their own findings: no classify agent, passes are unioned,
//     and every pass is told not to run code from the change (decisions 11-12)
{
  const perspective = { id: 'correctness', kind: 'perspective', sectionText: 'correctness', passes: 2, pass: 'no-blocking', blocking: true, autofix: 'agent' }
  const graded = (sev) => ({ path: 'a.py', line: 4, summary: 'off-by-one', failure_scenario: null, severity: sev, category: 'correctness' })
  const prompts = {}
  const { result, calls } = await run((label, prompt) => {
    prompts[label] = prompt
    if (label === 'r1:correctness#1') return { findings: [graded('blocking')] }
    if (label === 'r1:correctness#2') return { findings: [] }
    if (label.startsWith('r2:correctness')) return { findings: [graded('nit')] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, { ...baseArgs, gates: [perspective] })
  assert.equal(result.verdict, 'pass', 'a nit left after the fix does not block')
  assert.deepEqual(result.rounds[0].keys, ['correctness|a.py|correctness'])
  assert.ok(!calls.some(l => l.endsWith(':classify')), 'graded findings skip the classify agent')
  assert.ok(calls.includes('r1:correctness#2'), 'passes: 2 runs two independent reviews')
  assert.match(prompts['r1:correctness#1'], /You have no shell/)
}

// 13. a perspective finding without a valid category still goes through the classify agent
{
  const perspective = { id: 'docs-links', kind: 'perspective', sectionText: 'docs', pass: 'no-blocking', blocking: true, autofix: 'agent' }
  const { result, calls } = await run((label, prompt) => {
    if (label === 'r1:docs-links#1') return { findings: [{ path: 'a.py', line: 1, summary: 'doc drift', failure_scenario: null, severity: 'nit' }] }
    if (label.endsWith(':classify')) return classifyAll('nit', 'docs')(label, prompt)
    throw new Error(`unexpected ${label}`)
  }, { ...baseArgs, gates: [perspective] })
  assert.ok(calls.includes('r1:classify'))
  assert.equal(result.verdict, 'pass')
  assert.equal(result.remaining[0].category, 'docs')
}

// Decision 15 helpers: one perspective gate, graded findings, fix agents that report what they fixed
const corr = { id: 'correctness', kind: 'perspective', sectionText: 'correctness', pass: 'no-blocking', blocking: true, autofix: 'agent' }
const g = (path, summary, repeat_of = null) => ({ path, line: 1, summary, failure_scenario: null, severity: 'blocking', category: 'correctness', repeat_of })
const perspArgs = { ...baseArgs, changedFiles: ['a.py', 'b.py', 'README.md'], gates: [corr] }

// 14. the fourth e2e run: a different problem in the same file and category is not "the same reason"
{
  const prompts = {}
  const { result, calls } = await run((label, prompt) => {
    prompts[label] = prompt
    if (label === 'r1:correctness#1') return { findings: [g('README.md', 'retry count'), g('a.py', 'units'), g('b.py', 'paging')] }
    if (label === 'r2:correctness#1') return { findings: [g('README.md', 'is_fresh never called')] }
    if (label === 'r3:correctness#1') return { findings: [] }
    if (label.endsWith(':fix')) return { committed: true, sha: 'abc', files: ['README.md', 'a.py', 'b.py'], fixed: [1, 2, 3], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'pass', 'a new problem under an old key does not stop the loop')
  assert.deepEqual(result.rounds[1].keys, ['correctness|README.md|correctness'])
  assert.match(prompts['r2:correctness#1'], /Findings reported as fixed in the previous round/)
  assert.match(prompts['r2:correctness#1'], /1\. \[correctness\] README\.md:1 — retry count/)
  assert.doesNotMatch(prompts['r1:correctness#1'], /reported as fixed/)
  assert.ok(!calls.some(l => l.endsWith(':confirm')), 'no claimed repeat, no confirmer')
}

// 15. a reviewer marks a finding as a repeat of a fixed one and the confirmer finds it in the
//     current file -> the fix did not hold -> fail
{
  const { result, calls, types } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units'), g('b.py', 'paging')] }
    if (label === 'r2:correctness#1') return { findings: [{ ...g('a.py', 'units still wrong', 1), answer: 'absent', round: 9 }] }
    if (label === 'r2:confirm') return { items: [{ index: 1, stillPresent: true, quote: 'ttl = CACHE_TTL' }] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py', 'b.py'], fixed: [1, 2], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'fail')
  assert.match(result.stopReason, /fix did not hold\): correctness\|a\.py\|correctness \(repeat of #1\); confirmer: 1 still present, 0 no answer$/)
  assert.ok(calls.includes('r2:confirm'))
  assert.equal(types['r2:confirm'], 'ccreview:ccreview-reviewer', 'the confirmer is read-only')
  assert.deepEqual(result.disputed, [])
  // Decision 37: an answer that keeps the finding is reported too, not only the disputed ones, and
  // the reviewer's own answer and round fields cannot overwrite the confirmer's.
  assert.deepEqual(result.confirmations, [{ round: 2, gate: 'correctness', path: 'a.py', line: 1, summary: 'units still wrong', repeat_of: 1, answer: 'present', quote: 'ttl = CACHE_TTL' }])
}

// 16. a late find in a file the fix did not touch is progress, not churn
{
  const { result } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r2:correctness#1') return { findings: [g('b.py', 'paging, found late')] }
    if (label === 'r3:correctness#1') return { findings: [] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [] }
    if (label === 'r2:fix') return { committed: true, sha: 'def', files: ['b.py'], fixed: [1], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'pass')
  assert.equal(result.rounds.length, 3)
}

// 17. an explicit empty "fixed" list means nothing was fixed -> fail instead of assuming everything
{
  const { result } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['notes.txt'], fixed: [], skipped: ['a.py: unclear'], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'fail')
  assert.equal(result.stopReason, 'fix agent reported no fixed finding')
}

// 18. reviewers cannot run git: they Read the diff git wrote, the skill's diff-r1.patch for round 1 and
//     the fix agent's diff-r2.patch for round 2. No diff text travels through an agent (e2e session B:
//     the fix agent's copy came back double-escaped).
{
  const prompts = {}
  const { result, types } = await run((label, prompt) => {
    prompts[label] = prompt
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r2:correctness#1') return { findings: [] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [], diffWritten: true }
    throw new Error(`unexpected ${label}`)
  }, { ...perspArgs, diffDir: '/wt/.ccreview/' })
  assert.equal(result.verdict, 'pass')
  assert.match(prompts['r1:correctness#1'], /Read `\/wt\/\.ccreview\/diff-r1\.patch` before any other file/)
  assert.match(prompts['r2:correctness#1'], /Read `\/wt\/\.ccreview\/diff-r2\.patch` before any other file/)
  assert.doesNotMatch(prompts['r2:correctness#1'], /diff-r1\.patch|predates/)
  assert.ok(prompts['r1:fix'].includes(`git diff ${perspArgs.range} --output=/wt/.ccreview/diff-r2.patch`),
    'the exact string write_allowlist.py puts on the list for --diff-dir /wt/.ccreview/')
  assert.equal(types['r1:correctness#1'], 'ccreview:ccreview-reviewer')
}

// 18c. the last round never fixes, so no fix agent is told to write diff-r<maxRounds+1>.patch
//      (the allowlist stops at maxRounds; verify_fixes.py would call a missing one invalid)
{
  const prompts = {}
  const { result, calls } = await run((label, prompt) => {
    prompts[label] = prompt
    // a new file each round, so no stop condition fires before the last round
    const file = { r1: 'a.py', r2: 'b.py', r3: 'README.md' }[label.split(':')[0]]
    if (label.endsWith(':correctness#1')) return { findings: [g(file, `problem in ${file}`)] }
    if (label.endsWith(':fix')) return { committed: true, sha: 'abc', files: [file], fixed: [1], skipped: [], commandResults: [], diffWritten: true }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'fail')
  assert.match(result.stopReason, /reached max rounds/)
  assert.ok(calls.includes(`r${perspArgs.maxRounds - 1}:fix`), 'the rounds before the last do fix')
  assert.ok(!calls.includes(`r${perspArgs.maxRounds}:fix`))
  assert.ok(!Object.values(prompts).some(p => p.includes(`diff-r${perspArgs.maxRounds + 1}.patch`)))
}

// 18b. a fix agent that did not write the next diff: the reviewers keep the old file and are told it is stale
{
  const prompts = {}
  const { result } = await run((label, prompt) => {
    prompts[label] = prompt
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r2:correctness#1') return { findings: [] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [], diffWritten: false }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'pass')
  assert.match(prompts['r2:correctness#1'], /Read `\.ccreview\/diff-r1\.patch` before any other file/, 'default directory, old file')
  assert.match(prompts['r2:correctness#1'], /predates the round 1 fixes: for files the fixes changed \(a\.py\)/)
}

// 19. the reviewer type is missing (agent calls fail) -> indeterminate, never a silent fallback to Explore
{
  const { result, types } = await run((label) => {
    if (label.startsWith('r1:correctness')) throw new Error("Agent type 'ccreview-reviewer' not found")
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'indeterminate')
  assert.ok(!Object.values(types).includes('Explore'))
}

// 20. the fifth e2e run: the fix committed a hook-written session capture -> reported, verdict unchanged
{
  const prompts = {}
  const capture = '.claude/tasks/t/context-session.md'
  const { result } = await run((label, prompt) => {
    prompts[label] = prompt
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r2:correctness#1') return { findings: [] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py', capture], fixed: [1], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'pass')
  assert.deepEqual(result.unexpectedFiles, [{ round: 1, path: capture }])
  assert.match(prompts['r1:fix'], /git add -- <path>/)
  assert.match(prompts['r1:fix'], /--no-gpg-sign/)
}

// Decision 18 helpers: an untrusted change with one perspective gate and the kb-lint command gate
const kbLint = baseArgs.gates.find(x => x.id === 'kb-lint')
const untrustedArgs = { ...perspArgs, trust: 'untrusted', gates: [corr, kbLint] }

// 21. untrusted + a blocking finding -> fail at once: no fix agent, no command agent
{
  const { result, calls } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    throw new Error(`unexpected ${label}`)
  }, untrustedArgs)
  assert.equal(result.verdict, 'fail')
  assert.match(result.stopReason, /untrusted change/)
  assert.deepEqual(calls, ['r1:correctness#1'], 'only the read-only reviewer runs')
  assert.deepEqual(result.notRun, ['kb-lint'])
  assert.equal(result.trust, 'untrusted')
}

// 22. untrusted + clean reviewers + a blocking command gate -> indeterminate, never pass;
//     a reported command result is ignored because the command comes from the change
{
  const { result, calls } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [] }
    throw new Error(`unexpected ${label}`)
  }, { ...untrustedArgs, commandResults: [{ id: 'kb-lint', ...lintOk }] })
  assert.equal(result.verdict, 'indeterminate')
  assert.equal(result.stopReason, 'command gate(s) not run for an untrusted change: kb-lint')
  assert.deepEqual(calls, ['r1:correctness#1'])
}

// 23. untrusted + only a non-blocking command gate -> pass, with the gate reported as not run
{
  const { result } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [] }
    throw new Error(`unexpected ${label}`)
  }, { ...untrustedArgs, gates: [corr, { ...kbLint, blocking: false }] })
  assert.equal(result.verdict, 'pass')
  assert.deepEqual(result.notRun, ['kb-lint'])
}

// 24. a caller that does not decide the trust mode is rejected before any agent runs
{
  const { trust, ...noTrust } = baseArgs
  await assert.rejects(run(() => { throw new Error('no agent expected') }, noTrust), /args\.trust/)
}

// Decision 22 helpers: a repeat claim with a verbatim quote, as in the sixth e2e run
const rp = (path, summary, n, evidence) => ({ ...g(path, summary, n), evidence })

// 25. the sixth e2e run in a blocking gate: the reviewer quotes the old link, the confirmer reads the
//     current file and does not find it -> disputed, dropped from the verdict and the fix -> pass
{
  const prompts = {}
  const { result, calls } = await run((label, prompt) => {
    prompts[label] = prompt
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units'), g('README.md', 'link points to the wezterm entry')] }
    if (label === 'r2:correctness#1') return { findings: [rp('README.md', 'link still points to the wezterm entry', 2, 'see: [wezterm](wezterm.md)')] }
    if (label === 'r2:confirm') return { items: [{ index: 1, stillPresent: false, quote: 'see: [tmux](tmux.md)' }] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py', 'README.md'], fixed: [1, 2], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'pass')
  assert.deepEqual(result.rounds[1].keys, [])
  assert.ok(!calls.includes('r2:fix'), 'a disputed finding never reaches the fix agent')
  assert.equal(result.disputed.length, 1)
  assert.equal(result.disputed[0].round, 2)
  assert.equal(result.disputed[0].evidence, 'see: [wezterm](wezterm.md)')
  assert.equal(result.disputed[0].confirmerQuote, 'see: [tmux](tmux.md)')
  assert.deepEqual(result.confirmations.map(c => [c.path, c.answer, c.quote]), [['README.md', 'absent', 'see: [tmux](tmux.md)']])
  assert.deepEqual(result.remaining, [], 'disputed findings are reported apart from the remaining ones')
  assert.match(prompts['r2:correctness#1'], /copy into evidence/)
  const confirm = prompts['r2:confirm']
  assert.match(confirm, /quoted by the reviewer as the current text: see: \[wezterm\]\(wezterm\.md\)/)
  assert.doesNotMatch(confirm, /reported as fixed|previous round/, 'the confirmer never sees the fixed list')
  assert.doesNotMatch(confirm, /link points to the wezterm entry\n/, 'only the claim, not the round-1 wording')
}

// 26. the confirmer returns nothing -> the claim stands and the loop stops as before
{
  const { result, calls } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r2:correctness#1') return { findings: [rp('a.py', 'units still wrong', 1, 'x * 1000')] }
    if (label === 'r2:confirm') throw new Error('agent failed')
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.ok(calls.includes('r2:confirm'))
  assert.equal(result.verdict, 'fail')
  assert.match(result.stopReason, /fix did not hold\).*; confirmer: 0 still present, 1 no answer$/,
    'a stop the confirmer could not check reads differently from one it confirmed')
  assert.deepEqual(result.disputed, [])
  assert.deepEqual(result.confirmations.map(c => [c.path, c.answer, c.quote]), [['a.py', 'none', null]])
}

// 27. two claims, the confirmer answers only one (absent) -> the unanswered one still stops the loop
{
  const { result } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units'), g('b.py', 'paging')] }
    if (label === 'r2:correctness#1') return { findings: [rp('a.py', 'units still wrong', 1, 'x'), rp('b.py', 'paging still wrong', 2, 'y')] }
    if (label === 'r2:confirm') return { items: [{ index: 1, stillPresent: false, quote: 'x * 1000' }] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py', 'b.py'], fixed: [1, 2], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'fail')
  assert.match(result.stopReason, /fix did not hold\): correctness\|b\.py\|correctness \(repeat of #2\); confirmer: 0 still present, 1 no answer$/,
    'the disputed claim is not counted in the stop reason')
  assert.deepEqual(result.disputed.map(d => d.path), ['a.py'])
  assert.deepEqual(result.confirmations.map(c => [c.path, c.repeat_of, c.answer]), [['a.py', 1, 'absent'], ['b.py', 2, 'none']])
}

// 28. a claimed repeat on a non-blocking gate cannot stop the loop, so nobody re-checks it
{
  const scope = { id: 'review-scope', kind: 'perspective', sectionText: 'scope', pass: 'no-blocking', blocking: false, autofix: 'none' }
  const { result, calls } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r1:review-scope#1') return { findings: [] }
    if (label === 'r2:correctness#1') return { findings: [] }
    if (label === 'r2:review-scope#1') return { findings: [rp('a.py', 'units still wrong', 1, 'x')] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, { ...perspArgs, gates: [corr, scope] })
  assert.equal(result.verdict, 'pass')
  assert.ok(!calls.some(l => l.endsWith(':confirm')))
}

// Decision 25 helpers: one perspective gate and the kb-lint command gate, trusted
const withLint = { ...perspArgs, gates: [corr, kbLint], commandResults: lintFixed }
const askDeferred = [{ id: 'kb-lint', reason: 'matches the ask rule Bash(rm *) in settings.json' }]

// 29. a gate the skill left off the allowlist (ask rule, decision 23): the round-1 pre-flight result
//     counts, the fix agent is not asked to run it, and after the fix the would-be pass is indeterminate
{
  let fixPrompt = ''
  const { result, calls } = await run((label, prompt) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r2:correctness#1') return { findings: [] }
    if (label === 'r1:fix') { fixPrompt = prompt; return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [] } }
    throw new Error(`unexpected ${label}`)
  }, { ...withLint, deferredGates: askDeferred })
  assert.equal(result.verdict, 'indeterminate')
  assert.match(result.stopReason, /deferred to the main session: kb-lint$/)
  assert.deepEqual(result.deferred, [{ id: 'kb-lint', round: 2, blocking: true, reason: askDeferred[0].reason }])
  assert.doesNotMatch(fixPrompt, /kb-lint/, 'the fix agent is never asked to run a deferred gate')
  assert.doesNotMatch(fixPrompt, /run each command below/)
  assert.deepEqual(calls, ['r1:correctness#1', 'r1:fix', 'r2:correctness#1'])
}

// 30. no pre-flight result for round 1 -> deferred as well (no agent runs it), never a pass
{
  const { result, calls } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [] }
    throw new Error(`unexpected ${label}`)
  }, { ...withLint, commandResults: [] })
  assert.equal(result.verdict, 'indeterminate')
  assert.deepEqual(result.deferred, [{ id: 'kb-lint', round: 1, blocking: true, reason: 'no pre-flight result from the skill' }])
  assert.deepEqual(calls, ['r1:correctness#1'])
}

// 31. a deferred gate does not soften a fail: a blocking repeat still fails, the gate is still reported
{
  const { result } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r2:correctness#1') return { findings: [rp('a.py', 'units still wrong', 1, 'x')] }
    if (label === 'r2:confirm') return { items: [{ index: 1, stillPresent: true, quote: 'x' }] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, { ...withLint, deferredGates: askDeferred })
  assert.equal(result.verdict, 'fail')
  assert.match(result.stopReason, /fix did not hold/)
  assert.deepEqual(result.deferred.map(d => d.id), ['kb-lint'])
}

// 32. only a non-blocking gate deferred -> pass, with the gate reported
{
  const { result } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r2:correctness#1') return { findings: [] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, { ...withLint, gates: [corr, { ...kbLint, blocking: false }], deferredGates: askDeferred })
  assert.equal(result.verdict, 'pass')
  assert.deepEqual(result.deferred.map(d => [d.id, d.blocking]), [['kb-lint', false]])
}

// 33. a gate the fix agent skipped in round 1 but reported in round 2 leaves the deferred list
{
  const { result } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r2:correctness#1') return { findings: [g('b.py', 'paging, found late')] }
    if (label === 'r3:correctness#1') return { findings: [] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [] }
    if (label === 'r2:fix') return { committed: true, sha: 'def', files: ['b.py'], fixed: [1], skipped: [], commandResults: lintFixed }
    throw new Error(`unexpected ${label}`)
  }, withLint)
  assert.equal(result.verdict, 'pass')
  assert.deepEqual(result.deferred, [], 'the latest report for the final state wins')
}

// 34. plugin skills and workflows share the <plugin>:<name> namespace: in e2e session B the workflow,
//     then also named 'ccreview', hid skills/ccreview, so /ccreview:ccreview never loaded SKILL.md
{
  const skill = readFileSync(new URL('../skills/ccreview/SKILL.md', import.meta.url), 'utf8')
  const skillName = /^name:\s*(\S+)\s*$/m.exec(skill)[1]
  const workflowName = /^\s*name:\s*'([^']+)'/m.exec(src)[1]
  assert.notEqual(workflowName, skillName, 'the workflow must not take the skill name')
  assert.ok(skill.includes(`Workflow({name: "ccreview:${workflowName}"`), 'the skill launches the workflow by its real name')
}

// 35. e2e D1: a reviewer gave worktree-absolute paths, and seven blocking findings of a blocking gate
//     fell out of the range. Absolute, git's b/ and ./ forms of a changed file are in the range.
{
  let fixPrompt = ''
  const { result } = await run((label, prompt) => {
    if (label === 'r1:correctness#1') return { findings: [g('/wt/a.py', 'units'), g('b/b.py', 'paging'), g('./README.md', 'retry count')] }
    if (label === 'r2:correctness#1') return { findings: [] }
    if (label === 'r1:fix') { fixPrompt = prompt; return { committed: true, sha: 'abc', files: ['a.py', 'b.py', 'README.md'], fixed: [1, 2, 3], skipped: [], commandResults: [], diffWritten: true } }
    throw new Error(`unexpected ${label}`)
  }, { ...perspArgs, diffDir: '/wt/.ccreview/' })
  assert.equal(result.verdict, 'pass')
  assert.deepEqual(result.rounds[0].keys, ['correctness|README.md|correctness', 'correctness|a.py|correctness', 'correctness|b.py|correctness'])
  assert.match(fixPrompt, /a\.py:1 — units/, 'the fix agent gets the finding given with an absolute path')
  assert.deepEqual(result.outOfRange, [])
  assert.equal(result.outOfRangeBlocking, 0)
}

// 36. an absolute path from another root (the main checkout) maps to the longest changed file it ends
//     with; a path inside the worktree to an unchanged file, or a relative one that only ends like a
//     changed file, stays out of the range and is counted when a blocking gate graded it blocking
{
  const scope = { id: 'review-scope', kind: 'perspective', sectionText: 'scope', pass: 'no-blocking', blocking: false, autofix: 'none' }
  const outside = [g('/wt/lib/b.py', 'other file'), g('vendor/b.py', 'other file')]
  const { result } = await run((label, prompt) => {
    if (label === 'r1:correctness#1') return { findings: [g('/home/u/repo/docs/a.py', 'units'), ...outside] }
    if (label === 'r1:review-scope#1') return { findings: [g('/wt/lib/c.py', 'scope')] }
    if (label === 'r2:correctness#1') return { findings: outside }
    if (label === 'r2:review-scope#1') return { findings: [] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['docs/a.py'], fixed: [1], skipped: [], commandResults: [], diffWritten: true }
    throw new Error(`unexpected ${label}`)
  }, { ...perspArgs, changedFiles: ['a.py', 'docs/a.py', 'b.py'], gates: [corr, scope], diffDir: '/wt/.ccreview' })
  assert.deepEqual(result.rounds[0].keys, ['correctness|docs/a.py|correctness'], 'the longest match, not a.py')
  assert.deepEqual(result.outOfRange.map(f => f.path).sort(), ['lib/b.py', 'lib/b.py', 'lib/c.py', 'vendor/b.py', 'vendor/b.py'])
  assert.equal(result.outOfRangeBlocking, 2, 'blocking findings of the blocking gate only, not of review-scope, and a repeat in round 2 is not counted twice')
}

// 37. the fix agent reports worktree-absolute paths: the files it was allowed to touch are not unexpected,
//     and the next round's prompts list them repo-relative
{
  const prompts = {}
  const { result } = await run((label, prompt) => {
    prompts[label] = prompt
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r2:correctness#1') return { findings: [g('b.py', 'paging')] }
    if (label === 'r3:correctness#1') return { findings: [] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['/wt/a.py'], fixed: [1], skipped: [], commandResults: [], diffWritten: true }
    if (label === 'r2:fix') return { committed: true, sha: 'def', files: ['/wt/b.py'], fixed: [1], skipped: [], commandResults: [], diffWritten: true }
    throw new Error(`unexpected ${label}`)
  }, { ...perspArgs, diffDir: '/wt/.ccreview' })
  assert.equal(result.verdict, 'pass')
  assert.deepEqual(result.unexpectedFiles, [])
  assert.match(prompts['r2:fix'], /You may edit only files already changed in the range: a\.py b\.py README\.md\. /)
}

// 38. e2e D2: three of four perspectives gave line numbers inside the diff file. Every round tells the
//     reviewers that a line is the file's own, taken from Read.
{
  const prompts = {}
  await run((label, prompt) => {
    prompts[label] = prompt
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units')] }
    if (label === 'r2:correctness#1') return { findings: [] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [], diffWritten: true }
    throw new Error(`unexpected ${label}`)
  }, { ...perspArgs, diffDir: '/wt/.ccreview' })
  for (const label of ['r1:correctness#1', 'r2:correctness#1']) {
    assert.match(prompts[label], /line is the line number in the file itself, not in the diff file/, label)
  }
}

// Decision 33 helper: a graded perspective finding with an origin
const go = (path, summary, origin) => ({ ...g(path, summary), origin })

// 39. e2e D4: a pre-existing problem in a file the change touched is neither judged nor fixed. It is
//     reported with its round, and counted once although it comes back in round 2.
{
  let fixPrompt = ''
  const { result, calls } = await run((label, prompt) => {
    if (label === 'r1:correctness#1') return { findings: [go('a.py', 'units', 'introduced'), go('README.md', 'old mismatch', 'pre-existing'), go('b.py', 'doc names the old function', 'exposed')] }
    if (label === 'r2:correctness#1') return { findings: [go('README.md', 'old mismatch', 'pre-existing')] }
    if (label === 'r1:fix') { fixPrompt = prompt; return { committed: true, sha: 'abc', files: ['a.py', 'b.py'], fixed: [1, 2], skipped: [], commandResults: [], diffWritten: true } }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'pass', 'a pre-existing finding alone does not fail round 2')
  assert.deepEqual(result.rounds[0].keys, ['correctness|a.py|correctness', 'correctness|b.py|correctness'], 'an exposed finding is judged')
  assert.doesNotMatch(fixPrompt, /old mismatch/, 'the fix agent never sees it')
  assert.deepEqual(result.preExisting.map(f => [f.round, f.gate, f.path]), [[1, 'correctness', 'README.md'], [2, 'correctness', 'README.md']])
  assert.equal(result.preExistingBlocking, 1)
  assert.deepEqual(result.remaining, [])
  assert.ok(!calls.includes('r2:fix'))
}

// 40. only pre-existing findings -> pass in round 1 without a fix; blocking ones of a non-blocking gate
//     are reported but not counted
{
  const scope = { id: 'review-scope', kind: 'perspective', sectionText: 'scope', pass: 'no-blocking', blocking: false, autofix: 'none' }
  const { result, calls } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [go('a.py', 'old bug', 'pre-existing')] }
    if (label === 'r1:review-scope#1') return { findings: [go('b.py', 'old scope note', 'pre-existing')] }
    throw new Error(`unexpected ${label}`)
  }, { ...perspArgs, gates: [corr, scope] })
  assert.equal(result.verdict, 'pass')
  assert.ok(!calls.some(l => l.endsWith(':fix')))
  assert.equal(result.preExisting.length, 2)
  assert.equal(result.preExistingBlocking, 1)
}

// 41. fail toward judging: a missing or unknown origin is judged, and only perspective findings can be
//     left out (a /code-review finding that says pre-existing is still judged)
{
  const { result } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'no origin'), go('b.py', 'unknown origin', 'legacy')] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: [], fixed: [], skipped: ['unclear'], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.deepEqual(result.rounds[0].keys, ['correctness|a.py|correctness', 'correctness|b.py|correctness'])
  assert.deepEqual(result.preExisting, [])
  const { result: reviewed } = await run((label, prompt) => {
    if (label.includes('code-review')) return { textReturned: true, findings: [{ ...bug, origin: 'pre-existing' }] }
    if (label.endsWith(':classify')) return classifyAll('blocking', 'correctness')(label, prompt)
    if (label === 'r1:fix') return { committed: false, sha: null, files: [], skipped: [] }
    throw new Error(`unexpected ${label}`)
  })
  assert.deepEqual(reviewed.rounds[0].keys, ['code-review|a.py|correctness'])
  assert.deepEqual(reviewed.preExisting, [])
}

// 42. every round asks the one question that decides origin, and the reviewers' schema requires it
{
  const prompts = {}
  const { schemas } = await run((label, prompt) => {
    prompts[label] = prompt
    if (label === 'r1:correctness#1') return { findings: [go('a.py', 'units', 'introduced')] }
    if (label === 'r2:correctness#1') return { findings: [] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [], diffWritten: true }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  for (const label of ['r1:correctness#1', 'r2:correctness#1']) {
    assert.match(prompts[label], /were reverted, would this problem still be there exactly as it is\?/, label)
  }
  assert.ok(schemas['r1:correctness#1'].properties.findings.items.required.includes('origin'))
}

// 43. e2e D5: the comment draft took a fixed finding's line from the diff file. Each round returns what the
//     fix agent claims it fixed, with the reviewers' paths and lines; a round with no fix returns [].
{
  const { result } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [{ ...go('a.py', 'off-by-one', 'introduced'), line: 38 }, go('CLAUDE.md', 'old name', 'exposed')] }
    if (label === 'r2:correctness#1') return { findings: [go('CLAUDE.md', 'old name', 'exposed')] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: ['2: protected'], commandResults: [], diffWritten: true }
    if (label === 'r2:fix') return { committed: false, sha: null, files: [], fixed: [], skipped: ['1: protected'], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, { ...perspArgs, changedFiles: ['a.py', 'CLAUDE.md'] })
  assert.equal(result.stopReason, 'fix agent made no commit')
  assert.deepEqual(result.rounds[0].fixedFindings, [{ gate: 'correctness', path: 'a.py', line: 38, category: 'correctness', summary: 'off-by-one' }],
    'only the claimed number, not the skipped one')
  assert.deepEqual(result.rounds[1].fixedFindings, [], 'a round whose fix made no commit')
}

// 44. open point J: a reviewer puts the number of a fixed finding on a problem in another file, one the fix
//     agent never claimed. It is a new finding, not a repeat: no confirmer, no "fix did not hold" stop,
//     the next fix gets it, and the mismatch is reported.
{
  const { result, calls } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units'), g('b.py', 'paging')] }
    if (label === 'r2:correctness#1') return { findings: [rp('b.py', 'paging still wrong', 1, 'page = 0')] }
    if (label === 'r3:correctness#1') return { findings: [] }
    if (label === 'r2:confirm') return { items: [{ index: 1, stillPresent: true, quote: 'page = 0' }] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: ['2: not reached'], commandResults: [] }
    if (label === 'r2:fix') return { committed: true, sha: 'def', files: ['b.py'], fixed: [1], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.ok(!calls.includes('r2:confirm'), 'a mismatched repeat_of never reaches the confirmer')
  assert.ok(calls.includes('r2:fix'), 'the finding goes to the next fix')
  assert.equal(result.verdict, 'pass')
  assert.deepEqual(result.repeatMismatches, [
    { round: 2, gate: 'correctness', path: 'b.py', line: 1, summary: 'paging still wrong', repeat_of: 1, claimedPath: 'a.py' },
  ])
}

// 45. a repeat_of that points at a finding in the same file is still a repeat, and nothing is reported as a mismatch
{
  const { result } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units'), g('b.py', 'paging')] }
    if (label === 'r2:correctness#1') return { findings: [rp('b.py', 'paging still wrong', 2, 'page = 0')] }
    if (label === 'r2:confirm') return { items: [{ index: 1, stillPresent: true, quote: 'page = 0' }] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py', 'b.py'], fixed: [1, 2], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'fail')
  assert.match(result.stopReason, /fix did not hold\): correctness\|b\.py\|correctness/)
  assert.deepEqual(result.repeatMismatches, [])
}

// 46. open point I: in-range perspective findings called introduced are returned with their round, so
//     verify_fixes.py can check their lines against git. They are still judged. Exposed, pre-existing,
//     out-of-range and non-perspective findings are not in the list.
{
  const { result } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [go('a.py', 'units', 'introduced'), go('b.py', 'doc names the old function', 'exposed'), go('README.md', 'old mismatch', 'pre-existing'), go('outside.py', 'elsewhere', 'introduced')] }
    if (label === 'r2:correctness#1') return { findings: [{ ...go('b.py', 'paging', 'introduced'), line: 7, round: 99, gate: 'forged' }] }
    if (label === 'r3:correctness#1') return { findings: [] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py', 'b.py'], fixed: [1, 2], skipped: [], commandResults: [] }
    if (label === 'r2:fix') return { committed: true, sha: 'def', files: ['b.py'], fixed: [1], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.equal(result.verdict, 'pass')
  assert.deepEqual(result.rounds[0].keys, ['correctness|a.py|correctness', 'correctness|b.py|correctness'], 'introduced findings are still judged')
  assert.deepEqual(result.introduced.map(f => [f.round, f.gate, f.path, f.line]), [[1, 'correctness', 'a.py', 1], [2, 'correctness', 'b.py', 7]],
    'a reviewer cannot overwrite the round and gate the check uses')
  const { result: reviewed } = await run((label, prompt) => {
    if (label.includes('code-review')) return { textReturned: true, findings: [{ ...bug, origin: 'introduced' }] }
    if (label.endsWith(':classify')) return classifyAll('blocking', 'correctness')(label, prompt)
    if (label === 'r1:fix') return { committed: false, sha: null, files: [], skipped: [] }
    throw new Error(`unexpected ${label}`)
  })
  assert.deepEqual(reviewed.introduced, [], 'only perspective findings carry a reviewer-set origin')
}

// 47. e2e D7 and D8: the reviewer that tagged base lines "introduced" had not read the diff. Every round
//     puts the diff (and the note on a stale diff) before the instruction to read the changed files, and
//     ties "introduced" to a line of the diff (decision 40).
{
  for (const diffWritten of [true, false]) {
    const prompts = {}
    await run((label, prompt) => {
      prompts[label] = prompt
      if (label === 'r1:correctness#1') return { findings: [go('a.py', 'units', 'introduced')] }
      if (label === 'r2:correctness#1') return { findings: [] }
      if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [], diffWritten }
      throw new Error(`unexpected ${label}`)
    }, { ...perspArgs, diffDir: '/wt/.ccreview' })
    for (const label of ['r1:correctness#1', 'r2:correctness#1']) {
      const p = prompts[label]
      const diffAt = p.indexOf('## Diff')
      const filesAt = p.indexOf('Changed files (')
      assert.ok(diffAt >= 0 && filesAt > diffAt, `${label}: the diff comes before the changed files`)
      assert.ok(p.indexOf('After the diff, Read the changed files') > diffAt, label)
      assert.match(p, /\nOnly if that file does not exist, review from the changed files alone\.\n/, `${label}: the fallback is narrowed`)
      assert.doesNotMatch(p, /read the changed files instead/, label)
      assert.match(p, /When there is a diff file, set it only after you have found in it the \+ or - line that causes the problem\./, label)
    }
    const p2 = prompts['r2:correctness#1']
    assert.match(p2, diffWritten ? /Read `\/wt\/\.ccreview\/diff-r2\.patch` before any other file/ : /Read `\/wt\/\.ccreview\/diff-r1\.patch` before any other file/)
    const staleAt = p2.indexOf('That diff predates')
    if (diffWritten) assert.equal(staleAt, -1, 'no stale note when the next diff was written')
    else assert.ok(staleAt > p2.indexOf('## Diff') && staleAt < p2.indexOf('Changed files ('), 'the stale note stays with the diff')
  }
}

// 48. decision 35: a repeat_of that is not a number of a fixed finding (0, past the list, not an integer)
//     points at nothing. It is a new finding: no confirmer, not reported as a mismatch, and the next fix gets it.
{
  for (const n of [0, 2, 1.5]) {
    let fixPrompt = ''
    const { result, calls } = await run((label, prompt) => {
      if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units'), g('b.py', 'paging')] }
      if (label === 'r2:correctness#1') return { findings: [rp('b.py', 'paging still wrong', n, 'page = 0')] }
      if (label === 'r3:correctness#1') return { findings: [] }
      if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: ['2: not reached'], commandResults: [] }
      if (label === 'r2:fix') { fixPrompt = prompt; return { committed: true, sha: 'def', files: ['b.py'], fixed: [1], skipped: [], commandResults: [] } }
      throw new Error(`unexpected ${label}`)
    }, perspArgs)
    assert.ok(!calls.includes('r2:confirm'), `repeat_of ${n}: no confirmer`)
    assert.match(fixPrompt, /b\.py:1 — paging still wrong/, `repeat_of ${n}: the finding goes to the next fix`)
    assert.equal(result.verdict, 'pass', `repeat_of ${n}`)
    assert.deepEqual(result.repeatMismatches, [], `repeat_of ${n}: nothing to compare, so no mismatch`)
  }
}

// 49. decision 35: the same reviewer slip in two rounds is recorded once per round. Each round compares the
//     number with that round's fixed list, so the two records point at different fixed findings.
{
  const { result, calls } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units'), g('b.py', 'paging')] }
    if (label === 'r2:correctness#1') return { findings: [rp('b.py', 'paging still wrong', 1, 'page = 0'), g('README.md', 'retry count')] }
    if (label === 'r3:correctness#1') return { findings: [rp('b.py', 'paging still wrong', 1, 'page = 0')] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: ['2: not reached'], commandResults: [] }
    if (label === 'r2:fix') return { committed: true, sha: 'def', files: ['README.md'], fixed: [2], skipped: ['1: not reached'], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, perspArgs)
  assert.ok(!calls.some(l => l.endsWith(':confirm')), 'no confirmer in any round')
  assert.equal(result.verdict, 'fail')
  assert.equal(result.stopReason, 'reached max rounds (3)')
  assert.deepEqual(result.repeatMismatches.map(m => [m.round, m.path, m.repeat_of, m.claimedPath]), [
    [2, 'b.py', 1, 'a.py'],
    [3, 'b.py', 1, 'README.md'],
  ])
}

// 50. decision 35: a mismatch in a file the last fix touched counts toward churn, as the same finding
//     without a repeat_of does. The stop reason then says the fix introduced it, which nobody checked.
{
  for (const n of [1, null]) {
    const { result, calls } = await run((label) => {
      if (label === 'r1:correctness#1') return { findings: [g('a.py', 'units'), g('b.py', 'paging')] }
      if (label === 'r2:correctness#1') return { findings: [rp('b.py', 'paging still wrong', n, 'page = 0')] }
      if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py', 'b.py'], fixed: [1], skipped: ['2: not reached'], commandResults: [] }
      throw new Error(`unexpected ${label}`)
    }, perspArgs)
    assert.ok(!calls.includes('r2:confirm'), `repeat_of ${n}: no confirmer`)
    assert.equal(result.verdict, 'fail', `repeat_of ${n}`)
    assert.equal(result.stopReason, 'fixes introduced as many new failures as they removed: correctness|b.py|correctness', `repeat_of ${n}`)
    assert.deepEqual(result.repeatMismatches.map(m => [m.round, m.path, m.claimedPath]), n ? [[2, 'b.py', 'a.py']] : [], `repeat_of ${n}`)
  }
}

// 53. decision 45: a gate kind the workflow does not run (a pr-body gate passed by mistake) stops it at start,
//     before any agent runs, naming the gate
{
  const prBody = { id: 'pr-evidence', kind: 'pr-body', require: ['Evidence'], blocking: true }
  await assert.rejects(run(() => { throw new Error('no agent expected') }, { ...baseArgs, gates: [...baseArgs.gates, prBody] }),
    /pr-evidence: pr-body/)
}

// 54. decision 47: the spec-scope gate the skill adds (perspective, blocking, no auto-fix) fails the change
//     on an out-of-scope finding without calling the fix agent, and still runs for an untrusted change
{
  const specScope = { id: 'spec-scope', kind: 'perspective', ref: 'Spec scope', sectionText: 'spec', passes: 1, pass: 'no-blocking', blocking: true, autofix: 'none' }
  const out = { ...g('a.py', 'adds the retry the spec leaves out'), origin: 'introduced' }
  for (const trust of ['trusted', 'untrusted']) {
    const { result, calls, types } = await run((label) => {
      if (label === 'r1:spec-scope#1') return { findings: [out] }
      throw new Error(`unexpected ${label}`)
    }, { ...baseArgs, trust, gates: [specScope], commandResults: [] })
    assert.equal(result.verdict, 'fail', trust)
    assert.deepEqual(result.rounds[0].keys, ['spec-scope|a.py|correctness'], trust)
    assert.ok(!calls.some(l => l.endsWith(':fix')), `${trust}: no fix agent for a gate without autofix`)
    assert.equal(types['r1:spec-scope#1'], 'ccreview:ccreview-reviewer', `${trust}: read-only reviewer`)
    assert.equal(result.stopReason, trust === 'trusted'
      ? 'blocking findings on gates without autofix'
      : 'blocking findings; auto-fix and command gates are off for an untrusted change', trust)
  }
}

// 55. decision 47: a spec-scope finding (no autofix) in the file a fix touched is not churn. Before, it made
//     the stop reason "fixes introduced as many new failures as they removed", though the fix never saw it.
{
  const corrFix = { id: 'correctness', kind: 'perspective', sectionText: 'c', passes: 1, pass: 'no-blocking', blocking: true, autofix: 'agent' }
  const specScope = { id: 'spec-scope', kind: 'perspective', sectionText: 'spec', passes: 1, pass: 'no-blocking', blocking: true, autofix: 'none' }
  const out = { ...g('a.py', 'adds the retry the spec leaves out'), origin: 'introduced' }
  const { result, calls } = await run((label) => {
    if (label === 'r1:correctness#1') return { findings: [{ ...g('a.py', 'units'), origin: 'introduced' }] }
    if (label === 'r2:correctness#1') return { findings: [] }
    if (label.includes('spec-scope')) return { findings: [out] }
    if (label === 'r1:fix') return { committed: true, sha: 'abc', files: ['a.py'], fixed: [1], skipped: [], commandResults: [] }
    throw new Error(`unexpected ${label}`)
  }, { ...baseArgs, gates: [corrFix, specScope], commandResults: [] })
  assert.ok(calls.includes('r1:fix'), 'the fix agent works on the correctness finding')
  assert.equal(result.verdict, 'fail')
  assert.deepEqual(result.rounds[1].keys, ['spec-scope|a.py|correctness'])
  assert.equal(result.stopReason, 'blocking findings on gates without autofix')
}

console.log('loop-sim: 55 scenarios passed')
