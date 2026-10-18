"""Check what a ccreview run's reviewers read and what its fix agents reported against git (decision 19 in docs/decisions.ja.md).

Usage (from the worktree root, on the reviewed branch, right after the workflow returns, trusted or not):
    python3 verify_fixes.py --base <merge-base SHA> --start <HEAD before the workflow> --result <result.json>
                            [--diff-dir .ccreview]

Reviewers cannot run git, so each round's diff is a file git wrote: <diff dir>/diff-r1.patch by the skill,
diff-r<N+1>.patch by the fix agent of round N after its commit. This script compares every such file with
git diff for that point, and the fix agents' reported commits and files with the commits git has.

Output (JSON on stdout):
    status           "ok" or "mismatch"
    invalid          reasons that invalidate the verdict: an unreported or missing commit, or a diff file
                     that is missing or differs from git (the reviewers read something else)
    notes            file-list differences; reported to the user, the verdict stands (decision 17)
    unexpectedFiles  files the fix commits changed that are neither in the range nor in a failing key
    preExistingConflicts  findings a reviewer called pre-existing on a line the change added or changed
                     (decision 33); the verdict stands, the skill asks the user to look at them
    introducedOnUnchangedLines  findings a reviewer called introduced on a line the change neither added
                     nor changed (decision 36); the verdict stands, the skill reports them to the user
    fixedOutsideCommit  findings a fix agent reported as fixed in a file its commit did not change
                     (decision 38); the verdict stands, the skill reports them to the user
"""
import argparse
import json
import os
import re
import subprocess
import sys


def git(*a):
    # errors="replace", as for the diff files: a non-UTF-8 source must not crash the check.
    return subprocess.run(["git", *a], check=True, capture_output=True, text=True, errors="replace").stdout


