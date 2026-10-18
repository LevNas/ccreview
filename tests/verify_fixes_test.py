"""Tests for scripts/verify_fixes.py against a throwaway git repository.
Run: python3 tests/verify_fixes_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "verify_fixes.py")
# The fixture repo must not trigger the user's commit signing or hooks.
GIT = ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
       "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


class VerifyFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.git("init", "-q", "-b", "main")
        self.write("a.py", "x = 1\n")
        self.write("doc.md", "one\ntwo\nthree\n")
        self.base = self.commit("base")
        self.write("a.py", "x = 2\n")
        self.write("doc.md", "one\nTWO\nthree\n")  # the change touches line 2 only
        self.start = self.commit("change under review")
        # As the skill does before starting the workflow.
        os.makedirs(os.path.join(self.dir, ".ccreview"))
        self.git("diff", f"{self.base}..HEAD", "--output=.ccreview/diff-r1.patch")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *a):
        return subprocess.run([*GIT, *a], cwd=self.dir, check=True, capture_output=True, text=True).stdout

    def write(self, path, text):
        full = os.path.join(self.dir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)

    def commit(self, msg):
        self.git("add", "-A", "--", ".", ":!.ccreview")  # the diff files stay untracked, as in a real run
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD").strip()

    def fix_commit(self, files, rnd=1, write_diff=True):
        """Commit as the fix agent of round rnd, then write the next round's diff as it does."""
        for path, text in files.items():
            self.write(path, text)
        sha = self.commit(f"ccreview: round {rnd} fixes")
        if write_diff:
            self.git("diff", f"{self.base}..HEAD", f"--output=.ccreview/diff-r{rnd + 1}.patch")
        return sha

    def verify(self, rounds, cwd=None, extra=(), pre_existing=None, introduced=None):
        path = os.path.join(self.dir, "result.json")
        result = {"rounds": rounds}
        if pre_existing is not None:
            result["preExisting"] = pre_existing
        if introduced is not None:
            result["introduced"] = introduced
        with open(path, "w") as fh:
            json.dump(result, fh)
        r = subprocess.run([sys.executable, SCRIPT, "--base", self.base, "--start", self.start, "--result", path, *extra],
                           cwd=cwd or self.dir, capture_output=True, text=True)
        return json.loads(r.stdout)

    def test_absolute_diff_dir_works_from_another_cwd(self):
        # The skill passes the absolute path; the main session may sit in a subdirectory.
        sub = os.path.join(self.dir, "pkg")
        os.makedirs(sub)
        out = self.verify([{"round": 1, "keys": []}], cwd=sub, extra=("--diff-dir", os.path.join(self.dir, ".ccreview")))
        self.assertEqual(out["status"], "ok", out)

    def tamper(self, rnd, old, new):
        path = os.path.join(self.dir, ".ccreview", f"diff-r{rnd}.patch")
        with open(path) as fh:
            text = fh.read()
        with open(path, "w") as fh:
            fh.write(text.replace(old, new))

    def test_no_fix_checks_the_round_one_diff(self):
        self.assertEqual(self.verify([{"round": 1, "keys": []}])["status"], "ok")
        self.tamper(1, "+x = 2", "+x = 9")
        out = self.verify([{"round": 1, "keys": []}])
        self.assertEqual(out["status"], "mismatch")
        self.assertTrue(any(r.startswith("round 1:") and "differs from git" in r for r in out["invalid"]), out)

    def test_missing_round_one_diff_is_invalid(self):
        os.remove(os.path.join(self.dir, ".ccreview", "diff-r1.patch"))
        out = self.verify([{"round": 1, "keys": []}])
        self.assertEqual(out["status"], "mismatch")
        self.assertTrue(any("was not written" in r for r in out["invalid"]), out)

    def test_honest_report_is_ok(self):
        sha = self.fix_commit({"a.py": "x = 3\n"})
        out = self.verify([{"round": 1, "keys": ["correctness|a.py|correctness"],
                            "fix": {"committed": True, "sha": sha[:10], "files": ["a.py"], "diffWritten": True}}])
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["notes"], [])
        self.assertEqual(out["unexpectedFiles"], [])

    def test_unreported_file_is_noted_and_unexpected(self):
        # The fifth e2e run: a hook-written capture went into the fix commit.
        sha = self.fix_commit({"a.py": "x = 3\n", ".claude/tasks/t/context.md": "log\n"})
        out = self.verify([{"round": 1, "keys": ["correctness|a.py|correctness"],
                            "fix": {"committed": True, "sha": sha, "files": ["a.py"]}}])
        self.assertEqual(out["status"], "ok", "a file-list difference alone does not invalidate the verdict")
        self.assertEqual(out["unexpectedFiles"], [{"round": 1, "path": ".claude/tasks/t/context.md"}])
        self.assertTrue(any("not in the reported files" in n for n in out["notes"]))

    def test_diff_file_differing_from_git_is_invalid(self):
        sha = self.fix_commit({"a.py": "x = 3\n"})
        self.tamper(2, "+x = 3", "+x = 2")
        out = self.verify([{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha, "files": ["a.py"]}},
                           {"round": 2, "keys": []}])
        self.assertEqual(out["status"], "mismatch")
        self.assertTrue(any("round 1 fix (read in round 2)" in r and "differs from git" in r for r in out["invalid"]), out)

    def test_fix_without_a_diff_file_is_invalid(self):
        # e2e session B: round 2 reviewed a diff other than the branch. A missing file means the same.
        sha = self.fix_commit({"a.py": "x = 3\n"}, write_diff=False)
        out = self.verify([{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha, "files": ["a.py"]}},
                           {"round": 2, "keys": []}])
        self.assertEqual(out["status"], "mismatch")
        self.assertTrue(any("diff-r2.patch was not written" in r for r in out["invalid"]), out)

    def test_fix_no_round_read_is_not_checked(self):
        # The loop stopped after the fix (e.g. no fixed finding): no reviewer read diff-r2, so a fail stays a fail.
        sha = self.fix_commit({"a.py": "x = 3\n"}, write_diff=False)
        out = self.verify([{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha, "files": ["a.py"]}}])
        self.assertEqual(out["status"], "ok", out)

    def test_non_utf8_source_does_not_crash(self):
        with open(os.path.join(self.dir, "sjis.txt"), "wb") as fh:
            fh.write("日本語\n".encode("shift_jis"))
        sha = self.fix_commit({})
        out = self.verify([{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha, "files": ["sjis.txt"]}},
                           {"round": 2, "keys": []}])
        self.assertEqual(out["status"], "ok", out)

    def test_two_fix_rounds_each_check_their_own_file(self):
        sha1 = self.fix_commit({"a.py": "x = 3\n"}, rnd=1)
        sha2 = self.fix_commit({"a.py": "x = 4\n"}, rnd=2)
        rounds = [{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha1, "files": ["a.py"]}},
                  {"round": 2, "keys": [], "fix": {"committed": True, "sha": sha2, "files": ["a.py"]}},
                  {"round": 3, "keys": []}]
        self.assertEqual(self.verify(rounds)["status"], "ok")
        self.tamper(2, "+x = 3", "+x = 4")  # round 2 read round 3's text: written at the wrong time
        self.assertTrue(any("read in round 2" in r for r in self.verify(rounds)["invalid"]))

    def pre(self, rnd, line, path="doc.md"):
        return {"round": rnd, "gate": "correctness", "path": path, "line": line,
                "severity": "blocking", "summary": f"old problem at {line}"}

    def test_pre_existing_on_a_changed_line_conflicts(self):
        # Decision 33: only a line the change added or changed contradicts "pre-existing". An unchanged
        # line may be exposed by the change, so it is not flagged; a finding without a line is not checked.
        out = self.verify([{"round": 1, "keys": []}],
                          pre_existing=[self.pre(1, 2), self.pre(1, 3), self.pre(1, None), self.pre(1, 1, "a.py")])
        self.assertEqual(out["status"], "ok", "a conflict does not invalidate the verdict")
        self.assertEqual([(c["path"], c["line"]) for c in out["preExistingConflicts"]], [("doc.md", 2), ("a.py", 1)])

    def test_pre_existing_after_a_fix_is_checked_against_that_fix(self):
        # Round 2 read the branch after the round 1 fix, which changed line 3.
        sha = self.fix_commit({"doc.md": "one\nTWO\nTHREE\n"})
        rounds = [{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha, "files": ["doc.md"]}},
                  {"round": 2, "keys": []}]
        out = self.verify(rounds, pre_existing=[self.pre(1, 3), self.pre(2, 3)])
        self.assertEqual([(c["round"], c["line"]) for c in out["preExistingConflicts"]], [(2, 3)])

    def test_pre_existing_conflict_found_from_a_subdirectory(self):
        # The main session may sit in a subdirectory; the finding's path is from the repository root.
        sub = os.path.join(self.dir, "pkg")
        os.makedirs(sub)
        out = self.verify([{"round": 1, "keys": []}], cwd=sub, extra=("--diff-dir", os.path.join(self.dir, ".ccreview")),
                          pre_existing=[self.pre(1, 2)])
        self.assertEqual([(c["path"], c["line"]) for c in out["preExistingConflicts"]], [("doc.md", 2)])

    def test_pre_existing_path_with_glob_characters_is_literal(self):
        # Read as a glob, "doc[1].md" also matches "doc1.md" (git tries the exact name too), so lines the
        # change touched in doc1.md would be taken for lines of doc[1].md: a false conflict.
        self.git("checkout", "-q", self.base)
        self.write("doc[1].md", "one\ntwo\n")
        self.write("doc1.md", "one\ntwo\n")
        base = self.commit("base with glob-like names")
        self.write("doc[1].md", "ONE\ntwo\n")
        self.write("doc1.md", "one\nTWO\n")
        start = self.commit("change line 1 of the bracketed file and line 2 of the other")
        self.base, self.start = base, start
        self.git("diff", f"{base}..HEAD", "--output=.ccreview/diff-r1.patch")
        out = self.verify([{"round": 1, "keys": []}],
                          pre_existing=[self.pre(1, 1, "doc[1].md"), self.pre(1, 2, "doc[1].md")])
        self.assertEqual([(c["path"], c["line"]) for c in out["preExistingConflicts"]], [("doc[1].md", 1)])

    def test_no_pre_existing_list_means_no_conflicts(self):
        self.assertEqual(self.verify([{"round": 1, "keys": []}])["preExistingConflicts"], [])

    def new(self, rnd, line, path="doc.md"):
        return {"round": rnd, "gate": "cross-file", "path": path, "line": line,
                "severity": "blocking", "summary": f"new problem at {line}"}

    def test_introduced_on_an_unchanged_line_is_reported(self):
        # Decision 36 (open point I): the other direction of decision 33's check. A finding the reviewer
        # called introduced on a line the change neither added nor changed is reported; the verdict
        # stands. A finding without a line is not checked.
        out = self.verify([{"round": 1, "keys": []}],
                          introduced=[self.new(1, 2), self.new(1, 3), self.new(1, None), self.new(1, 1, "a.py")])
        self.assertEqual(out["status"], "ok", "a report does not invalidate the verdict")
        self.assertEqual([(c["path"], c["line"]) for c in out["introducedOnUnchangedLines"]], [("doc.md", 3)])
        self.assertEqual(out["preExistingConflicts"], [])

    def test_introduced_next_to_a_deletion_is_not_reported(self):
        # git diff -U0 gives a pure deletion no new-side lines (@@ -3 +2,0 @@). A problem the deletion
        # made shows on the line before or after the removed lines, so those two count as touched.
        self.git("checkout", "-q", self.base)
        self.write("doc.md", "one\ntwo\nthree\nfour\nfive\n")
        base = self.commit("base")
        self.write("doc.md", "one\ntwo\nfour\nfive\n")  # removes "three"
        start = self.commit("delete line 3")
        self.base, self.start = base, start
        self.git("diff", f"{base}..HEAD", "--output=.ccreview/diff-r1.patch")
        out = self.verify([{"round": 1, "keys": []}],
                          introduced=[self.new(1, 1), self.new(1, 2), self.new(1, 3), self.new(1, 4)],
                          pre_existing=[self.pre(1, 2)])
        self.assertEqual([c["line"] for c in out["introducedOnUnchangedLines"]], [1, 4])
        # Decision 33's check is unchanged: a line next to a deletion is not a line the change added or
        # changed, so an old problem reported there is not a conflict.
        self.assertEqual(out["preExistingConflicts"], [])

    def test_introduced_after_a_fix_is_checked_against_that_fix(self):
        # Round 2 read the branch after the round 1 fix, which changed line 3.
        sha = self.fix_commit({"doc.md": "one\nTWO\nTHREE\n"})
        rounds = [{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha, "files": ["doc.md"]}},
                  {"round": 2, "keys": []}]
        out = self.verify(rounds, introduced=[self.new(1, 3), self.new(2, 3)])
        self.assertEqual([(c["round"], c["line"]) for c in out["introducedOnUnchangedLines"]], [(1, 3)])

    def test_no_introduced_list_means_no_report(self):
        self.assertEqual(self.verify([{"round": 1, "keys": []}])["introducedOnUnchangedLines"], [])

    def fixed(self, path, line=1):
        return {"gate": "correctness", "path": path, "line": line, "category": "correctness", "summary": f"fixed in {path}"}

    def test_fixed_finding_outside_the_commit_is_reported(self):
        # Decision 38 (D7 observation 5): a finding the fix agent reported as fixed in a file its commit
        # did not change was probably not fixed. Reported only; the verdict stands, since a cross-file
        # finding can rightly be fixed on the other side. Paths come from git with quotepath off, so a
        # non-ASCII name is compared as written.
        sha = self.fix_commit({"a.py": "x = 3\n", "日本語.md": "new\n"})
        rounds = [{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha, "files": ["a.py", "日本語.md"]},
                   "fixedFindings": [self.fixed("a.py"), self.fixed("doc.md", 2), self.fixed("日本語.md")]},
                  {"round": 2, "keys": []}]
        out = self.verify(rounds)
        self.assertEqual(out["status"], "ok", "a report does not invalidate the verdict")
        self.assertEqual(out["fixedOutsideCommit"],
                         [{"round": 1, "gate": "correctness", "path": "doc.md", "line": 2, "summary": "fixed in doc.md"}])

    def test_fixed_outside_is_checked_per_round(self):
        # Each round's claims are checked against that round's commit, not the files changed so far.
        sha1 = self.fix_commit({"a.py": "x = 3\n"}, rnd=1)
        sha2 = self.fix_commit({"doc.md": "one\nTWO\nTHREE\n"}, rnd=2)
        rounds = [{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha1, "files": ["a.py"]},
                   "fixedFindings": [self.fixed("a.py")]},
                  {"round": 2, "keys": [], "fix": {"committed": True, "sha": sha2, "files": ["doc.md"]},
                   "fixedFindings": [self.fixed("doc.md"), self.fixed("a.py")]},
                  {"round": 3, "keys": []}]
        self.assertEqual([(c["round"], c["path"]) for c in self.verify(rounds)["fixedOutsideCommit"]], [(2, "a.py")])

    def test_fixed_outside_skips_a_commit_the_workflow_did_not_make(self):
        # An unknown sha, or one made before the workflow, is already invalid; its claims are not
        # reported a second time.
        for sha in ("0" * 40, self.start):
            rounds = [{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha, "files": ["a.py"]},
                       "fixedFindings": [self.fixed("b.py")]}]
            out = self.verify(rounds)
            self.assertEqual(out["status"], "mismatch")
            self.assertEqual(out["fixedOutsideCommit"], [], sha)

    def test_no_fixed_findings_means_no_report(self):
        sha = self.fix_commit({"a.py": "x = 3\n"})
        out = self.verify([{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha, "files": ["a.py"]}}])
        self.assertEqual(out["fixedOutsideCommit"], [])

    def test_fixed_outside_skips_rounds_without_a_commit_and_broken_items(self):
        # A round whose fix did not commit (or has no fix) has no commit to compare with; an item that is
        # not an object is skipped rather than stopping the whole check.
        sha = self.fix_commit({"a.py": "x = 3\n"})
        rounds = [{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha, "files": ["a.py"]},
                   "fixedFindings": ["doc.md", self.fixed("doc.md")]},
                  {"round": 2, "keys": [], "fix": {"committed": False, "sha": sha, "files": []},
                   "fixedFindings": [self.fixed("b.py")]},
                  {"round": 3, "keys": [], "fix": None, "fixedFindings": [self.fixed("c.py")]}]
        out = self.verify(rounds)
        self.assertEqual([(c["round"], c["path"]) for c in out["fixedOutsideCommit"]], [(1, "doc.md")])

    def test_unreported_commit_is_invalid(self):
        sha = self.fix_commit({"a.py": "x = 3\n"})
        self.fix_commit({"b.py": "y = 1\n"}, rnd=2, write_diff=False)
        out = self.verify([{"round": 1, "keys": [], "fix": {"committed": True, "sha": sha, "files": ["a.py"]}}])
        self.assertEqual(out["status"], "mismatch")
        self.assertTrue(any("not reported by any fix agent" in r for r in out["invalid"]), out)


if __name__ == "__main__":
    unittest.main()
