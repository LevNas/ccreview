"""Write the per-run allowlist that hooks/gate.py enforces on the ccreview fix agent.

Decisions 21, 23 and 25 in docs/decisions.ja.md. The ccreview skill runs this for a trusted change,
after expanding the command gates and before starting the workflow:

    python3 write_allowlist.py --range <base>..HEAD --max-rounds 3 --gates '<json>'
    python3 write_allowlist.py --remove            # after the run, trusted or not

The fix agent of round N writes the diff the next reviewers read with
`git diff <range> --output=<diff dir>/diff-r<N+1>.patch`; each of those exact commands is on the list.
--remove deletes the allowlist and the diff directory in the worktree (the skill writes diff-r1.patch
for an untrusted change too, so it runs --remove after every run).

--gates is a JSON list of the command gates, [{"id", "run", "template", "blocking"}]: `run` with {files}
expanded, `template` as declared in review-gates.yaml.

A settings `ask` rule beats a hook's allow (decision-21 spike), so a gate that matches one would stop the
background fix agent on a prompt. Such gates, gates matching a `deny` rule, and gates with shell
metacharacters (the hook allows one plain command per call) are left off the allowlist and reported as
deferred: the workflow does not run them in the loop and the skill runs them in the main session (decision
25). Matching errs toward a match: a false match only defers a gate, a missed one stalls the run.

The allowlist goes to <worktree git dir>/ccreview/allowlist.json, outside the working tree, where the fix
agent's edits cannot reach it (decision 24). Output is a JSON summary on stdout.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

TTL_SECONDS = 6 * 3600
SHELL_META = re.compile(r"[;&|`$<>\n\r]")
ENV_PREFIX = re.compile(r"^(\s*[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
READ_ONLY_GIT = ["git status", "git status --short", "git diff", "git diff --cached",
                 "git show --stat HEAD", "git rev-parse HEAD"]
MANAGED_SETTINGS = ["/etc/claude-code/managed-settings.json",
                    "/Library/Application Support/ClaudeCode/managed-settings.json"]


def git(*args, cwd="."):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def settings_files(worktree):
    """Every settings file whose permission rules may apply. The main checkout is included because a
    gitignored settings.local.json lives only there, not in the worktree."""
    main = os.path.dirname(os.path.realpath(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=worktree)))
    user_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    managed = [os.environ["CCREVIEW_MANAGED_SETTINGS"]] if os.environ.get("CCREVIEW_MANAGED_SETTINGS") else MANAGED_SETTINGS
    project = [os.path.join(root, ".claude", name)
               for root in dict.fromkeys([worktree, main]) for name in ("settings.json", "settings.local.json")]
    return [*managed, os.path.join(user_dir, "settings.json"), *project]


def load_rules(files):
    rules, read, errors = [], [], []
    for path in files:
        try:
            with open(path) as f:
                perms = (json.load(f) or {}).get("permissions") or {}
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        read.append(path)
        for kind in ("ask", "deny"):
            rules += [(kind, r, path) for r in perms.get(kind) or [] if isinstance(r, str)]
    return rules, read, errors


def bash_pattern(rule):
    """The pattern inside Bash(...), '*' for a bare Bash rule, None for rules on other tools."""
    rule = rule.strip()
    if rule == "Bash":
        return "*"
    m = re.fullmatch(r"Bash\((.*)\)", rule, re.S)
    return m.group(1).strip() if m else None


def rule_matches(pattern, command):
    cmd = command.strip()
    candidates = {cmd, ENV_PREFIX.sub("", cmd)}
    if pattern in ("", "*"):
        return True
    if pattern.endswith(":*"):  # legacy prefix form, e.g. Bash(git push:*)
        p = pattern[:-2]
        return any(c == p or c.startswith(p + " ") for c in candidates)
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    if any(re.fullmatch(regex, c, re.S) for c in candidates):
        return True
    # "rm *" also covers a bare "rm"
    return pattern.endswith(" *") and any(c == pattern[:-2] for c in candidates)


def gate_script_paths(gate, worktree, changed):
    """Files a gate's own command names (its script, its config): the fix agent must not edit them, or
    an allowed gate would run code the fix agent wrote. The files {files} expanded to are the gate's
    input, which the fix agent is there to fix, so they are read from the unexpanded template. Without a
    template, the changed files are left out instead (a script the change edits is then not protected)."""
    template = gate.get("template")
    try:
        tokens = shlex.split(template if template else gate.get("run") or "")
    except ValueError:
        return []
    found = []
    for tok in tokens:
        if tok.startswith("-") or tok == "{files}" or (not template and tok in changed):
            continue
        full = os.path.normpath(os.path.join(worktree, tok))
        if full.startswith(worktree + os.sep) and os.path.isfile(full):
            found.append(os.path.relpath(full, worktree).replace(os.sep, "/"))
    return found


def classify_gates(gates, rules):
    allowed, deferred = [], []
    for g in gates:
        run = (g.get("run") or "").strip()
        reason = None
        if not run:
            reason = "no command"
        elif SHELL_META.search(run):
            reason = "shell metacharacters: the fix agent's hook allows one plain command per call"
        else:
            try:
                shlex.split(run)
            except ValueError:
                reason = "the command could not be parsed"
        if reason is None:
            for kind, rule, path in rules:
                pattern = bash_pattern(rule)
                if pattern is not None and rule_matches(pattern, run):
                    reason = f"matches the {kind} rule {rule} in {path}"
                    break
        if reason:
            deferred.append({"id": g["id"], "reason": reason})
        else:
            allowed.append(g)
    return allowed, deferred


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--range")
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--gates", default="[]", help="JSON list of command gates [{id, run, template, blocking}]")
    ap.add_argument("--remove", action="store_true", help="delete the allowlist and the diff directory after the run")
    ap.add_argument("--diff-dir", default=".ccreview",
                    help="directory for the round diffs inside the worktree: absolute, or relative to the worktree root")
    ap.add_argument("--worktree", default=".")
    a = ap.parse_args(argv)

    worktree = os.path.realpath(git("rev-parse", "--show-toplevel", cwd=a.worktree))
    path = os.path.join(git("rev-parse", "--absolute-git-dir", cwd=worktree), "ccreview", "allowlist.json")
    # Kept as given: the fix agent is told this exact string, and the hook matches commands exactly.
    # The skill passes an absolute path so that no agent's cwd matters.
    diff_dir = a.diff_dir.strip().rstrip("/")
    full_diff_dir = os.path.realpath(os.path.join(worktree, diff_dir))
    if not diff_dir or not full_diff_dir.startswith(worktree + os.sep):
        ap.error("--diff-dir must be a directory inside the worktree")
    # The fix agent gets the path unquoted and the hook compares shlex tokens: a space would split it
    # into --output=<prefix> plus a pathspec (git then writes <prefix>), and metacharacters are denied.
    if re.search(r"[\s'\"\\]", diff_dir) or SHELL_META.search(diff_dir):
        ap.error("--diff-dir must not contain whitespace, quotes or shell metacharacters")
    if os.path.islink(os.path.join(worktree, diff_dir)):
        ap.error("--diff-dir must not be a symbolic link")
    if a.remove:
        existed = os.path.exists(path)
        if existed:
            os.remove(path)
        diffs = os.path.isdir(full_diff_dir)
        if diffs:
            # Only the files this run writes: a directory that holds anything else is left alone.
            for name in os.listdir(full_diff_dir):
                if re.fullmatch(r"diff-r\d+\.patch", name):
                    os.remove(os.path.join(full_diff_dir, name))
            if not os.listdir(full_diff_dir):
                os.rmdir(full_diff_dir)
        print(json.dumps({"path": path, "removed": existed, "diffDir": full_diff_dir,
                          "diffDirRemoved": diffs and not os.path.exists(full_diff_dir)}))
        return 0
    if not a.range:
        ap.error("--range is required unless --remove is given")

    gates = json.loads(a.gates)
    rules, read, errors = load_rules(settings_files(worktree))
    if errors:  # rules we could not read might have matched: defer every gate rather than risk a stall
        allowed, deferred = [], [{"id": g["id"], "reason": "a settings file could not be read"} for g in gates]
    else:
        allowed, deferred = classify_gates(gates, rules)
    # quotepath=off: a non-ASCII name must match the path the fix agent edits, not git's "\343..." form
    files = [f for f in git("-c", "core.quotepath=off", "diff", "--name-only", a.range, cwd=worktree).splitlines() if f]
    protected = sorted({p for g in gates for p in gate_script_paths(g, worktree, set(files))})
    allowlist = {
        "version": 1,
        "worktree": worktree,
        "range": a.range,
        "files": files,
        "commands": READ_ONLY_GIT + [f"git diff {a.range}", f"git log --oneline {a.range}"]
                    + [f"git diff {a.range} --output={diff_dir}/diff-r{n}.patch" for n in range(2, a.max_rounds + 1)]
                    + [g["run"].strip() for g in allowed],
        "commitMessages": [f"ccreview: round {n} fixes" for n in range(1, a.max_rounds + 1)],
        "protected": protected,
        "expires": int(time.time()) + TTL_SECONDS,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(allowlist, f, ensure_ascii=False, indent=2)
    print(json.dumps({
        "path": path,
        "allowedGates": [g["id"] for g in allowed],
        "deferred": deferred,
        "protected": protected,
        "files": len(files),
        "settingsRead": read,
        "settingsErrors": errors,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
