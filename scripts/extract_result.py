"""Take a ccreview run's result out of the workflow's output file and write it where the skill reads it (decision 42 in docs/decisions.ja.md).

Usage (from the worktree root, right after the workflow's task-notification arrives):
    python3 extract_result.py --output <the notification's output-file, absolute> --out <a file outside the repository, absolute>

The task-notification carries the result inline, but cut short: in e2e D8, the first 8,000 characters, then
"... (truncated 10741 chars, full result in <output file>)". The full output is one JSON object in the output file; the workflow's return value is under its "result" key
(an object in e2e D8; a JSON string is accepted too). A file that is already a bare result (it has "verdict" and
"rounds" at the top) is accepted as it is.

Without this script, the main session wrote its own command to read the output file. In e2e D8 the first try
guessed the shape wrong, and the second, a compound command with cd and a relative path, stopped at a permission
prompt until the user came back. The command is fixed here, so the skill can name it.

Output: the result is written to --out (indented JSON). stdout gets one short JSON summary: source ("wrapped" or
"bare"), out, verdict, stopReason, rounds (count), and the length of each list the skill reads (null if absent).
The skill reads the result itself from --out.

The "inside the repository" check looks only at the repository of the current directory (the skill runs this
inside the worktree under review); it does not see a --out inside some other repository.

Exit codes: 0 written; 2 refused (a relative path, --out inside the repository, the same file as --output or not
writable, an unreadable file, or a shape this script does not know; the message names the top-level keys it found, never their contents).
"""
import argparse
import json
import os
import subprocess
import sys

REQUIRED = ("verdict", "rounds")
LISTS = ("remaining", "preExisting", "introduced", "outOfRange", "disputed", "confirmations",
         "repeatMismatches", "unexpectedFiles", "deferred", "notRun")


def refuse(msg):
    print(f"extract_result: {msg}", file=sys.stderr)
    return 2


def repo_root():
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def is_result(obj):
    return isinstance(obj, dict) and all(k in obj for k in REQUIRED)


def unwrap(data):
    """Return (result, source) or (None, reason)."""
    if is_result(data):
        return data, "bare"
    if not isinstance(data, dict):
        return None, f"the file holds a JSON {type(data).__name__}, not an object"
    if "result" not in data:
        return None, f"no \"result\" key; top-level keys: {sorted(data)}"
    inner = data["result"]
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except json.JSONDecodeError:
            return None, "\"result\" is a string that is not JSON"
        except RecursionError:
            return None, "\"result\" is a JSON string nested too deeply to read"
    if not is_result(inner):
        keys = sorted(inner) if isinstance(inner, dict) else type(inner).__name__
        return None, f"\"result\" lacks {list(REQUIRED)}; it has: {keys}"
    return inner, "wrapped"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True, help="the task-notification's output-file, absolute")
    p.add_argument("--out", required=True, help="where to write the result: an absolute path outside the repository "
                                                "(only the repository of the current directory is checked)")
    a = p.parse_args()
    for name in ("output", "out"):
        if not os.path.isabs(getattr(a, name)):
            return refuse(f"--{name} must be an absolute path (no cd, no relative paths)")
    out = os.path.realpath(a.out)
    if out == os.path.realpath(a.output):
        return refuse("--out is the --output file; writing there would replace the workflow's output")
    root = repo_root()
    if root and os.path.commonpath([out, os.path.realpath(root)]) == os.path.realpath(root):
        return refuse(f"--out is inside the repository ({root}); an untracked file there stops git worktree remove")
    try:
        with open(a.output, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeDecodeError) as e:
        return refuse(f"cannot read --output: {e.__class__.__name__}")
    except json.JSONDecodeError as e:
        return refuse(f"--output is not one JSON value (line {e.lineno}, column {e.colno})")
    except RecursionError:
        return refuse("--output is nested too deeply to read")
    result, source = unwrap(data)
    if result is None:
        return refuse(source)
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
    except OSError as e:
        return refuse(f"cannot write --out: {e.__class__.__name__}")
    summary = {"source": source, "out": out, "verdict": result.get("verdict"), "stopReason": result.get("stopReason"),
               "rounds": len(result["rounds"]) if isinstance(result["rounds"], list) else None}
    summary.update({k: len(result[k]) if isinstance(result.get(k), list) else None for k in LISTS})
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
