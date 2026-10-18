"""Tests for hooks/gate.py: feed PreToolUse inputs for a throwaway git repository and compare the decision.
Run: python3 tests/gate_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "gate.py")
FIXER = "ccreview:ccreview-fixer"
RANGE = "abc123..HEAD"


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self.tmp.name)
        subprocess.run(["git", "init", "-q", self.dir], check=True)
        for rel in ["a.py", "docs/b.md", "tools/lint.py", ".claude/settings.json", "sub/.gitattributes",
                    "plugin/hooks/gate.py"]:
            self.write(rel, "x\n")
        os.symlink(os.path.join(self.dir, ".git", "config"), os.path.join(self.dir, "link.md"))
        self.allowlist = {
            "version": 1, "worktree": self.dir, "range": RANGE,
            "files": ["a.py", "docs/b.md", "tools/lint.py", ".claude/settings.json", "sub/.gitattributes",
                      "link.md", "gone.py"],
            "commands": ["git status", "git status --short", f"git diff {RANGE}", "python3 tools/lint.py a.py"],
            "commitMessages": ["ccreview: round 1 fixes", "ccreview: round 2 fixes"],
            "protected": ["tools/lint.py"],
            "expires": time.time() + 3600,
        }
        self.save()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def save(self):
        path = os.path.join(self.dir, ".git", "ccreview", "allowlist.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.allowlist, f)

    def run_gate(self, payload, env=None, cwd=None):
        if isinstance(payload, dict):
            payload = json.dumps({"cwd": cwd or self.dir, **payload})
        base = {k: v for k, v in os.environ.items() if k not in ("CCREVIEW_ALLOWLIST", "CLAUDE_PLUGIN_DATA")}
        env = dict(base, CLAUDE_PLUGIN_ROOT=os.path.join(self.dir, "plugin"), **(env or {}))
        out = subprocess.run([sys.executable, GATE], input=payload, capture_output=True, text=True, env=env,
                             cwd=self.dir)
        self.assertEqual(out.returncode, 0, out.stderr)
        if not out.stdout.strip():
            return None, None
        o = json.loads(out.stdout)["hookSpecificOutput"]
        return o["permissionDecision"], o["permissionDecisionReason"]

    def bash(self, cmd, agent=FIXER, **kw):
        return self.run_gate({"agent_type": agent, "tool_name": "Bash", "tool_input": {"command": cmd}}, **kw)[0]

    def edit(self, path, tool="Edit", agent=FIXER, **kw):
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        return self.run_gate({"agent_type": agent, "tool_name": tool, "tool_input": {key: path}}, **kw)[0]

    # --- who is judged ---------------------------------------------------------------

    def test_other_agents_and_main_get_no_decision(self):
        self.assertIsNone(self.bash("rm -rf /", agent="Explore"))
        self.assertIsNone(self.bash("rm -rf /", agent="ccreview:ccreview-reviewer"))
        self.assertIsNone(self.bash("rm -rf /", agent=""))
        self.assertIsNone(self.edit(os.path.join(self.dir, ".git", "config"), agent=""))

    def test_project_type_name_without_namespace_is_judged(self):
        self.assertEqual(self.bash("git status", agent="ccreview-fixer"), "allow")

    # --- Bash ---------------------------------------------------------------------------

    def test_listed_commands(self):
        self.assertEqual(self.bash("git status --short"), "allow")
        self.assertEqual(self.bash(f"git diff {RANGE}"), "allow")
        self.assertEqual(self.bash("python3  tools/lint.py   a.py"), "allow", "token match, not string match")

    def test_unlisted_and_extended_commands(self):
        self.assertEqual(self.bash("git branch --show-current"), "deny")
        self.assertEqual(self.bash("python3 tools/lint.py a.py docs/b.md"), "deny")
        self.assertEqual(self.bash("python3 -c 'print(1)'"), "deny")
        self.assertEqual(self.bash("git push"), "deny")
        self.assertEqual(self.bash("git -c core.hooksPath=/tmp commit -m 'ccreview: round 1 fixes'"), "deny")

    def test_shell_metacharacters(self):
        for cmd in ["git status && rm -rf x", "git status; ls", "git status | cat", "git status > out",
                    "git diff $(echo x)", "git diff `echo x`", "git status\nls"]:
            self.assertEqual(self.bash(cmd), "deny", cmd)

    def test_git_add(self):
        self.assertEqual(self.bash("git add -- a.py docs/b.md"), "allow")
        self.assertEqual(self.bash("git add -- ./a.py"), "allow")
        self.assertEqual(self.bash("git add -- a.py notes.txt"), "deny", "a file outside the range")
        self.assertEqual(self.bash("git add -- tools/lint.py"), "deny", "a protected file")
        self.assertEqual(self.bash("git add -- *.py"), "deny", "a glob the shell would expand")
        self.assertEqual(self.bash("git add a.py"), "deny", "the -- form only")
        self.assertEqual(self.bash("git add -A"), "deny")
        self.assertEqual(self.bash("git add -- ../outside.py"), "deny")

    def test_git_commit(self):
        self.assertEqual(self.bash('git commit -m "ccreview: round 2 fixes"'), "allow")
        self.assertEqual(self.bash('git commit -m "something else"'), "deny")
        self.assertEqual(self.bash('git commit --no-verify -m "ccreview: round 1 fixes"'), "deny")
        self.assertEqual(self.bash('git commit -m "ccreview: round 1 fixes" --no-gpg-sign'), "deny")

    def test_deny_reason_names_the_allowed_forms(self):
        _, reason = self.run_gate({"agent_type": FIXER, "tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertIn("`git status`", reason)
        self.assertIn("Do not retry", reason)

    # --- Edit / Write / NotebookEdit -------------------------------------------------------

    def test_changed_files_are_editable(self):
        self.assertEqual(self.edit(os.path.join(self.dir, "a.py")), "allow")
        self.assertEqual(self.edit("docs/b.md", tool="Write"), "allow", "relative to cwd")
        self.assertEqual(self.edit("b.md", cwd=os.path.join(self.dir, "docs")), "allow")
        self.assertEqual(self.edit("a.py", tool="NotebookEdit"), "allow")

    def test_files_outside_the_range(self):
        self.write("c.py", "x\n")
        self.assertEqual(self.edit("c.py"), "deny")
        self.assertEqual(self.edit("new.py", tool="Write"), "deny", "new files")
        self.assertEqual(self.edit("gone.py", tool="Write"), "deny", "a file the range deleted is not recreated")
        self.assertEqual(self.edit("/etc/hosts"), "deny")
        self.assertEqual(self.edit("../x.py"), "deny")

    def test_git_internals_and_protected_paths(self):
        self.assertEqual(self.edit(".git/hooks/pre-commit", tool="Write"), "deny")
        self.assertEqual(self.edit(".git/config"), "deny")
        self.assertEqual(self.edit("link.md"), "deny", "a changed file that is a symlink into .git")
        self.assertEqual(self.edit(".claude/settings.json"), "deny", "settings, even when changed")
        self.assertEqual(self.edit("sub/.gitattributes"), "deny")
        self.assertEqual(self.edit("tools/lint.py"), "deny", "a script a command gate runs")

    def test_rules_claude_md_and_gate_declarations_are_protected(self):
        # Files the session loads or the review checks against: protected even when the range changed them.
        paths = [".claude/rules/tone.md", ".claude/rules/sub/x.md", "plugins/p/.claude/rules/r.md",
                 "CLAUDE.md", "docs/CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md", "a/CLAUDE.local.md",
                 "REVIEW.md", ".claude/review-gates.yaml"]
        for rel in paths:
            self.write(rel, "x\n")
        self.allowlist["files"].extend(paths)
        self.save()
        for rel in paths:
            self.assertEqual(self.edit(rel), "deny", rel)
            self.assertEqual(self.bash(f"git add -- {rel}"), "deny", rel)
        _, reason = self.run_gate({"agent_type": FIXER, "tool_name": "Edit", "tool_input": {"file_path": "REVIEW.md"}})
        self.assertIn("skipped", reason)

    def test_staging_a_protected_file_gives_the_protected_reason(self):
        # e2e D5: `git add -- tools/invsync/CLAUDE.md` (in the range, protected) was told the file was not in
        # the range. A protected file gets the reason its Edit gets; a file outside the range keeps its own.
        self.write("docs/CLAUDE.md", "x\n")
        self.allowlist["files"].append("docs/CLAUDE.md")
        self.save()
        cmd = {"agent_type": FIXER, "tool_name": "Bash"}
        decision, reason = self.run_gate({**cmd, "tool_input": {"command": "git add -- docs/CLAUDE.md"}})
        self.assertEqual(decision, "deny")
        self.assertIn("is protected", reason)
        self.assertIn("skipped", reason)
        self.assertNotIn("only files changed in the review range", reason)
        _, edit_reason = self.run_gate({"agent_type": FIXER, "tool_name": "Edit", "tool_input": {"file_path": "docs/CLAUDE.md"}})
        sentence = reason[reason.index("is protected"):]
        self.assertTrue(edit_reason.endswith(sentence), "the same sentence as the Edit denial")
        _, outside = self.run_gate({**cmd, "tool_input": {"command": "git add -- notes.txt"}})
        self.assertIn("only files changed in the review range", outside)
        self.assertNotIn("protected", outside)
        _, both = self.run_gate({**cmd, "tool_input": {"command": "git add -- a.py docs/CLAUDE.md notes.txt"}})
        self.assertIn("cannot stage docs/CLAUDE.md: it is protected", both)
        self.assertIn("cannot stage notes.txt: only files changed", both)
        self.assertNotIn("a.py", both, "a stageable file is not named")

    def test_names_close_to_the_protected_ones_stay_editable(self):
        # REVIEW.md and review-gates.yaml are protected at the root only, where ccreview reads them.
        paths = ["docs/REVIEW.md", "sub/REVIEW.md", "sub/.claude/review-gates.yaml", "my-CLAUDE.md",
                 "docs/rules/x.md", ".claude/review-gates.yaml.bak", ".claude/tasks/x/readme.md"]
        for rel in paths:
            self.write(rel, "x\n")
        self.allowlist["files"].extend(paths)
        self.save()
        for rel in paths:
            self.assertEqual(self.edit(rel), "allow", rel)

    def test_plugin_root_is_protected(self):
        self.allowlist["files"].append("plugin/hooks/gate.py")
        self.save()
        self.assertEqual(self.edit("plugin/hooks/gate.py"), "deny")

    # --- fail-closed ---------------------------------------------------------------------------

    def test_missing_expired_or_broken_allowlist(self):
        self.allowlist["expires"] = time.time() - 1
        self.save()
        self.assertEqual(self.bash("git status"), "deny")
        self.allowlist["expires"] = time.time() + 3600
        self.allowlist["version"] = 2
        self.save()
        self.assertEqual(self.bash("git status"), "deny")
        os.remove(os.path.join(self.dir, ".git", "ccreview", "allowlist.json"))
        self.assertEqual(self.bash("git status"), "deny")
        self.assertEqual(self.edit("a.py"), "deny")

    def test_cwd_outside_the_worktree(self):
        other = tempfile.TemporaryDirectory()
        try:
            self.assertEqual(self.bash("git status", cwd=other.name), "deny", "not a git repository")
        finally:
            other.cleanup()

    def test_forced_failure_and_unparsable_input(self):
        self.assertEqual(self.bash("git status", env={"CCREVIEW_GATE_BREAK": "1"}), "deny")
        self.assertIsNone(self.bash("git status", agent="Explore", env={"CCREVIEW_GATE_BREAK": "1"}))
        self.assertEqual(self.run_gate('{"agent_type": "ccreview:ccreview-fixer", broken')[0], "deny")
        self.assertIsNone(self.run_gate("{broken")[0])

    def test_decisions_are_logged_next_to_the_allowlist(self):
        self.bash("git status")
        self.bash("ls")
        self.assertEqual([e["decision"] for e in self.log(os.path.join(self.dir, ".git", "ccreview"))], ["allow", "deny"])

    # --- the decision log (decision 27: e2e session C lost seven fail-closed denies) ------

    def log(self, directory):
        with open(os.path.join(directory, "hook-decisions.jsonl")) as f:
            return [json.loads(line) for line in f]

    def test_forced_failure_is_logged_next_to_the_allowlist(self):
        self.assertEqual(self.bash("git status", env={"CCREVIEW_GATE_BREAK": "1"}), "deny")
        os.remove(os.path.join(self.dir, ".git", "ccreview", "allowlist.json"))
        self.assertEqual(self.edit("a.py"), "deny")
        got = self.log(os.path.join(self.dir, ".git", "ccreview"))
        self.assertEqual([(e["tool"], e["decision"]) for e in got], [("Bash", "deny"), ("Edit", "deny")])
        self.assertIn("the gate hook failed (RuntimeError)", got[0]["reason"])
        self.assertIn("no allowlist for this run", got[1]["reason"])

    def test_no_git_dir_logs_to_the_plugin_data_directory(self):
        data, other = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        try:
            decision, reason = self.run_gate({"agent_type": FIXER, "tool_name": "Bash", "tool_input": {"command": "ls"}},
                                             cwd=other.name, env={"CLAUDE_PLUGIN_DATA": data.name})
            self.assertEqual(decision, "deny")
            self.assertNotIn("not logged", reason)
            [entry] = self.log(data.name)
            self.assertEqual((entry["cwd"], entry["input"], entry["decision"]), (other.name, "ls", "deny"))
        finally:
            data.cleanup()
            other.cleanup()

    def test_an_unwritable_log_falls_back_to_the_plugin_data_directory(self):
        os.makedirs(os.path.join(self.dir, ".git", "ccreview", "hook-decisions.jsonl"))  # a directory: open() fails
        data = tempfile.TemporaryDirectory()
        try:
            self.assertEqual(self.bash("git status", env={"CLAUDE_PLUGIN_DATA": data.name}), "allow")
            self.assertEqual(self.log(data.name)[0]["decision"], "allow")
        finally:
            data.cleanup()

    def test_a_decision_nowhere_to_log_says_so_in_its_reason(self):
        other = tempfile.TemporaryDirectory()
        try:
            decision, reason = self.run_gate({"agent_type": FIXER, "tool_name": "Bash", "tool_input": {"command": "ls"}},
                                             cwd=other.name)
        finally:
            other.cleanup()
        self.assertEqual(decision, "deny")
        self.assertIn("This decision was not logged: no git dir and no CLAUDE_PLUGIN_DATA", reason)

    def test_unparsable_fixer_input_is_logged(self):
        data = tempfile.TemporaryDirectory()
        try:
            self.assertEqual(self.run_gate('{"agent_type": "ccreview:ccreview-fixer", broken',
                                           env={"CLAUDE_PLUGIN_DATA": data.name})[0], "deny")
            self.assertEqual(self.log(data.name)[0]["input"], "(unparsable hook input)")
        finally:
            data.cleanup()

    def test_a_log_write_error_still_gives_a_decision(self):
        # A lone surrogate cannot be encoded as UTF-8; the hook must still answer (exit 1 = no decision = not denied).
        self.assertEqual(self.bash("ls \ud800"), "deny")
        self.assertEqual(self.log(os.path.join(self.dir, ".git", "ccreview"))[0]["decision"], "deny")

    def test_another_repository_as_cwd_is_not_written_to(self):
        other, data = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        try:
            subprocess.run(["git", "init", "-q", other.name], check=True)
            self.assertEqual(self.bash("ls", cwd=other.name, env={"CLAUDE_PLUGIN_DATA": data.name}), "deny")
            self.assertFalse(os.path.exists(os.path.join(other.name, ".git", "ccreview")))
            self.assertEqual(self.log(data.name)[0]["cwd"], other.name)
        finally:
            other.cleanup()
            data.cleanup()

    def test_a_relative_plugin_data_directory_is_not_used(self):
        other = tempfile.TemporaryDirectory()
        try:
            _, reason = self.run_gate({"agent_type": FIXER, "tool_name": "Bash", "tool_input": {"command": "ls"}},
                                      cwd=other.name, env={"CLAUDE_PLUGIN_DATA": "reldata"})
        finally:
            other.cleanup()
        self.assertIn("This decision was not logged", reason)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "reldata")), "the hook runs in the project")

    def test_other_agents_are_not_logged(self):
        self.bash("git status", agent="Explore")
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".git", "ccreview", "hook-decisions.jsonl")))


if __name__ == "__main__":
    unittest.main()
