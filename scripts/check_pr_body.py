"""Check that a pull request body has the sections a `kind: pr-body` gate requires (decision 45 in docs/decisions.ja.md).

Usage (the skill runs it in the main session, never inside the workflow):
    python3 check_pr_body.py --pr <number> --repo OWNER/NAME --require 'Evidence|確かめたこと' --require 'Not tested' [--id pr-evidence]
    python3 check_pr_body.py --body-file <path> --require ...        (for tests, or a body not yet posted)

GitHub only: --pr reads the body with `gh pr view`. For another host, fetch the body yourself and use --body-file.

Each --require is one section, given as heading names separated by "|" (any one of them will do). The rules:
- Only ATX headings (`#` to `######`) count, at any level. Setext headings (a line underlined with `===` or `---`)
  are not headings here. Names are compared ignoring case, surrounding spaces, `*`/`_` emphasis around the name and
  a trailing colon.
- HTML comments are removed from the whole body first; an unclosed `<!--` runs to the end of the body. So a
  commented-out section is not there.
- Lines inside fenced code blocks are not headings (a fence closes on a line of the same marker, at least as long,
  with nothing after it).
- A section's own text is the lines up to the next heading of any level, so a child section's text does not count
  for its parent. It is empty unless some line, after its list marker and checkbox, has a letter or digit and is not
  a lone template placeholder (`<...>` with no `=` and no `://`). An image tag or an autolink counts as text; a lone
  `<details>` does not.
- A section passes when one matching heading has text of its own.

This is computation only: it says whether the sections are there and not empty, not whether what they say is true.
The body is read as data; nothing in it is run. The output names only the required headings, never the body's text.

Output (stdout): one JSON object {"id", "ok", "missing": [...], "empty": [...], "source": "pr"|"file"}, where missing
and empty list the --require values as given.

Exit codes: 0 every section present and not empty; 1 a section missing or empty; 2 usage error, unreadable file,
a body over the size limit, or a failed `gh` call (the message says which, without the body).
"""
import argparse
import json
import re
import subprocess
import sys
import unicodedata

MAX_BODY = 200_000  # characters; GitHub caps a PR body at 65,536
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*))?$")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
LIST_MARKER = re.compile(r"^(?:[-*+]|\d{1,9}[.)])(?:[ \t]+|$)")
CHECKBOX = re.compile(r"^\[[ xX]?\](?:[ \t]+|$)")
PLACEHOLDER = re.compile(r"^<[^<>=]+>$")


def strip_comments(body):
    """Remove <!-- ... --> everywhere; an unclosed <!-- runs to the end. Linear time."""
    out, i = [], 0
    while True:
        start = body.find("<!--", i)
        if start < 0:
            out.append(body[i:])
            return "".join(out)
        out.append(body[i:start])
        end = body.find("-->", start + 4)
        if end < 0:
            return "".join(out)
        i = end + 3


def heading_text(raw):
    """The heading's text with CommonMark's optional closing #s removed (only when a space precedes them)."""
    text = (raw or "").rstrip(" \t")
    stripped = text.rstrip("#")
    if stripped != text and (stripped == "" or stripped[-1] in " \t"):
        text = stripped.rstrip(" \t")
    return text


def norm(name):
    name = " ".join(name.split()).strip("*_ ")
    if name.endswith(":"):
        name = name[:-1].strip("*_ ")
    return name.casefold()


def sections(body):
    """Return [(heading, own_text)] for each ATX heading outside fenced code blocks."""
    out = []
    fence = None
    current = None
    for line in body.splitlines():
        if fence:
            c = FENCE_CLOSE.match(line)
            if c and c.group(1)[0] == fence[0] and len(c.group(1)) >= len(fence):
                fence = None
            if current is not None:
                current[1].append(line)
            continue
        f = FENCE_OPEN.match(line)
        if f:
            fence = f.group(1)
            if current is not None:
                current[1].append(line)
            continue
        h = HEADING.match(line)
        if h:
            current = (heading_text(h.group(2)), [])
            out.append(current)
        elif current is not None:
            current[1].append(line)
    return [(name, lines) for name, lines in out]


def visible(line):
    return "".join(ch for ch in line if unicodedata.category(ch) != "Cf").replace(" ", " ").strip()


def has_text(lines):
    for line in lines:
        line = CHECKBOX.sub("", LIST_MARKER.sub("", visible(line))).strip()
        if not any(ch.isalnum() for ch in line):
            continue
        if PLACEHOLDER.match(line) and "://" not in line:
            continue
        return True
    return False


def check(body, requires):
    found = sections(strip_comments(body or ""))
    missing, empty = [], []
    for req in requires:
        names = {norm(n) for n in req.split("|") if n.strip()}
        matches = [lines for name, lines in found if norm(name) in names]
        if not matches:
            missing.append(req)
        elif not any(has_text(lines) for lines in matches):
            empty.append(req)
    return missing, empty


def safe_message(text, limit=120):
    first = (text or "").strip().splitlines()[:1]
    first = first[0] if first else ""
    return "".join(ch if ch.isprintable() else "?" for ch in first)[:limit]


def read_pr(number, repo):
    cmd = ["gh", "pr", "view", str(number), "--json", "body"]
    if repo:
        cmd += ["-R", repo]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"gh could not be run: {type(e).__name__}")
    if r.returncode:
        raise RuntimeError(f"gh pr view exited {r.returncode}: {safe_message(r.stderr)}")
    try:
        data = json.loads(r.stdout)
        return data.get("body") or ""
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
                body, source = f.read(MAX_BODY + 1), "file"
    except (OSError, UnicodeDecodeError) as e:
        print(f"check_pr_body: cannot read the body file: {type(e).__name__}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"check_pr_body: {e}", file=sys.stderr)
        return 2
    if len(body) > MAX_BODY:
        print(f"check_pr_body: the body is over {MAX_BODY} characters; not checked", file=sys.stderr)
        return 2

    missing, empty = check(body, a.require)
    ok = not missing and not empty
    print(json.dumps({"id": a.id, "ok": ok, "missing": missing, "empty": empty, "source": source}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
