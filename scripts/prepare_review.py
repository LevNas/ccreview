"""Prepare a ccreview run: the skill's steps 1 to 3 in one script (decision 48).

The main session checks out the target in an isolated worktree, then runs:

    python3 prepare_review.py --worktree <abs> --pr <n> --out <abs path outside the repository>
    python3 prepare_review.py --worktree <abs> --branch <name> [--base main] --out <abs>

Options: --trust trusted|untrusted (when the user states it), --max-rounds N (wins over the yaml's max_rounds),
--spec <abs> (adds the spec-scope gate, decision 47), --declarations-from <abs dir> (borrow REVIEW.md and
.claude/review-gates.yaml from another checkout, trusted only, decision 49).

What it does, in order. Every git and gate command runs with the worktree as its cwd, whatever the caller's cwd.
1. The target. A PR: `gh pr view` gives the branches, the author and the head SHA; the worktree's HEAD must be
   that SHA. Trust is `trusted` only when the author equals `gh api user` (decision 18), unless --trust is given.
   A branch is `trusted` unless --trust says otherwise; use a branch for a GitLab MR, with the trust the user states.
   After `git fetch origin`, the range is `<merge-base origin/<base> HEAD>..HEAD` (decision 14).
2. The declarations: from the worktree when trusted, from `origin/<base>` when untrusted (so the change cannot
   weaken them), or from --declarations-from. review-gates.yaml is read by a small reader for the shapes the
   examples use (see read_gates_yaml); anything else stops with its line number. REVIEW.md sections run from a
   `## ` heading to the next one or to the end of the file.
3. The diff: `mkdir <worktree>/.ccreview` (never -p) and `git diff <range> --output=.../diff-r1.patch`.
4. The gates: a gate with `paths` is kept only when a changed file matches one of its globs (fnmatch: `*` also
   matches `/`). `{files}` in a command gate's `run` becomes the matching files. Command gates run here when
   trusted and the declarations are not borrowed; pr-body gates run here for a GitHub PR (decision 45) and are
   never passed to the workflow.
5. The allowlist (trusted only): write_allowlist.py, whose `deferred` goes to args.deferredGates.

Output: the workflow's args as JSON in --out, and a summary on stdout whose first line names where the
declarations came from. Exit 2 means the run must not start; the reason is on stderr, and --out is not written.
Nothing in the worktree is created before the declarations have been read.
"""
import argparse
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GATES_PATH = ".claude/review-gates.yaml"
REVIEW_PATH = "REVIEW.md"
SHARED = {  # args.review key -> accepted headings (decision 44), compared casefolded
    "severityText": ("Severity definitions", "重大度の定義"),
    "categoriesText": ("Category vocabulary", "category の語彙"),
    "commonText": ("Rules for every perspective", "観点共通の決まり"),
}
KINDS = ("perspective", "command", "review", "pr-body")
SPEC_TEXT = """This perspective is added by the ccreview skill from a spec the user passed, not from REVIEW.md.
Read the spec at `<spec>` and find its section headed "Out of Scope" (or "範囲外"). Each item there is something the user decided this work leaves out.
Report a finding for each change in the range that does what one of those items leaves out, and quote that item in the summary. A change no item covers is not a finding here, and neither is a problem in the spec itself.
Such a finding is `blocking`. For its category, take the word from the vocabulary that fits best."""


class Stop(Exception):
    """The run must not start."""


# --- review-gates.yaml (decision 48: no YAML library; only the shapes the examples use) ------------------

def _strip_comment(line):
    quote = None
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _scalar(text, n):
    t = text.strip()
    if t.startswith('"'):
        try:
            value, end = json.JSONDecoder().raw_decode(t)
        except ValueError:
            raise Stop(f"{GATES_PATH} line {n}: a double-quoted string that does not close")
        if t[end:].strip():
            raise Stop(f"{GATES_PATH} line {n}: text after a quoted string")
        return value
    if t.startswith("'"):
        m = re.fullmatch(r"'((?:[^']|'')*)'", t)
        if not m:
            raise Stop(f"{GATES_PATH} line {n}: a single-quoted string that does not close")
        return m.group(1).replace("''", "'")
    if not t or t[0] in "{[|>&*!%@`" or t in ("~", "null") or re.search(r":\s", t):
        raise Stop(f"{GATES_PATH} line {n}: a value this reader does not take ({t or 'empty'!r})")
    if t in ("true", "false"):
        return t == "true"
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    return t


