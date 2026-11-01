"""Tests for scripts/prepare_review.py.
Run: python3 tests/prepare_review_test.py

Each test builds a throwaway origin (a bare repository) and a clone standing in for the PR's worktree, and
puts a fake `gh` first on PATH. The first three tests are the regressions of decision 48: failures of the
hand-written preparation scripts this one replaces.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "prepare_review.py")

REVIEW = """# Review instructions

## Severity definitions

Blocking or nit.

## Category vocabulary

- `correctness` - wrong behavior
- `docs` - wrong documentation

## Rules for every perspective

Read the whole file.

## Correctness

Look for wrong behavior.

## Notes

Notes body text.
"""

GATES = """# gates for the test repository
max_rounds: 2

gates:
  - id: correctness
    kind: perspective
    ref: "Correctness"
    pass: no-blocking
    blocking: true
    autofix: agent

  - id: count
    kind: command
    run: "python3 scripts/count_files.py {files}"   # counts the files it was given that exist
    pass: exit0
    blocking: true
    autofix: agent
    paths: ["docs/**"]

  - id: notes-scope
    kind: perspective
    ref: 'Notes'
    pass: no-blocking
    blocking: false
    autofix: none
    paths: ["notes/**"]

  - id: unused
    kind: perspective
    ref: "Correctness"
    pass: no-blocking
    blocking: true
    autofix: agent
    paths: ["nothing/**"]

  - id: pr-evidence
    kind: pr-body
    require: ["Evidence", "Not tested"]
    blocking: true
"""

COUNT = """import os, sys
n = sum(1 for p in sys.argv[1:] if os.path.isfile(p))
print(f"checked {n} file(s)")
sys.exit(0 if n else 1)
"""

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
if a[:2] == ["api", "user"]:
    print(os.environ["FAKE_USER"])
elif a[:2] == ["pr", "view"] and "body" in a:
    print(json.dumps({"body": os.environ.get("FAKE_BODY", "")}))
elif a[:2] == ["pr", "view"]:
    print(os.environ["FAKE_PR"])
else:
    sys.exit("fake gh: unexpected " + " ".join(a))
"""