def resolve(sha):
    r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def normalize_diff(text):
    # Only trailing whitespace is forgiven.
    lines = [line.rstrip() for line in (text or "").splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def check_diff_file(path, want, what, invalid):
    """Compare a round's diff file with git's diff; record why the reviewers read something else."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            got = normalize_diff(fh.read())
    except FileNotFoundError:
        invalid.append(f"{what}: {path} was not written; the reviewers had no diff or an older one")
        return
    want = normalize_diff(want)
    if got != want:
        invalid.append(f"{what}: {path} differs from git (first difference at line {first_difference(got, want)})")


def first_difference(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i + 1
    return min(len(a), len(b)) + 1


def reported_as(path, reported):
    # Gates may report paths from another root; accept a '/'-bounded suffix, as the workflow does.
    return any(path == r or path.endswith(f"/{r}") for r in reported)


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(base, state, path, around_deletions=False):
    """Line numbers of path at state that git diff base..state shows as added or changed.

    With around_deletions, a pure deletion (a hunk with no new-side lines, +c,0) also counts the lines
    just before and after the removed ones (c and c + 1): a problem a deletion makes shows there.
    """
    lines = set()
    # :(top) reads the path from the repository root: a plain pathspec is relative to the cwd, and from a
    # subdirectory the diff would be empty and every conflict missed. literal keeps *, ? and [ in a
    # reported path from being read as a glob: git matches the exact name too, so a glob adds the hunks
    # of other files, whose lines would be taken for this file's (false conflicts).
    out = git("diff", "--no-color", "--no-ext-diff", "-U0", f"{base}..{state}", "--", f":(top,literal){path}")
    for line in out.splitlines():
        m = HUNK.match(line)
        if m:
            first, count = int(m.group(1)), int(m.group(2) or "1")
            lines.update(range(first, first + count))
            if around_deletions and count == 0:
                lines.update((first, first + 1))
    return lines


def findings_by_touched(base, start, result, key, around_deletions, want_touched):
    """The findings in result[key] whose line is (want_touched) or is not a line the change touched.

    Each finding is checked against the branch as its round read it: start for round 1, the previous
    round's fix commit after that. A finding without a line or path is not checked.
    """
    fix_sha = {r["round"]: r["fix"].get("sha") for r in result.get("rounds", [])
               if r.get("fix") and r["fix"].get("committed")}
    cache, hits = {}, []
    for f in result.get(key, []):
        rnd, line, path = f.get("round"), f.get("line"), f.get("path")
        if not isinstance(line, int) or not path:
            continue
        state = resolve(start if rnd == 1 else fix_sha.get(rnd - 1) or "")
        if not state:
            continue  # an unreported fix commit is already an invalid reason
        if (state, path) not in cache:
            cache[(state, path)] = changed_lines(base, state, path, around_deletions)
        if (line in cache[(state, path)]) == want_touched:
            hits.append({"round": rnd, "gate": f.get("gate"), "path": path, "line": line,
                         "severity": f.get("severity"), "summary": f.get("summary")})
    return hits


def pre_existing_conflicts(base, start, result):
    """Decision 33: a finding on a line the change added or changed cannot be pre-existing.

    An unchanged line may still be wrong because of the change (an exposed finding), so it is not
    checked here. A line number that is off by one can only add a conflict.
    """
    return findings_by_touched(base, start, result, "preExisting", around_deletions=False, want_touched=True)


def introduced_on_unchanged_lines(base, start, result):
    """Decision 36 (open point I): the other direction. A finding called introduced on a line the change
    neither added nor changed may be an old problem a reviewer mislabelled (e2e D7: one reviewer that
    had not read the diff called four base problems introduced). It may also be an exposed problem
    labelled introduced, or a wrong line number; so it is only reported, and the verdict stands.
    Lines next to a pure deletion count as touched, since a problem a deletion makes shows there.
    """
    return findings_by_touched(base, start, result, "introduced", around_deletions=True, want_touched=False)


def commit_files(sha):
    return set(git("-c", "core.quotepath=off", "diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines())


def fixed_outside_commit(result, actual):
    """Decision 38 (D7 observation 5): findings a fix agent reported as fixed in a file its commit did not change.

    The confirmer only checks that a repeated problem is still there, so a finding nobody tried to fix
    would stop the loop as "fix did not hold". A cross-file finding can rightly be fixed on the other
    side, so this is reported, never judged. A commit that is not one of the workflow's own is already
    invalid and is skipped here.
    """
    out = []
    for r in result.get("rounds", []):
        fix = r.get("fix") or {}
        sha = resolve(fix.get("sha") or "") if fix.get("committed") else None
        if not sha or sha not in actual:
            continue
        files = commit_files(sha)
        out += [{"round": r["round"], "gate": f.get("gate"), "path": f.get("path"), "line": f.get("line"),
                 "summary": f.get("summary")}
                for f in r.get("fixedFindings") or [] if isinstance(f, dict) and f.get("path") not in files]
    return out


def verify(base, start, result, diff_dir=".ccreview"):
    invalid, notes, unexpected = [], [], []
    diff_path = lambda rnd: os.path.join(diff_dir, f"diff-r{rnd}.patch")  # noqa: E731
    # quotepath=off and one name per line: non-ASCII names and names with spaces must match the reported paths
    range_files = set(git("-c", "core.quotepath=off", "diff", "--name-only", f"{base}..{start}").splitlines())
    actual = git("rev-list", "--reverse", f"{start}..HEAD").split()
    fixes = [(r["round"], r["fix"]) for r in result.get("rounds", []) if r.get("fix") and r["fix"].get("committed")]

    check_diff_file(diff_path(1), git("diff", f"{base}..{start}"), "round 1", invalid)

    if len(actual) != len(fixes):
        invalid.append(f"{len(actual)} commit(s) after the workflow started, {len(fixes)} reported by fix agents")

    seen = set()
    for rnd, fix in fixes:
        sha = resolve(fix.get("sha") or "")
        if not sha or sha not in actual:
            invalid.append(f"round {rnd}: reported sha {fix.get('sha')!r} is not a commit made during the workflow")
            continue
        seen.add(sha)
        files = sorted(commit_files(sha))
        reported = fix.get("files") or []
        for f in files:
            if not reported_as(f, reported):
                notes.append(f"round {rnd}: {f} is in the commit but not in the reported files")
        for f in reported:
            if not any(reported_as(a, [f]) for a in files):
                notes.append(f"round {rnd}: {f} is reported but not in the commit")
        keys = next((r.get("keys", []) for r in result["rounds"] if r["round"] == rnd), [])
        expected = range_files | {k.split("|")[1] for k in keys}
        unexpected += [{"round": rnd, "path": f} for f in files if f not in expected]

        # A fix after which the loop stopped was read by no reviewer: its diff file does not matter.
        if any(r["round"] == rnd + 1 for r in result.get("rounds", [])):
            check_diff_file(diff_path(rnd + 1), git("diff", f"{base}..{sha}"),
                            f"round {rnd} fix (read in round {rnd + 1})", invalid)

    for sha in actual:
        if sha not in seen:
            invalid.append(f"commit {sha[:12]} was not reported by any fix agent")

    return {"status": "mismatch" if invalid else "ok", "invalid": invalid, "notes": notes,
            "unexpectedFiles": unexpected,
            "preExistingConflicts": pre_existing_conflicts(base, start, result),
            "introducedOnUnchangedLines": introduced_on_unchanged_lines(base, start, result),
            "fixedOutsideCommit": fixed_outside_commit(result, actual)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--result", required=True)
    p.add_argument("--diff-dir", default=".ccreview", help="where the round diffs are: the absolute path the skill passed")
    a = p.parse_args()
    with open(a.result) as fh:
        result = json.load(fh)
    out = verify(a.base, a.start, result, a.diff_dir)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0 if out["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