def _value(text, n):
    t = text.strip()
    if not t.startswith("["):
        return _scalar(t, n)
    if not t.endswith("]"):
        raise Stop(f"{GATES_PATH} line {n}: a [list] must open and close on one line")
    items, buf, quote = [], "", None
    for c in t[1:-1]:
        if quote:
            quote = None if c == quote else quote
        elif c in "\"'":
            quote = c
        elif c == ",":
            items.append(buf)
            buf = ""
            continue
        buf += c
    if buf.strip() or items:
        items.append(buf)
    return [_scalar(i, n) for i in items]


def _key_value(text, n):
    m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?", text)
    if not m:
        raise Stop(f"{GATES_PATH} line {n}: expected `key: value`")
    return m.group(1), (m.group(2) or "").strip()


def read_gates_yaml(text):
    """Read `max_rounds: N` and `gates:` as a list of flat maps whose values are strings, integers, booleans or
    one-line ["a", "b"] lists. Comments and blank lines are ignored. Any other shape (a nested map, a block
    list, anchors, multi-line strings) raises Stop with the line number."""
    top, gates, item, item_indent = {}, None, None, None
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise Stop(f"{GATES_PATH} line {n}: a tab in the indentation")
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        if indent == 0:
            key, val = _key_value(body, n)
            if key in top:
                raise Stop(f"{GATES_PATH} line {n}: `{key}` appears twice")
            if key == "gates":
                if val:
                    raise Stop(f"{GATES_PATH} line {n}: `gates:` must be followed by a list of `- id: ...` items")
                gates = top["gates"] = []
            else:
                if not val:
                    raise Stop(f"{GATES_PATH} line {n}: `{key}:` without a value (nested maps are not read)")
                top[key] = _value(val, n)
                gates = None
            item = None
            continue
        if gates is None:
            raise Stop(f"{GATES_PATH} line {n}: an indented line outside `gates:`")
        if body.startswith("- "):
            item, item_indent = {}, indent + 2
            gates.append(item)
            body = body[2:].strip()
        elif item is None or indent != item_indent:
            raise Stop(f"{GATES_PATH} line {n}: unexpected indentation (nested maps and block lists are not read)")
        key, val = _key_value(body, n)
        if not val:
            raise Stop(f"{GATES_PATH} line {n}: `{key}:` without a value (nested maps and block lists are not read)")
        if key in item:
            raise Stop(f"{GATES_PATH} line {n}: `{key}` appears twice in one gate")
        item[key] = _value(val, n)
    if top.get("gates") is None:
        raise Stop(f"{GATES_PATH}: no `gates:` list")
    for g in top["gates"]:
        if not isinstance(g.get("id"), str) or not g["id"]:
            raise Stop(f"{GATES_PATH}: a gate without an id")
        if g.get("kind") not in KINDS:
            raise Stop(f"{GATES_PATH}: gate {g['id']} has kind {g.get('kind')!r}; expected one of {', '.join(KINDS)}")
        if "paths" in g and not (isinstance(g["paths"], list) and all(isinstance(p, str) for p in g["paths"])):
            raise Stop(f"{GATES_PATH}: gate {g['id']}: paths must be a [list] of strings")
    if "max_rounds" in top and (not isinstance(top["max_rounds"], int) or top["max_rounds"] < 1):
        raise Stop(f"{GATES_PATH}: max_rounds must be a positive integer")
    return top


# --- REVIEW.md -------------------------------------------------------------------------------------------

def read_review(text):
    """Sections by `## ` heading. A section ends at the next `## ` heading or at the end of the file (the last
    section has no next heading; decision 48)."""
    return {m.group(1).strip(): m.group(2).strip()
            for m in re.finditer(r"^## (.+?)[ \t]*\n(.*?)(?=^## |\Z)", text + "\n", re.S | re.M)}