BODY = "## Summary\n\nx\n\n## Evidence\n\n- ran it\n\n## Not tested\n\n- nothing else\n"

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class PrepareReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self.tmp.name)
        self.origin = os.path.join(self.dir, "origin.git")
        self.seed = os.path.join(self.dir, "seed")
        self.wt = os.path.join(self.dir, "wt")
        self.out = os.path.join(self.dir, "out", "args.json")
        os.makedirs(os.path.dirname(self.out))
        self.git(self.dir, "init", "-q", "--bare", "-b", "main", self.origin)
        self.git(self.dir, "clone", "-q", self.origin, self.seed)
        self.write(self.seed, "REVIEW.md", REVIEW)
        self.write(self.seed, ".claude/review-gates.yaml", GATES)
        self.write(self.seed, "scripts/count_files.py", COUNT)
        self.write(self.seed, "docs/a.md", "a\n")
        self.git(self.seed, "add", "-A")
        self.git(self.seed, "commit", "-q", "-m", "base")
        self.git(self.seed, "push", "-q", "origin", "HEAD:main")
        self.git(self.dir, "clone", "-q", self.origin, self.wt)
        self.git(self.wt, "checkout", "-q", "-b", "feature")
        self.write(self.wt, "docs/a.md", "a changed\n")
        self.write(self.wt, "notes/b.md", "b\n")
        self.commit("change")
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write(FAKE_GH)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC)
        self.cfg = os.path.join(self.dir, "cfg")
        os.makedirs(self.cfg)
        self.env = dict(os.environ, **GIT_ENV, PATH=bindir + os.pathsep + os.environ["PATH"],
                        CLAUDE_CONFIG_DIR=self.cfg,
                        CCREVIEW_MANAGED_SETTINGS=os.path.join(self.dir, "_none.json"),
                        FAKE_USER="alice", FAKE_BODY=BODY)
        self.pr(author="alice")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True,
                              env=dict(os.environ, **GIT_ENV)).stdout.strip()

    def write(self, root, rel, text):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def commit(self, msg):
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", msg)

    def pr(self, author="alice", head=None):
        self.env["FAKE_PR"] = json.dumps({
            "headRefName": "feature", "baseRefName": "main", "author": {"login": author},
            "url": "https://github.com/acme/widget/pull/7",
            "headRefOid": head or self.git(self.wt, "rev-parse", "HEAD")})

    def run_script(self, *args, cwd=None):
        r = subprocess.run([sys.executable, SCRIPT, "--worktree", self.wt, "--out", self.out, *args],
                           capture_output=True, text=True, env=self.env, cwd=cwd or self.dir)
        args_json = None
        if os.path.exists(self.out):
            with open(self.out, encoding="utf-8") as f:
                args_json = json.load(f)
        return r.returncode, r.stdout, r.stderr, args_json

    def gate(self, args_json, gid):
        return next((g for g in args_json["gates"] if g["id"] == gid), None)

    def assertStopped(self, result, text):
        code, out, err, args_json = result
        self.assertEqual(code, 2, out + err)
        self.assertIn(text, err)
        self.assertIsNone(args_json)

    # --- regressions (decision 48) -------------------------------------------------------------

    def test_gates_with_paths_are_kept_when_a_file_matches(self):
        code, out, err, a = self.run_script("--pr", "7")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([g["id"] for g in a["gates"]], ["correctness", "count", "notes-scope"])
        self.assertIn("skipped: unused (paths matched no changed file)", out)
        self.assertEqual(self.gate(a, "count")["run"], "python3 scripts/count_files.py docs/a.md")
        self.assertEqual(self.gate(a, "count")["template"], "python3 scripts/count_files.py {files}")

    def test_command_gates_run_in_the_worktree_and_report_the_file_count(self):
        code, out, err, a = self.run_script("--pr", "7", cwd=self.dir)  # not the worktree
        self.assertEqual(code, 0, out + err)
        [res] = a["commandResults"]
        self.assertEqual(res["id"], "count")
        self.assertEqual(res["exitCode"], 0, res)
        self.assertIn("checked 1 file(s)", res["outputTail"])
        self.assertIn("command: count exit 0 (1 file)", out)

    def test_the_last_review_section_is_read(self):
        code, out, err, a = self.run_script("--pr", "7")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.gate(a, "notes-scope")["sectionText"], "Notes body text.")

    # --- the rest of steps 1 to 3 ----------------------------------------------------------------

    def test_trusted_pr_writes_args_allowlist_and_runs_the_pr_body_gate(self):
        code, out, err, a = self.run_script("--pr", "7")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(out.startswith("declarations: worktree"), out)
        self.assertIn("trust: trusted (PR author alice = gh user alice)", out)
        base = self.git(self.wt, "merge-base", "origin/main", "HEAD")
        self.assertEqual(a["range"], base + "..HEAD")
        self.assertEqual(a["changedFiles"], ["docs/a.md", "notes/b.md"])
        self.assertEqual(a["diffDir"], os.path.join(self.wt, ".ccreview"))
        self.assertTrue(os.path.isfile(os.path.join(self.wt, ".ccreview", "diff-r1.patch")))
        self.assertEqual(a["maxRounds"], 2)
        self.assertEqual(a["trust"], "trusted")
        self.assertEqual(a["review"]["categories"], ["correctness", "docs"])
        self.assertEqual(a["review"]["severityText"], "Blocking or nit.")
        self.assertEqual(a["review"]["commonText"], "Read the whole file.")
        self.assertEqual(self.gate(a, "correctness")["passes"], 1)
        self.assertEqual(a["deferredGates"], [])
        self.assertIn('pr-body: pr-evidence exit 0', out)
        gitdir = self.git(self.wt, "rev-parse", "--absolute-git-dir")
        self.assertTrue(os.path.isfile(os.path.join(gitdir, "ccreview", "allowlist.json")))

    def test_max_rounds_argument_wins_over_the_yaml(self):
        code, out, err, a = self.run_script("--pr", "7", "--max-rounds", "1")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(a["maxRounds"], 1)

    def test_untrusted_reads_the_base_declarations_and_runs_no_command(self):
        self.write(self.wt, ".claude/review-gates.yaml", "max_rounds: 1\ngates:\n  - id: count\n    kind: command\n"
                   "    run: \"true\"\n    pass: exit0\n    blocking: false\n    autofix: none\n")
        self.commit("weaken the gates")
        self.pr(author="mallory")
        code, out, err, a = self.run_script("--pr", "7")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(out.startswith("declarations: origin/main"), out)
        self.assertIn("trust: untrusted (PR author mallory, gh user alice)", out)
        self.assertEqual(a["trust"], "untrusted")
        self.assertEqual(self.gate(a, "correctness")["blocking"], True)
        self.assertEqual(self.gate(a, "count")["run"], "python3 scripts/count_files.py docs/a.md")
        self.assertEqual(a["commandResults"], [])
        self.assertIn("not run: untrusted", out)
        gitdir = self.git(self.wt, "rev-parse", "--absolute-git-dir")
        self.assertFalse(os.path.exists(os.path.join(gitdir, "ccreview", "allowlist.json")))

    def test_user_stated_trust_wins(self):
        self.pr(author="mallory")
        code, out, err, a = self.run_script("--pr", "7", "--trust", "trusted")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(a["trust"], "trusted")
        self.assertIn("trust: trusted (stated by the user)", out)

    def test_branch_target_is_trusted_and_skips_pr_body_gates(self):
        code, out, err, a = self.run_script("--branch", "feature")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(a["trust"], "trusted")
        self.assertIn("pr-body: pr-evidence not run (no GitHub PR)", out)

    def test_borrowed_declarations(self):
        decl = os.path.join(self.dir, "decl")
        shutil.copytree(self.seed, decl, ignore=shutil.ignore_patterns(".git"))
        self.git(self.wt, "rm", "-q", "REVIEW.md", ".claude/review-gates.yaml")
        self.commit("no declarations here")
        self.pr(author="alice")
        code, out, err, a = self.run_script("--pr", "7", "--declarations-from", decl)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out.splitlines()[0], f"declarations: borrowed from {decl}")
        self.assertIn(os.path.join(self.wt, "docs/a.md"), a["changedFiles"])
        self.assertTrue(all(os.path.isabs(f) for f in a["changedFiles"]), a["changedFiles"])
        self.assertIsNone(self.gate(a, "count"))
        self.assertIn("skipped: count (declarations are borrowed; their commands are not in this repository)", out)
        self.assertEqual(a["commandResults"], [])

    def test_borrowing_is_refused_when_untrusted(self):
        self.pr(author="mallory")
        self.assertStopped(self.run_script("--pr", "7", "--declarations-from", self.seed), "untrusted")

    def test_spec_with_out_of_scope_adds_the_gate(self):
        spec = os.path.join(self.dir, "plan.md")
        self.write(self.dir, "plan.md", "# Plan\n\n## Out of Scope\n\n- no new commands\n")
        code, out, err, a = self.run_script("--pr", "7", "--spec", spec)
        self.assertEqual(code, 0, out + err)
        g = self.gate(a, "spec-scope")
        self.assertEqual((g["kind"], g["blocking"], g["autofix"]), ("perspective", True, "none"))
        self.assertIn(spec, g["sectionText"])

    def test_spec_without_out_of_scope_adds_no_gate(self):
        spec = os.path.join(self.dir, "plan.md")
        self.write(self.dir, "plan.md", "# Plan\n\n## Steps\n\n- do it\n")
        code, out, err, a = self.run_script("--pr", "7", "--spec", spec)
        self.assertEqual(code, 0, out + err)
        self.assertIsNone(self.gate(a, "spec-scope"))
        self.assertIn("spec: no spec-scope gate", out)

    def test_claude_files_in_the_range_are_reported(self):
        self.write(self.wt, ".claude/notes.md", "x\n")
        self.commit("touch .claude")
        self.pr()
        code, out, err, a = self.run_script("--pr", "7")
        self.assertEqual(code, 0, out + err)
        self.assertIn("note: the range has files under .claude/", out)

    # --- stops ---------------------------------------------------------------------------------------

    def test_stops_when_head_is_not_the_pr_head(self):
        self.pr(head="0" * 40)
        self.assertStopped(self.run_script("--pr", "7"), "PR head")

    def test_stops_when_the_diff_dir_exists(self):
        os.mkdir(os.path.join(self.wt, ".ccreview"))
        self.assertStopped(self.run_script("--pr", "7"), "--remove")

    def test_stops_when_the_change_contains_ccreview(self):
        self.write(self.wt, ".ccreview/diff-r1.patch", "x\n")
        self.commit("planted")
        self.pr()
        self.assertStopped(self.run_script("--pr", "7"), ".ccreview")

    def test_stops_when_a_shared_section_is_missing(self):
        self.write(self.wt, "REVIEW.md", REVIEW.replace("## Rules for every perspective", "## Something else"))
        self.commit("drop a section")
        self.pr()
        self.assertStopped(self.run_script("--pr", "7"), "Rules for every perspective")
        self.assertFalse(os.path.exists(os.path.join(self.wt, ".ccreview")))

    def test_stops_on_yaml_shapes_it_does_not_read(self):
        shapes = {
            "nested map": GATES.replace("    autofix: agent\n", "    autofix:\n      mode: agent\n", 1),
            "block list": GATES.replace('    paths: ["docs/**"]\n', '    paths:\n      - "docs/**"\n'),
        }
        for name, text in shapes.items():
            with self.subTest(name):
                self.write(self.wt, ".claude/review-gates.yaml", text)
                self.commit(name)
                self.pr()
                self.assertStopped(self.run_script("--pr", "7"), "line ")
                self.assertFalse(os.path.exists(os.path.join(self.wt, ".ccreview")))

    def test_stops_when_out_is_inside_the_repository(self):
        r = subprocess.run([sys.executable, SCRIPT, "--worktree", self.wt, "--out", os.path.join(self.wt, "a.json"),
                            "--pr", "7"], capture_output=True, text=True, env=self.env, cwd=self.dir)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("outside", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.wt, "a.json")))


if __name__ == "__main__":
    unittest.main()
