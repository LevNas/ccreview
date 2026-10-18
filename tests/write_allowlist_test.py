"""Tests for scripts/write_allowlist.py: ask-rule matching, deferral, and the allowlist it writes for hooks/gate.py.
Run: python3 tests/write_allowlist_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "write_allowlist.py")
GATE = os.path.join(HERE, "..", "hooks", "gate.py")
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import write_allowlist as wa  # noqa: E402

GIT = ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
       "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


class RuleMatching(unittest.TestCase):
    def m(self, rule, cmd):
        return wa.rule_matches(wa.bash_pattern(rule), cmd)

    def test_wildcard_with_word_boundary(self):
        self.assertTrue(self.m("Bash(rm *)", "rm -f x"))
        self.assertTrue(self.m("Bash(rm *)", "rm"), "a trailing ' *' also covers the bare command")
        self.assertFalse(self.m("Bash(rm *)", "rmdir x"))
        self.assertFalse(self.m("Bash(rm *)", "python3 lint.py rm"))

    def test_legacy_prefix_form(self):
        self.assertTrue(self.m("Bash(git push:*)", "git push origin x"))
        self.assertTrue(self.m("Bash(git push:*)", "git push"))
        self.assertFalse(self.m("Bash(git push:*)", "git pushx"))

    def test_bare_and_star_rules_match_everything(self):
        self.assertTrue(self.m("Bash", "python3 lint.py"))
        self.assertTrue(self.m("Bash(*)", "python3 lint.py"))

    def test_env_prefix_is_ignored(self):
        self.assertTrue(self.m("Bash(npm test *)", "CI=1 npm test --silent"))

    def test_exact_and_inner_wildcards(self):
        self.assertTrue(self.m("Bash(make lint)", "make lint"))
        self.assertFalse(self.m("Bash(make lint)", "make lint-all"))
        self.assertTrue(self.m("Bash(python3 * --fix)", "python3 tools/x.py --fix"))

    def test_rules_on_other_tools_are_skipped(self):
        self.assertIsNone(wa.bash_pattern("Read(./.env)"))
        self.assertIsNone(wa.bash_pattern("WebFetch"))


class WriteAllowlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self.tmp.name)
        self.cfg = os.path.join(self.dir, "_config")
        os.makedirs(self.cfg)
        self.git("init", "-q", "-b", "main")
        self.write("a.py", "x = 1\n")
        self.write("tools/lint.py", "print('ok')\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.write("a.py", "x = 2\n")
        self.write("docs/b.md", "doc\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")
        self.range = f"{self.base}..HEAD"
        self.settings(os.path.join(self.cfg, "settings.json"), ask=["Bash(rm *)"], deny=["Bash(curl *)"])

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *a):
        return subprocess.run([*GIT, *a], cwd=self.dir, capture_output=True, text=True, check=True).stdout.strip()

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def settings(self, path, **perms):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"permissions": perms}, f)

    def run_script(self, *args):
        env = dict(os.environ, CLAUDE_CONFIG_DIR=self.cfg, CCREVIEW_MANAGED_SETTINGS=os.path.join(self.dir, "_none.json"))
        out = subprocess.run([sys.executable, SCRIPT, *args], cwd=self.dir, capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def write_for(self, gates, rounds=2):
        return self.run_script("--range", self.range, "--max-rounds", str(rounds), "--gates", json.dumps(gates))

    def allowlist(self):
        with open(os.path.join(self.dir, ".git", "ccreview", "allowlist.json")) as f:
            return json.load(f)

    def test_allowlist_contents(self):
        out = self.write_for([{"id": "lint", "run": "python3 tools/lint.py a.py", "blocking": True}])
        self.assertEqual(out["allowedGates"], ["lint"])
        self.assertEqual(out["deferred"], [])
        al = self.allowlist()
        self.assertEqual(al["worktree"], self.dir)
        self.assertEqual(sorted(al["files"]), ["a.py", "docs/b.md"])
        self.assertIn("python3 tools/lint.py a.py", al["commands"])
        self.assertIn(f"git diff {self.range}", al["commands"])
        # rounds=2: only the fix agent of round 1 writes a diff, for round 2's reviewers.
        self.assertIn(f"git diff {self.range} --output=.ccreview/diff-r2.patch", al["commands"])
        self.assertFalse(any("diff-r1.patch" in c or "diff-r3.patch" in c for c in al["commands"]))
        self.assertEqual(al["commitMessages"], ["ccreview: round 1 fixes", "ccreview: round 2 fixes"])
        self.assertEqual(al["protected"], ["tools/lint.py"], "the gate's script, not the changed files it checks")

    def test_protected_comes_from_the_template(self):
        self.write("tools/lint.py", "print('changed')\n")
        self.git("commit", "-q", "-am", "the change edits the gate script too")
        out = self.write_for([{"id": "lint", "run": "python3 tools/lint.py a.py tools/lint.py",
                               "template": "python3 tools/lint.py {files}", "blocking": True}])
        self.assertEqual(out["protected"], ["tools/lint.py"], "a script the change edits stays protected")
        self.assertIn("tools/lint.py", self.allowlist()["files"])

    def test_ask_deny_and_metacharacters_defer(self):
        out = self.write_for([
            {"id": "clean", "run": "rm -f build/x", "blocking": True},
            {"id": "fetch", "run": "curl https://example.invalid", "blocking": False},
            {"id": "chain", "run": "make lint && make test", "blocking": True},
            {"id": "ok", "run": "python3 tools/lint.py", "blocking": True},
        ])
        self.assertEqual(out["allowedGates"], ["ok"])
        reasons = {d["id"]: d["reason"] for d in out["deferred"]}
        self.assertIn("ask rule Bash(rm *)", reasons["clean"])
        self.assertIn("deny rule Bash(curl *)", reasons["fetch"])
        self.assertIn("metacharacters", reasons["chain"])
        self.assertNotIn("rm -f build/x", self.allowlist()["commands"])

    def test_project_and_local_settings_are_read(self):
        self.settings(os.path.join(self.dir, ".claude", "settings.local.json"), ask=["Bash(python3 *)"])
        out = self.write_for([{"id": "lint", "run": "python3 tools/lint.py", "blocking": True}])
        self.assertEqual(out["allowedGates"], [])
        self.assertIn("settings.local.json", out["deferred"][0]["reason"])

    def test_unreadable_settings_defer_every_gate(self):
        with open(os.path.join(self.cfg, "settings.json"), "w") as f:
            f.write("{not json")
        out = self.write_for([{"id": "lint", "run": "python3 tools/lint.py", "blocking": True}])
        self.assertEqual(out["allowedGates"], [])
        self.assertEqual(out["deferred"][0]["reason"], "a settings file could not be read")

    def test_remove(self):
        self.write_for([])
        self.assertTrue(self.run_script("--remove")["removed"])
        self.assertFalse(self.run_script("--remove")["removed"])

    def test_remove_deletes_the_round_diffs(self):
        self.write(".ccreview/diff-r1.patch", "d\n")
        self.write(".ccreview/diff-r2.patch", "d\n")
        out = self.run_script("--remove")  # no allowlist: an untrusted run still wrote diff-r1.patch
        self.assertFalse(out["removed"])
        self.assertTrue(out["diffDirRemoved"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".ccreview")))

    def test_remove_leaves_files_it_did_not_write(self):
        self.write(".ccreview/diff-r1.patch", "d\n")
        self.write(".ccreview/notes.txt", "mine\n")
        self.assertFalse(self.run_script("--remove")["diffDirRemoved"])
        self.assertEqual(os.listdir(os.path.join(self.dir, ".ccreview")), ["notes.txt"])

    def test_absolute_diff_dir_is_kept_as_given(self):
        # What the skill passes: the fix agent runs this exact string from whatever cwd it has.
        diff_dir = os.path.join(self.dir, ".ccreview")
        self.run_script("--range", self.range, "--max-rounds", "2", "--diff-dir", diff_dir + "/")
        self.assertIn(f"git diff {self.range} --output={diff_dir}/diff-r2.patch", self.allowlist()["commands"])
        self.write(".ccreview/diff-r2.patch", "d\n")
        self.assertTrue(self.run_script("--remove", "--diff-dir", diff_dir)["diffDirRemoved"])

    def test_diff_dir_outside_the_worktree_is_refused(self):
        env = dict(os.environ, CLAUDE_CONFIG_DIR=self.cfg)
        os.symlink("docs", os.path.join(self.dir, "linked"))
        # A space would split --output=<path> into two tokens the hook still allows; git then writes the prefix.
        for bad in ("../elsewhere", "/tmp/x", self.dir, "", "my diffs", "d'x", "d$x", "linked"):
            out = subprocess.run([sys.executable, SCRIPT, "--remove", "--diff-dir", bad],
                                 cwd=self.dir, capture_output=True, text=True, env=env)
            self.assertEqual(out.returncode, 2, bad)

    def test_fix_agent_writes_the_diff_with_git_and_cannot_touch_it(self):
        self.write_for([], rounds=3)
        self.assertEqual(self.gate("Bash", {"command": f"git diff {self.range} --output=.ccreview/diff-r2.patch"}), "allow")
        self.assertEqual(self.gate("Bash", {"command": f"git diff {self.range} --output=a.py"}), "deny")
        self.assertEqual(self.gate("Bash", {"command": f"git diff {self.range} --output=.ccreview/diff-r1.patch"}), "deny",
                         "round 1's diff is the skill's")
        # git does not create the directory (exit 128): the skill makes it before writing diff-r1.patch.
        with self.assertRaises(subprocess.CalledProcessError):
            self.git("diff", self.range, "--output=.ccreview/diff-r2.patch")
        os.makedirs(os.path.join(self.dir, ".ccreview"))
        # With the directory there, git writes exactly what `git diff <range>` prints.
        self.git("diff", self.range, "--output=.ccreview/diff-r2.patch")
        with open(os.path.join(self.dir, ".ccreview", "diff-r2.patch")) as f:
            self.assertEqual(f.read().rstrip("\n"), self.git("diff", self.range))
        self.assertEqual(self.gate("Bash", {"command": "git add -- .ccreview/diff-r2.patch"}), "deny")
        self.assertEqual(self.gate("Edit", {"file_path": os.path.join(self.dir, ".ccreview", "diff-r2.patch")}), "deny")
        self.assertEqual(self.gate("Write", {"file_path": os.path.join(self.dir, ".ccreview", "diff-r3.patch"),
                                             "content": "a transcribed diff"}), "deny")
        self.assertIn("?? .ccreview/", self.git("status", "--short"), "untracked, so --remove must clean it up")

    def gate(self, tool, tool_input):
        payload = json.dumps({"cwd": self.dir, "agent_type": "ccreview:ccreview-fixer",
                              "tool_name": tool, "tool_input": tool_input})
        env = {k: v for k, v in os.environ.items() if k != "CCREVIEW_ALLOWLIST"}
        out = subprocess.run([sys.executable, GATE], input=payload, capture_output=True, text=True, env=env)
        return json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"]

    def test_the_hook_accepts_what_was_written(self):
        self.write_for([{"id": "lint", "run": "python3 tools/lint.py a.py",
                         "template": "python3 tools/lint.py {files}", "blocking": True}])

        def gate(tool, tool_input):
            payload = json.dumps({"cwd": self.dir, "agent_type": "ccreview:ccreview-fixer",
                                  "tool_name": tool, "tool_input": tool_input})
            env = {k: v for k, v in os.environ.items() if k != "CCREVIEW_ALLOWLIST"}
            out = subprocess.run([sys.executable, GATE], input=payload, capture_output=True, text=True, env=env)
            return json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"]

        self.assertEqual(gate("Bash", {"command": "python3 tools/lint.py a.py"}), "allow")
        self.assertEqual(gate("Bash", {"command": "git add -- docs/b.md"}), "allow")
        self.assertEqual(gate("Bash", {"command": 'git commit -m "ccreview: round 1 fixes"'}), "allow")
        self.assertEqual(gate("Edit", {"file_path": os.path.join(self.dir, "docs/b.md")}), "allow")
        self.assertEqual(gate("Edit", {"file_path": os.path.join(self.dir, "tools/lint.py")}), "deny")
        self.assertEqual(gate("Edit", {"file_path": os.path.join(self.dir, "a.py")}), "allow",
                         "a changed file the gate checks is what the fix agent is there to fix")


if __name__ == "__main__":
    unittest.main()