def shared_sections(sections):
    folded = {k.casefold(): v for k, v in sections.items()}
    review, missing = {}, []
    for key, names in SHARED.items():
        hit = next((folded[n.casefold()] for n in names if n.casefold() in folded), None)
        if not hit:
            missing.append(" / ".join(f'"{n}"' for n in names))
        review[key] = hit
    if missing:
        raise Stop(f"{REVIEW_PATH} lacks the section(s) {'; '.join(missing)} (or they are empty)")
    review["categories"] = re.findall(r"^\s*[-*]\s+`([^`]+)`", review["categoriesText"], re.M)
    if not review["categories"]:
        raise Stop(f"{REVIEW_PATH}: the category section lists no words in backticks")
    return review


# --- helpers ---------------------------------------------------------------------------------------------

def run(cmd, cwd, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Stop(f"`{' '.join(cmd)}` failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def git(wt, *args):
    return run(["git", *args], wt).stdout


def inside(path, root):
    root = os.path.realpath(root)
    return os.path.commonpath([os.path.realpath(path), root]) == root


def toplevel(cwd):
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def tail(text, lines=40):
    return "\n".join(text.splitlines()[-lines:])


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# --- the steps -------------------------------------------------------------------------------------------

def target(a, wt, say):
    """Step 1 up to the range: returns (base, trust, pr)."""
    pr = None
    if a.pr is not None:
        pr = json.loads(run(["gh", "pr", "view", str(a.pr), "--json",
                             "headRefName,baseRefName,author,url,headRefOid"], wt).stdout)
        base = pr["baseRefName"]
        head = git(wt, "rev-parse", "HEAD").strip()
        if head != pr["headRefOid"]:
            raise Stop(f"the worktree's HEAD {head} is not the PR head {pr['headRefOid']}; check out the PR first")
        author = (pr.get("author") or {}).get("login") or ""
        if a.trust:
            trust, why = a.trust, "stated by the user"
        else:
            me = run(["gh", "api", "user", "--jq", ".login"], wt).stdout.strip()
            trust = "trusted" if author and author == me else "untrusted"
            why = f"PR author {author} = gh user {me}" if trust == "trusted" else f"PR author {author}, gh user {me}"
    else:
        base = a.base
        branch = git(wt, "branch", "--show-current").strip()
        if branch != a.branch:
            raise Stop(f"the worktree is on {branch or 'a detached HEAD'}, not {a.branch}")
        trust = a.trust or "trusted"
        why = "stated by the user" if a.trust else "a local branch the user gave"
    run(["git", "fetch", "origin"], wt)
    start = git(wt, "merge-base", f"origin/{base}", "HEAD").strip()
    say(f"trust: {trust} ({why})")
    return base, f"{start}..HEAD", trust, pr


def declarations(a, wt, base, trust):
    """Step 2's reading: returns (source line, gates yaml, review sections)."""
    if a.declarations_from:
        if trust != "trusted":
            raise Stop("--declarations-from is refused for an untrusted change: its gates would come from elsewhere "
                       "while the change itself is not trusted")
        src = os.path.realpath(a.declarations_from)

        def read(rel):
            try:
                with open(os.path.join(src, rel), encoding="utf-8") as f:
                    return f.read()
            except OSError:
                raise Stop(f"{rel} is missing in {src}")
        line = f"declarations: borrowed from {src}"
    elif trust == "untrusted":
        def read(rel):
            r = run(["git", "show", f"origin/{base}:{rel}"], wt, check=False)
            if r.returncode != 0:
                raise Stop(f"{rel} is missing on origin/{base}; an untrusted change is reviewed only with the base's "
                           "declarations")
            return r.stdout
        line = f"declarations: origin/{base}"
    else:
        def read(rel):
            try:
                with open(os.path.join(wt, rel), encoding="utf-8") as f:
                    return f.read()
            except OSError:
                raise Stop(f"{rel} is missing in the worktree")
        line = f"declarations: worktree {wt}"
    return line, read_gates_yaml(read(GATES_PATH)), read_review(read(REVIEW_PATH))


def select_gates(decl, sections, files, wt, borrowed, say):
    """Step 2's gates: (gates for the workflow, command gates to run, pr-body gates)."""
    gates, commands, bodies = [], [], []
    for g in decl["gates"]:
        if g["kind"] == "pr-body":  # paths is not read for these (decision 45)
            bodies.append(g)
            continue
        matched = files
        if "paths" in g:
            matched = [f for f in files if any(fnmatch.fnmatchcase(f, p) for p in g["paths"])]
            if not matched:
                say(f"skipped: {g['id']} (paths matched no changed file)")
                continue
        if g["kind"] == "command" and borrowed:
            say(f"skipped: {g['id']} (declarations are borrowed; their commands are not in this repository)")
            continue
        gate = dict(g)
        if g["kind"] == "perspective":
            ref = g.get("ref")
            if ref not in sections or not sections[ref]:
                raise Stop(f"gate {g['id']}: REVIEW.md has no section headed exactly {ref!r}, or it is empty")
            gate["sectionText"] = sections[ref]
            gate.setdefault("passes", 1)
        if g["kind"] == "command":
            if not isinstance(g.get("run"), str) or not g["run"].strip():
                raise Stop(f"gate {g['id']}: a command gate needs `run`")
            gate["template"] = g["run"]
            gate["run"] = g["run"].replace("{files}", " ".join(shlex.quote(f) for f in matched))
            commands.append((gate, len(matched) if "{files}" in g["run"] else None))
        say(f"kept: {g['id']} ({g['kind']}, {plural(len(matched), 'matching file')})")
        gates.append(gate)
    return gates, commands, bodies


def run_commands(commands, wt, trust, borrowed, say):
    results = []
    for gate, nfiles in commands:
        if trust != "trusted":
            say(f"command: {gate['id']} not run: untrusted (the command is part of the change)")
            continue
        r = subprocess.run(gate["run"], shell=True, cwd=wt, capture_output=True, text=True)
        given = "" if nfiles is None else f" ({plural(nfiles, 'file')})"
        say(f"command: {gate['id']} exit {r.returncode}{given}")
        results.append({"id": gate["id"], "exitCode": r.returncode, "outputTail": tail(r.stdout + r.stderr),
                        "failures": []})
    return results


def run_pr_bodies(bodies, pr, n, wt, say):
    for g in bodies:
        if pr is None:
            say(f"pr-body: {g['id']} not run (no GitHub PR)")
            continue
        repo = re.sub(r"^https://github\.com/|/pull/\d+/?$", "", pr["url"])
        req = g.get("require") or []
        cmd = [sys.executable, os.path.join(HERE, "check_pr_body.py"), "--pr", str(n), "--repo", repo, "--id", g["id"]]
        for item in req:
            cmd += ["--require", item]
        r = subprocess.run(cmd, cwd=wt, capture_output=True, text=True)
        say(f"pr-body: {g['id']} exit {r.returncode} {(r.stdout or r.stderr).strip()}")


def spec_gate(spec, files, wt, say):
    if not os.path.isabs(spec) or re.search(r"[`'\n]", spec) or not os.path.isfile(spec):
        say(f"spec: no spec-scope gate (the path must be absolute, an existing file, with no quote, backtick or "
            f"newline): {spec}")
        return None
    r = subprocess.run([sys.executable, os.path.join(HERE, "check_pr_body.py"), "--body-file", spec,
                        "--require", "Out of Scope|範囲外", "--id", "spec-scope"], capture_output=True, text=True)
    if r.returncode != 0:
        say(f"spec: no spec-scope gate (exit {r.returncode}: no non-empty section headed exactly \"Out of Scope\" "
            f"or \"範囲外\"; the review runs without the scope check) {r.stdout.strip()}")
        return None
    if any(os.path.realpath(os.path.join(wt, f)) == os.path.realpath(spec) for f in files):
        say("note: the change edits the spec it is checked against")
    say(f"spec: spec-scope gate added from {spec}")
    return {"id": "spec-scope", "kind": "perspective", "ref": "Spec scope", "sectionText": SPEC_TEXT.replace("<spec>", spec),
            "passes": 1, "pass": "no-blocking", "blocking": True, "autofix": "none"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--worktree", required=True, help="absolute path of the worktree the target is checked out in")
    tgt = ap.add_mutually_exclusive_group(required=True)
    tgt.add_argument("--pr", type=int)
    tgt.add_argument("--branch")
    ap.add_argument("--base", default="main", help="the base branch for --branch (a PR names its own)")
    ap.add_argument("--trust", choices=("trusted", "untrusted"), help="only when the user states it")
    ap.add_argument("--max-rounds", type=int)
    ap.add_argument("--spec", help="absolute path of a spec with an Out of Scope section (decision 47)")
    ap.add_argument("--declarations-from", help="absolute path of a checkout to borrow the declarations from")
    ap.add_argument("--out", required=True, help="where to write the args JSON: absolute, outside the repository")
    a = ap.parse_args(argv)

    lines = []
    say = lines.append
    try:
        for name in ("worktree", "out", "declarations_from"):
            v = getattr(a, name)
            if v is not None and not os.path.isabs(v):
                raise Stop(f"--{name.replace('_', '-')} must be an absolute path")
        if a.max_rounds is not None and a.max_rounds < 1:
            raise Stop("--max-rounds must be 1 or more")
        wt = toplevel(a.worktree)
        if not wt:
            raise Stop(f"{a.worktree} is not in a git repository")
        if os.path.realpath(wt) != os.path.realpath(a.worktree):
            raise Stop(f"--worktree must be the top of the worktree ({wt})")
        for root in filter(None, (wt, toplevel(os.getcwd()))):
            if inside(a.out, root):
                raise Stop(f"--out must be outside the repository ({root}); an untracked file there stops "
                           "git worktree remove")

        base, rng, trust, pr = target(a, wt, say)
        files = [f for f in git(wt, "-c", "core.quotepath=off", "diff", "--name-only", "-z", rng).split("\0") if f]
        if any(f == ".ccreview" or f.startswith(".ccreview/") for f in files):
            raise Stop("the change contains .ccreview; a link there would make git diff --output write elsewhere")
        source, decl, sections = declarations(a, wt, base, trust)
        review = shared_sections(sections)
        borrowed = bool(a.declarations_from)
        gates, commands, bodies = select_gates(decl, sections, files, wt, borrowed, say)
        if a.spec:
            g = spec_gate(a.spec, files, wt, say)
            if g:
                gates.append(g)
        max_rounds = a.max_rounds or decl.get("max_rounds") or 3

        diff_dir = os.path.join(wt, ".ccreview")
        try:
            os.mkdir(diff_dir)
        except FileExistsError:
            raise Stop(f"{diff_dir} already exists. If an earlier run left it, remove it with "
                       f"write_allowlist.py --remove --worktree {wt} --diff-dir {diff_dir} and start again")
        git(wt, "diff", rng, f"--output={diff_dir}/diff-r1.patch")

        results = run_commands(commands, wt, trust, borrowed, say)
        run_pr_bodies(bodies, pr, a.pr, wt, say)
        deferred = []
        if trust == "trusted":
            spec = [{"id": g["id"], "run": g["run"], "template": g["template"], "blocking": g.get("blocking", True)}
                    for g, _ in commands]
            r = run([sys.executable, os.path.join(HERE, "write_allowlist.py"), "--worktree", wt, "--range", rng,
                     "--max-rounds", str(max_rounds), "--diff-dir", diff_dir, "--gates", json.dumps(spec)], wt)
            out = json.loads(r.stdout)
            deferred = out["deferred"]
            say(f"allowlist: {out['path']} (allowed: {', '.join(out['allowedGates']) or 'none'})")
            if out["settingsErrors"]:
                say(f"note: settings could not be read, so every command gate is deferred: {out['settingsErrors']}")
            claude = [f for f in files if f.startswith(".claude/") and not f.startswith(".claude/worktrees/")]
            if claude:
                say(f"note: the range has files under .claude/ ({len(claude)}); the fix agent's edits there go "
                    "through Claude Code's protected-path check (decision 31)")
        for d in deferred:
            say(f"deferred: {d['id']} ({d.get('reason', '')})")

        args = {
            "range": rng,
            "changedFiles": [os.path.join(wt, f) for f in files] if borrowed else files,
            "diffDir": diff_dir,
            "maxRounds": max_rounds,
            "trust": trust,
            "gates": gates,
            "review": review,
            "commandResults": results,
            "deferredGates": deferred,
        }
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(args, f, ensure_ascii=False, indent=1)
    except Stop as e:
        print(f"prepare_review: {e}", file=sys.stderr)
        if lines:
            print("\n".join(lines))
        return 2
    print(source)
    print(f"range: {rng} ({plural(len(files), 'changed file')}), maxRounds {max_rounds}")
    print("\n".join(lines))
    print(f"args: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
