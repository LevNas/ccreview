"""Tests for scripts/check_pr_body.py.
Run: python3 tests/check_pr_body_test.py
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "check_pr_body.py")
REQ = ["--require", "Evidence|確かめたこと", "--require", "Not tested|試していない範囲"]

GOOD = """## Summary

Adds a gate.

## Evidence

- `python3 tests/x.py` -> OK

### Not tested

- GitLab merge requests.
"""


class CheckPrBody(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def run_body(self, body, *args, env=None):
        path = os.path.join(self.dir, "body.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return self.run_args("--body-file", path, *args, env=env)

    def run_args(self, *args, env=None):
        r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env)
        out = json.loads(r.stdout) if r.stdout.strip() else None
        return r.returncode, out, r.stderr

    def test_all_present(self):
        code, out, _ = self.run_body(GOOD, *REQ, "--id", "pr-evidence")
        self.assertEqual(code, 0)
        self.assertEqual(out, {"id": "pr-evidence", "ok": True, "missing": [], "empty": [], "source": "file"})

    def test_missing_section(self):
        code, out, _ = self.run_body(GOOD.replace("### Not tested\n\n- GitLab merge requests.\n", ""), *REQ)
        self.assertEqual(code, 1)
        self.assertEqual(out["missing"], ["Not tested|試していない範囲"])
        self.assertEqual(out["empty"], [])

    def test_alias_case_and_level(self):
        body = "# 確かめたこと\n\n- ran it\n\n#### NOT TESTED ##\n\n- other OS\n"
        code, out, _ = self.run_body(body, *REQ)
        self.assertEqual(code, 0, out)

    def test_parent_text_does_not_include_child(self):
        # Evidence holds only its child's text, so Evidence itself is empty.
        body = "## Evidence\n\n### Not tested\n\n- other OS\n"
        code, out, _ = self.run_body(body, *REQ)
        self.assertEqual(code, 1)
        self.assertEqual(out["empty"], ["Evidence|確かめたこと"])

    def test_empty_forms(self):
        for filler in ("\n   \n", "<!-- fill in -->\n", "<!--\nmulti\nline\n-->\n",
                       "- <the result you observed>\n", "<what this PR does not show working>\n"):
            with self.subTest(filler=filler):
                body = f"## Evidence\n\n- ran it\n\n### Not tested\n\n{filler}"
                code, out, _ = self.run_body(body, *REQ)
                self.assertEqual(code, 1)
                self.assertEqual(out["empty"], ["Not tested|試していない範囲"])

    def test_more_empty_forms(self):
        # bare list markers, an unchecked box, a stray comment end, zero-width and no-break spaces, a lone HTML tag
        for filler in ("-\n*\n1.\n", "- [ ]\n", "-->\n", "​⁠\n", " \n", "<details>\n</details>\n"):
            with self.subTest(filler=filler):
                body = f"## Evidence\n\n- ran it\n\n### Not tested\n\n{filler}"
                code, out, _ = self.run_body(body, *REQ)
                self.assertEqual(code, 1)
                self.assertEqual(out["empty"], ["Not tested|試していない範囲"])

    def test_image_and_autolink_count_as_text(self):
        for evidence in ('<img width="600" src="https://example.com/shot.png">', "<https://example.com/run/42>"):
            with self.subTest(evidence=evidence):
                body = f"## Evidence\n\n{evidence}\n\n### Not tested\n\n- other OS\n"
                code, out, _ = self.run_body(body, *REQ)
                self.assertEqual(code, 0, out)

    def test_commented_out_sections_are_not_there(self):
        # a template's commented-out sections, closed and unclosed
        for body in ("<!--\n## Evidence\nx\n## Not tested\ny\n-->\n",
                     "## Evidence\n\n- ran it\n\n<!--\n## Not tested\n- y\n-->\n",
                     "## Evidence\n\n- ran it\n\n<!--\n## Not tested\n- y\n"):
            with self.subTest(body=body):
                code, out, _ = self.run_body(body, *REQ)
                self.assertEqual(code, 1)
                self.assertIn("Not tested|試していない範囲", out["missing"])

    def test_heading_name_forms(self):
        body = "## **Evidence**\n\n- ran it\n\n## _Not tested_:\n\n- other OS\n"
        code, out, _ = self.run_body(body, *REQ)
        self.assertEqual(code, 0, out)
        # closing #s go only after a space: "C#" keeps its #
        code, out, _ = self.run_body("## C#\n\n- x\n", "--require", "C#")
        self.assertEqual(code, 0, out)
        code, out, _ = self.run_body("## Evidence ##\n\n- x\n", "--require", "Evidence")
        self.assertEqual(code, 0, out)

    def test_setext_heading_is_not_a_section(self):
        code, out, _ = self.run_body("Evidence\n--------\n\n- ran it\n", "--require", "Evidence")
        self.assertEqual(code, 1)
        self.assertEqual(out["missing"], ["Evidence"])

    def test_fence_closer_with_info_string_does_not_close(self):
        body = "## Summary\n\n```\n```x\n## Evidence\n- y\n### Not tested\n- z\n```\n"
        code, out, _ = self.run_body(body, *REQ)
        self.assertEqual(code, 1)
        self.assertEqual(len(out["missing"]), 2)

    def test_crafted_lines_finish_quickly(self):
        import time
        body = "# " + " \t" * 30000 + "x\n" + "<!--" * 20000 + "\n"
        start = time.monotonic()
        code, _, _ = self.run_body(body, *REQ)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(code, 1)

    def test_body_over_limit(self):
        code, out, err = self.run_body("## Evidence\n\n" + "x" * 200_001, *REQ)
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        self.assertIn("over", err)

    def test_heading_inside_code_fence_is_not_a_section(self):
        body = "## Summary\n\n```markdown\n## Evidence\n- x\n### Not tested\n- y\n```\n"
        code, out, _ = self.run_body(body, *REQ)
        self.assertEqual(code, 1)
        self.assertEqual(out["missing"], ["Evidence|確かめたこと", "Not tested|試していない範囲"])

    def test_one_filled_duplicate_is_enough(self):
        body = "## Evidence\n\n## Evidence\n\n- ran it\n\n## Not tested\n\n- none left out: docs only\n"
        code, _, _ = self.run_body(body, *REQ)
        self.assertEqual(code, 0)

    def test_empty_body(self):
        code, out, _ = self.run_body("", *REQ)
        self.assertEqual(code, 1)
        self.assertEqual(len(out["missing"]), 2)

    def test_output_never_copies_the_body(self):
        secret = "ghp_" + "x" * 36
        code, out, err = self.run_body(f"## Evidence\n\n{secret}\n", *REQ)
        self.assertEqual(code, 1)
        self.assertNotIn(secret, json.dumps(out) + err)

    def test_usage_errors(self):
        path = os.path.join(self.dir, "b.md")
        open(path, "w").close()
        for args in (REQ,                                         # no source
                     ["--pr", "1", "--body-file", path, *REQ],    # both sources
                     ["--pr", "0", *REQ],                         # not positive
                     ["--pr", "x", *REQ],                         # not a number
                     ["--body-file", path, "--repo", "o/r", *REQ],  # --repo without --pr
                     ["--body-file", path],                       # no --require
                     ["--body-file", path, "--require", " | "]):   # no heading name
            with self.subTest(args=args):
                code, out, _ = self.run_args(*args)
                self.assertEqual(code, 2)
                self.assertIsNone(out)

    def test_unreadable_file(self):
        code, out, err = self.run_args("--body-file", os.path.join(self.dir, "nope.md"), *REQ)
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def fake_gh(self, script):
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir, exist_ok=True)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as f:
            f.write("#!/bin/sh\n" + script)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC)
        with open(os.path.join(self.dir, "args"), "w"):
            pass
        return dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"], ARGS=os.path.join(self.dir, "args"))

    def test_pr_reads_body_through_gh(self):
        env = self.fake_gh('echo "$@" > "$ARGS"\n'
                           "printf '%s' '{\"body\": \"## Evidence\\n\\n- ran\\n\\n### Not tested\\n\\n- other OS\\n\"}'\n")
        code, out, _ = self.run_args("--pr", "7", "--repo", "o/r", *REQ, env=env)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["source"], "pr")
        with open(os.path.join(self.dir, "args")) as f:
            self.assertEqual(f.read().split(), ["pr", "view", "7", "--json", "body", "-R", "o/r"])

    def test_pr_null_body(self):
        env = self.fake_gh("echo '{\"body\": null}'\n")
        code, out, _ = self.run_args("--pr", "7", *REQ, env=env)
        self.assertEqual(code, 1)
        self.assertEqual(len(out["missing"]), 2)

    def test_gh_failure(self):
        env = self.fake_gh("printf 'no pull requests found\\033[31m\\nsecond line\\n' >&2\nexit 1\n")
        code, out, err = self.run_args("--pr", "7", *REQ, env=env)
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        self.assertIn("exited 1: no pull requests found?[31m", err)
        self.assertNotIn("\033", err)
        self.assertNotIn("second line", err)

    def test_gh_not_json(self):
        env = self.fake_gh("echo 'not json'\n")
        code, _, err = self.run_args("--pr", "7", *REQ, env=env)
        self.assertEqual(code, 2)
        self.assertIn("did not return", err)

    # Decision 47: the skill runs the same check on a spec file before adding the spec-scope gate.
    SPEC_REQ = ["--require", "Out of Scope|範囲外", "--id", "spec-scope"]

    def test_spec_out_of_scope_present(self):
        spec = "# Plan\n\n## Approach\n\nDo X.\n\n## Out of Scope\n\n- Y: decided later.\n\n## Verification\n\n- tests\n"
        code, out, _ = self.run_body(spec, *self.SPEC_REQ)
        self.assertEqual(code, 0)
        self.assertEqual(out, {"id": "spec-scope", "ok": True, "missing": [], "empty": [], "source": "file"})

    def test_spec_japanese_heading(self):
        code, out, _ = self.run_body("# 計画\n\n## 範囲外\n\n- Y はやらない（あとで決める）\n", *self.SPEC_REQ)
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])

    def test_spec_heading_missing_or_partial(self):
        for spec in ("# Plan\n\n## Approach\n\nDo X.\n",
                     "# 計画\n\n## 範囲外（やらないこと）\n\n- Y\n"):  # names match exactly, not by part
            code, out, _ = self.run_body(spec, *self.SPEC_REQ)
            self.assertEqual(code, 1, spec)
            self.assertEqual(out["missing"], ["Out of Scope|範囲外"], spec)

    def test_spec_section_empty(self):
        code, out, _ = self.run_body("# Plan\n\n## Out of Scope\n\n<!-- none yet -->\n\n## Verification\n\n- tests\n", *self.SPEC_REQ)
        self.assertEqual(code, 1)
        self.assertEqual(out["empty"], ["Out of Scope|範囲外"])


if __name__ == "__main__":
    unittest.main()
