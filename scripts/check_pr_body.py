"""Check that a pull request body has the sections a `kind: pr-body` gate requires (decision 45 in docs/decisions.ja.md).

Usage (the skill runs it in the main session, never inside the workflow):
    python3 check_pr_body.py --pr <number> [--repo OWNER/NAME] --require "Evidence|確かめたこと" --require "Not tested" [--id pr-evidence]
    python3 check_pr_body.py --body-file <path> --require ...        (for tests, or a body not yet posted)

Each --require is one section, given as heading names separated by "|" (any one of them will do). A heading matches
at any level (## or ###), ignoring case and surrounding spaces. A section passes when one matching heading has text
of its own: the lines up to the next heading of any level, so a child section's text does not count for its parent.
Lines inside fenced code blocks are not headings. HTML comments, blank lines and lines that are only a template
placeholder (`<...>`, optionally after a list marker) count as empty.

This is computation only: it says whether the sections are there and not empty, not whether what they say is true.
The body is read as data; nothing in it is run. The output names only the required headings, never the body's text.

Output (stdout): one JSON object {"id", "ok", "missing": [...], "empty": [...], "source": "pr"|"file"}, where missing
and empty list the --require values as given.

Exit codes: 0 every section present and not empty; 1 a section missing or empty; 2 usage error, unreadable file
or a failed `gh` call (the message says which, without the body).
"""
import argparse
import json
import re
import subprocess
import sys

HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
PLACEHOLDER = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?<[^<>]+>\s*$")
COMMENT = re.compile(r"<!--.*?-->", re.S)


def norm(text):
    return " ".join(text.split()).casefold()


def sections(body):
    """Return [(heading, own_text)] for each ATX heading outside fenced code blocks."""
    out = []
    fence = None
    current = None
    for line in body.splitlines():
        f = FENCE.match(line)
        if fence:
            if f and f.group(1)[0] == fence[0] and len(f.group(1)) >= len(fence):
                fence = None
            if current is not None:
                current[1].append(line)
            continue
        if f:
            fence = f.group(1)
            if current is not None:
                current[1].append(line)
            continue
        h = HEADING.match(line)
        if h:
            current = (h.group(2), [])
            out.append(current)
        elif current is not None:
            current[1].append(line)
    return [(name, "\n".join(lines)) for name, lines in out]


def has_text(text):
    text = COMMENT.sub("", text)
    return any(line.strip() and not PLACEHOLDER.match(line) for line in text.splitlines())


def check(body, requires):
    found = sections(body or "")
    missing, empty = [], []
    for req in requires:
        names = {norm(n) for n in req.split("|") if n.strip()}
        matches = [text for name, text in found if norm(name) in names]
        if not matches:
            missing.append(req)
        elif not any(has_text(t) for t in matches):
            empty.append(req)
    return missing, empty


def read_pr(number, repo):
    cmd = ["gh", "pr", "view", str(number), "--json", "body"]
    if repo:
        cmd += ["-R", repo]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"gh could not be run: {type(e).__name__}")
    if r.returncode:
        raise RuntimeError(f"gh pr view exited {r.returncode}: {r.stderr.strip()[:300]}")
    try:
        return json.loads(r.stdout).get("body") or ""
    except (ValueError, AttributeError):
        raise RuntimeError("gh pr view did not return a JSON object with body")


def main(argv=None):
    p = argparse.ArgumentParser(description="Check a PR body for required sections (ccreview pr-body gate).")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--pr", type=int)
    src.add_argument("--body-file")
    p.add_argument("--repo", help="OWNER/NAME, passed to gh as -R (only with --pr)")
    p.add_argument("--require", action="append", required=True, help='heading names separated by "|"; repeat per section')
    p.add_argument("--id", default="pr-body", help="the gate id, echoed in the output")
    a = p.parse_args(argv)

    if a.pr is not None and a.pr <= 0:
        p.error("--pr must be a positive number")
    if a.repo and a.pr is None:
        p.error("--repo goes with --pr")
    if any(not any(n.strip() for n in r.split("|")) for r in a.require):
        p.error("--require needs at least one heading name")

    try:
        if a.pr is not None:
            body, source = read_pr(a.pr, a.repo), "pr"
        else:
            with open(a.body_file, encoding="utf-8") as f:
                body, source = f.read(), "file"
    except (OSError, UnicodeDecodeError) as e:
        print(f"check_pr_body: cannot read the body file: {type(e).__name__}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"check_pr_body: {e}", file=sys.stderr)
        return 2

    missing, empty = check(body, a.require)
    ok = not missing and not empty
    print(json.dumps({"id": a.id, "ok": ok, "missing": missing, "empty": empty, "source": source}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
