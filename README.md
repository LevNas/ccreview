# ccreview (v0.1, prototype)

A Claude Code plugin that gates a PR/MR or branch before merge. It runs perspective reviewers, declared command gates and a bounded auto-fix loop as a Workflow, and returns a verdict and a comment draft. It never posts or merges.

The design decisions and the verification behind them are summarized in [`docs/decisions.ja.md`](docs/decisions.ja.md) (Japanese). Labels in this README and the code such as "decision N" (the heading "決定 N") or "e2e D5" point there.

## Install

Install from the `levnas-plugins` marketplace:

```text
/plugin marketplace add LevNas/claudecode-plugins
/plugin install ccreview@levnas-plugins
```

Then start a new session (agent types and hooks are loaded only when a session starts).

To try a local clone, start a session at the root of the repository to review with the plugin loaded:

```sh
claude --plugin-dir <path to ccreview>
```

## Declaring the review

The repository under review needs two files. Copying [`examples/`](examples/) and editing it is the quickest start.

| File | Contents |
|---|---|
| `REVIEW.md` (repository root) | Severity definitions, the category vocabulary, rules for every perspective, and one section per perspective (example: [`examples/REVIEW.md`](examples/REVIEW.md)) |
| `.claude/review-gates.yaml` | The gate declarations. A perspective gate names a `REVIEW.md` section heading in `ref` (example with the keys explained: [`examples/review-gates.yaml`](examples/review-gates.yaml)) |

The three shared sections may use the English headings of the example or the Japanese headings "重大度の定義", "category の語彙" and "観点共通の決まり". Headings are matched ignoring case. A `ref` names its section heading exactly, so when you re-copy one example file, keep the `ref` values and the `REVIEW.md` headings in the same language. The comment draft is written in the language the user or the loaded instructions name; otherwise in Japanese when the body of the severity section is Japanese, and in English otherwise (decision 44).

A gate of `kind: pr-body` checks the PR body instead of the diff (decision 45):

```yaml
- id: pr-evidence
  kind: pr-body
  require: ["Evidence", "Not tested"]   # one item per section; "Evidence|確かめたこと" accepts either name
  blocking: true
```

- Each section must be there and have text of its own; whether the text is true is not checked.
- Only ATX headings (`#` to `######`) count. A child section's text does not count for its parent. One filled heading is enough when a name appears twice.
- HTML comments are removed first, so a commented-out template section is missing. Headings inside code fences do not count. Bare list markers and lone `<placeholders>` are empty; an image tag or a link is text.
- GitHub PRs only. It is not run for a GitLab MR or a branch with no PR, and `paths` is not read.

When a PR is judged `untrusted`, both files are read from the base branch instead of the working tree, so the PR cannot weaken the gates by rewriting them. If they are missing on the base, the run stops.

## Usage

In a session, give the skill `/ccreview:ccreview` a PR number or branch name.

You can also pass a spec: the absolute path of a file that records what the work was meant to do and to leave out, such as a plan (decision 47). When the spec has a section headed exactly "Out of Scope" or "範囲外" with text in it, the skill adds a perspective gate `spec-scope` that reports changes doing what that section leaves out. The gate is blocking, has no auto-fix, and runs for trusted and untrusted changes alike. Without such a section, the skill adds no gate and says why. Matching the change against the spec's other sections is not done.

When the fix agent edits a file under `.claude/` (knowledge-base documents and the like), Claude Code's protected-path check applies (in manual and acceptEdits; in auto the classifier decides, and in dontAsk the edit is denied and only the other findings are fixed). Settings allow rules cannot lift it, and the plugin's hook allowing it did not lift it either (e2e D1). The prompt for the background fix agent's edit appears in the session that started ccreview, and the run waits until the user approves or allows editing the `.claude` folder for the session (decision 31).

It depends on no other plugin. The result comes back as a verdict and a comment draft (a temporary file outside the repository). On `fail`, the next steps are in a not-to-be-posted section of the same file. Whether to keep them as a plan or task, and where, is the user's call (decision 29).

## Layout

| Path | Role |
|---|---|
| `skills/ccreview/SKILL.md` | The main session's steps (target and trust, reading the declarations, writing the allowlist, starting the workflow, the check, the report and comment draft) |
| `workflows/ccreview-loop.js` | The review and fix loop (`ccreview:ccreview-loop`). Named apart from the skill because a workflow with the skill's name hides the skill |
| `agents/ccreview-reviewer.md` | Perspective, classifier and confirmer agents. Read, Grep and Glob only (decision 16) |
| `agents/ccreview-fixer.md` | The fix agent. Its Bash and edits are held by the hook (decision 21) |
| `hooks/gate.py` | Decides allow / deny for the fix agent's Bash, Edit, Write and NotebookEdit from the per-run allowlist. Does nothing for other agents or the main session (decisions 21 and 24) |
| `scripts/write_allowlist.py` | Writes the allowlist to the worktree's git dir. Defers command gates that match an `ask` rule (decisions 23 and 25). Takes the gates as `--gates '<json>'` or from a file with `--gates-file` (decision 46) |
| `scripts/verify_fixes.py` | Checks the fix agent's reports against git (decision 19) |
| `scripts/extract_result.py` | Takes the result out of the workflow's output file and writes it to a file outside the repository, so the main session does not build its own way of reading it (decision 42) |
| `scripts/check_pr_body.py` | For a `kind: pr-body` gate: checks that the PR body has each required section and that it is not empty. Run by the skill in the main session, never by the workflow (decision 45). The skill also runs it on a spec with `--body-file` before adding the `spec-scope` gate (decision 47) |
| `docs/decisions.ja.md` | Design decisions (1–47) and a summary of the verification behind them (Japanese) |
| `examples/` | Example `REVIEW.md` and `.claude/review-gates.yaml` |

## Tests

Run from the plugin root:

```sh
node tests/loop-sim.mjs
python3 tests/gate_test.py
python3 tests/write_allowlist_test.py
python3 tests/verify_fixes_test.py
python3 tests/extract_result_test.py
python3 tests/check_pr_body_test.py
```

## License

MIT (`LICENSE`).
