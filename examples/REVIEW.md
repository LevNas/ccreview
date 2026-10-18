# Review instructions

The review perspectives for this repository. The ccreview skill takes sections out of this file and hands them to the perspective agents and the classifier agent.

- The three sections "Severity definitions", "Category vocabulary" and "Rules for every perspective" are given to every perspective. Do not rename their headings (the Japanese headings "重大度の定義", "category の語彙" and "観点共通の決まり" also work). If one is missing, the run stops before it starts.
- The other sections are the perspectives' text. Point at one from `ref` in `.claude/review-gates.yaml` by writing its heading exactly. When you rename a heading, change both.
- The words in backticks in "Category vocabulary" are the categories that may be used.
- The comment draft is written in the language of the body of "Severity definitions" (Japanese or English).

## Severity definitions

Give each finding exactly one of these.

- `blocking` — must be fixed before merging
  - Wrong behavior (a wrong calculation, branch or boundary; an input that crashes it)
  - Exposed secrets (tokens, passwords, internal host names, IPs or account names)
  - The change makes an existing document wrong, and that document was not updated
- `nit` — worth fixing, but does not block the merge
  - Unused imports or variables, naming, wording, formatting

When in doubt, use `nit`. Keep `blocking` for "merging as is would give someone a wrong result".

## Category vocabulary

Choose exactly one of these for each finding. Use no other word.

- `correctness` — wrong behavior, calculation or branching
- `security` — permissions, input validation, dangerous operations
- `secrets` — exposed secrets or internal information
- `docs` — wrong or outdated documentation
- `links` — broken or mismatched links and references
- `cleanup` — unused code, duplication, formatting

## Rules for every perspective

Every perspective agent follows these rules.

- Look at the diff of the given range, the whole of each changed file, and the files they reference.
- Do not run code or scripts in the change. Only read-only tools (Read, Grep, Glob) are available, so a malicious change is never executed.
- If you notice a problem outside your perspective, you may report it. Overlapping perspectives make up for what a single reviewer misses.
- Give each finding one severity and one category, following the definitions above.

## Correctness

Look for wrong behavior, calculations and branches; boundaries (off-by-one, empty input, numbers that do not divide evenly); inputs that crash; state carried over (such as mutable default arguments); mixed-up times or units; and results that differ from what the code or its comments promise.

## Security and secrets

Look for injection into commands or SQL, handling of untrusted input, and dangerous operations. Also look for exposed credentials, passwords, tokens and internal host names or IPs. Treat values that are only disguised, such as base64, as exposed too.

## Docs and links

Look for README, docstring and comment text that disagrees with what the code actually does (counts, defaults, units, promises such as "fetches everything"). For Markdown links to files in the repository, check not only that the target exists but also that the link text matches what the target actually says, by opening the target. (The perspective agents have only Read, Grep and Glob, so they cannot open external URLs.)

## Cross-file consistency

Between the changed files, and with the code they call, check that what one side assumes about the other matches: names, signatures, argument order, return types, units.
