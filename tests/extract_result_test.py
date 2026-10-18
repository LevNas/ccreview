"""Tests for scripts/extract_result.py.
Run: python3 tests/extract_result_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "extract_result.py")
GIT = ["git", "-c", "core.hooksPath=/dev/null"]

RESULT = {"verdict": "fail", "stopReason": "same reason failed twice (fix did not hold): g|a.py|correctness",
          "trust": "trusted", "notRun": [], "deferred": [], "rounds": [{"round": 1, "keys": ["g|a.py|correctness"]}],
          "remaining": [{"gate": "g", "path": "a.py", "line": 1, "severity": "blocking"}],
          "preExisting": [], "introduced": [{"round": 1, "gate": "g", "path": "a.py", "line": 1}],
          "outOfRange": [], "disputed": [], "confirmations": [], "repeatMismatches": [], "unexpectedFiles": []}


def wrapper(result):
    # The shape of the workflow's output file in e2e D8 (keys as found there; values trimmed).
    return {"summary": "Merge-time review gate", "agentCount": 2, "logs": ["round 1: failing keys=1"],
            "result": result, "workflowProgress": [], "totalTokens": 1, "totalToolCalls": 1}


class ExtractResult(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.out = os.path.join(self.dir, "result.json")
        inside = subprocess.run([*GIT, "rev-parse", "--show-toplevel"], cwd=self.dir, capture_output=True, text=True)
        if inside.returncode == 0:
            self.tmp.cleanup()
            self.skipTest("the temporary directory is inside a git repository; --out would be refused there")

    def tearDown(self):
        self.tmp.cleanup()

    def write_output(self, data, raw=False):
        path = os.path.join(self.dir, "task.output")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(data if raw else json.dumps(data, ensure_ascii=False, indent=2))
        return path

    def run_script(self, *args, cwd=None):
        return subprocess.run([sys.executable, SCRIPT, *args], cwd=cwd or self.dir, capture_output=True, text=True)

    def extract(self, output, out=None, cwd=None):
        return self.run_script("--output", output, "--out", out or self.out, cwd=cwd)

    def written(self):
        with open(self.out, encoding="utf-8") as fh:
            return json.load(fh)

    def test_wrapped_object(self):
        r = self.extract(self.write_output(wrapper(RESULT)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.written(), RESULT)
        summary = json.loads(r.stdout)
        self.assertEqual(summary["source"], "wrapped")
        self.assertEqual(summary["verdict"], "fail")
        self.assertEqual(summary["rounds"], 1)
        self.assertEqual(summary["remaining"], 1)
        self.assertEqual(summary["introduced"], 1)
        self.assertEqual(summary["preExisting"], 0)
        self.assertEqual(summary["out"], os.path.realpath(self.out))

    def test_wrapped_json_string(self):
        r = self.extract(self.write_output(wrapper(json.dumps(RESULT))))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.written(), RESULT)

    def test_bare_result_is_accepted(self):
        r = self.extract(self.write_output(RESULT))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.written(), RESULT)
        self.assertEqual(json.loads(r.stdout)["source"], "bare")

    def test_absent_lists_are_null(self):
        r = self.extract(self.write_output(wrapper({"verdict": "pass", "rounds": []})))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(json.loads(r.stdout)["remaining"])

    def test_no_result_key_names_keys_not_values(self):
        data = wrapper(RESULT)
        del data["result"]
        data["output"] = {"note": "VALUE-MUST-NOT-PRINT"}
        r = self.extract(self.write_output(data))
        self.assertEqual(r.returncode, 2)
        self.assertIn("'output'", r.stderr)
        self.assertNotIn("VALUE-MUST-NOT-PRINT", r.stderr + r.stdout)
        self.assertFalse(os.path.exists(self.out))

    def test_result_without_verdict(self):
        r = self.extract(self.write_output(wrapper({"rounds": [], "summary": "VALUE-MUST-NOT-PRINT"})))
        self.assertEqual(r.returncode, 2)
        self.assertIn("verdict", r.stderr)
        self.assertNotIn("VALUE-MUST-NOT-PRINT", r.stderr + r.stdout)
        self.assertFalse(os.path.exists(self.out))

    def test_result_string_not_json(self):
        r = self.extract(self.write_output(wrapper("not json")))
        self.assertEqual(r.returncode, 2)
        self.assertIn("string that is not JSON", r.stderr)
        self.assertFalse(os.path.exists(self.out))

    def test_top_level_array_is_refused(self):
        r = self.extract(self.write_output([RESULT]))
        self.assertEqual(r.returncode, 2)
        self.assertIn("JSON list", r.stderr)
        self.assertFalse(os.path.exists(self.out))

    def test_not_utf8_is_refused(self):
        path = os.path.join(self.dir, "task.output")
        with open(path, "wb") as fh:
            fh.write(b'{"result": "\xff\xfe"}')
        r = self.extract(path)
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot read --output", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_deep_nesting_is_refused_without_traceback(self):
        r = self.extract(self.write_output("[" * 100000 + "]" * 100000, raw=True))
        self.assertEqual(r.returncode, 2)
        self.assertIn("nested too deeply", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.exists(self.out))

    def test_out_same_as_output_is_refused(self):
        output = self.write_output(wrapper(RESULT))
        r = self.extract(output, out=output)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--out is the --output file", r.stderr)
        with open(output, encoding="utf-8") as fh:
            self.assertIn("agentCount", json.load(fh))

    def test_truncated_notification_text_is_refused(self):
        # What the task-notification carries inline: the start of the result, then a truncation note.
        text = json.dumps(RESULT)[:40] + "\n... (truncated 100 chars, full result in /x/tasks/t.output)"
        r = self.extract(self.write_output(text, raw=True))
        self.assertEqual(r.returncode, 2)
        self.assertIn("not one JSON value", r.stderr)

    def test_relative_paths_are_refused(self):
        self.write_output(wrapper(RESULT))
        r = self.run_script("--output", "task.output", "--out", self.out)
        self.assertEqual(r.returncode, 2)
        r = self.run_script("--output", os.path.join(self.dir, "task.output"), "--out", "result.json")
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(self.out))

    def test_out_inside_the_repository_is_refused(self):
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        subprocess.run([*GIT, "init", "-q"], cwd=repo, check=True)
        inside = os.path.join(repo, "result.json")
        r = self.extract(self.write_output(wrapper(RESULT)), out=inside, cwd=repo)
        self.assertEqual(r.returncode, 2)
        self.assertIn("inside the repository", r.stderr)
        self.assertFalse(os.path.exists(inside))

    def test_symlink_into_the_repository_is_refused(self):
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        subprocess.run([*GIT, "init", "-q"], cwd=repo, check=True)
        link = os.path.join(self.dir, "link")
        os.symlink(repo, link)
        r = self.extract(self.write_output(wrapper(RESULT)), out=os.path.join(link, "result.json"), cwd=repo)
        self.assertEqual(r.returncode, 2)
        self.assertIn("inside the repository", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(repo, "result.json")))

    def test_out_not_writable(self):
        out = os.path.join(self.dir, "absent-dir", "result.json")
        r = self.extract(self.write_output(wrapper(RESULT)), out=out)
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot write --out", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_missing_output_file(self):
        r = self.extract(os.path.join(self.dir, "absent.output"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot read", r.stderr)


if __name__ == "__main__":
    unittest.main()
