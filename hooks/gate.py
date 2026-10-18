"""PreToolUse gate for the ccreview fix agent (decisions 21 and 24 in docs/decisions.ja.md).

Only calls from the fix agent type (agent_type "ccreview-fixer" or "<plugin>:ccreview-fixer") are judged;
every other agent and the main session get no decision, so the normal permission flow applies.

For the fix agent the hook reads the per-run allowlist that scripts/write_allowlist.py put in the worktree's
git dir (outside the working tree, so the agent cannot edit it) and answers allow or deny, never ask: a
background workflow agent that hits a permission prompt waits for a human (79 minutes in the sixth e2e run).

- Bash: one plain command per call. Allowed are the listed commands (exact token match),
  `git add -- <changed files>` and `git commit -m <listed message>`.
- Edit / Write / NotebookEdit: only files the review range already changed, inside the worktree, and none
  of the protected paths. A command allowlist alone is not a boundary: `git commit` runs .git/hooks,
  `git status` runs core.fsmonitor, a command gate runs its script, and this hook is itself a file.

Anything unexpected on the fix agent's path (no allowlist, expired, unreadable, an exception) is a deny:
fail-closed for this agent type only (decision 21).

Every decision on the fix agent is appended to hook-decisions.jsonl next to the allowlist. Where the git dir
cannot be found, it goes to ${CLAUDE_PLUGIN_DATA}/hook-decisions.jsonl with the cwd. Where neither takes it,
the decision's reason says so, which lands in the transcript (decision 27; e2e session C lost seven denies).
"""
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import time

FIXER = "ccreview-fixer"
PLUGIN_ROOT = os.path.realpath(os.environ.get("CLAUDE_PLUGIN_ROOT")
                               or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SHELL_META = re.compile(r"[;&|`$<>\n\r]")
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}
# Never editable, even when the change touched them: each can make an allowed command run other code,
# changes what the session loads (settings, hooks, agent types, workflows, skills, rules, CLAUDE.md), or
# changes what this review checks (its gate declarations). A finding on one of them goes back to the main
# session in skipped (option A, 2026-10-05): the fix agent must not rewrite the rules that bind it.
# fnmatch's "*" also matches "/", so "dir/*" covers the whole subtree.
PROTECTED_SEGMENTS = {".git", ".claude-plugin", ".husky"}
PROTECTED_GLOBS = [
    ".gitattributes", "*/.gitattributes", ".gitmodules", ".mcp.json",
    ".claude/settings.json", ".claude/settings.local.json",
    ".claude/hooks/*", ".claude/agents/*", ".claude/workflows/*", ".claude/skills/*", ".claude/commands/*",
    ".claude/rules/*", "*/.claude/rules/*",
    "CLAUDE.md", "*/CLAUDE.md", "CLAUDE.local.md", "*/CLAUDE.local.md",
    "REVIEW.md", ".claude/review-gates.yaml",
    "hooks/hooks.json", "*/hooks/hooks.json",
    ".pre-commit-config.yaml", "lefthook.yml", "lefthook.yaml", ".lefthook.yml", ".lefthook.yaml",
]
# Said for a protected file by both Edit and `git add`, so the fix agent hears one reason for one file.
PROTECTED_REASON = ("is protected (git internals and attributes, settings, hooks, agent types, workflows, skills, "
                    "rules, CLAUDE.md, the review's gate declarations, or a script a command gate runs). "
                    "Report the finding in skipped.")


class Denied(Exception):
    pass


def decide(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": f"ccreview: {reason}",
    }}))


def allowlist_path(cwd):
    override = os.environ.get("CCREVIEW_ALLOWLIST")
    if override:
        return override
    out = subprocess.run(["git", "-C", cwd, "rev-parse", "--absolute-git-dir"],
                         capture_output=True, text=True, timeout=10, check=True)
    return os.path.join(out.stdout.strip(), "ccreview", "allowlist.json")


def load_allowlist(path, cwd):
    try:
        with open(path) as f:
            al = json.load(f)
    except FileNotFoundError:
        raise Denied("no allowlist for this run (the ccreview skill writes it before starting the workflow)")
    if al.get("version") != 1:
        raise Denied("allowlist has an unknown version")
    if not isinstance(al.get("expires"), (int, float)) or al["expires"] < time.time():
        raise Denied("allowlist has expired")
    worktree = os.path.realpath(al["worktree"])
    real_cwd = os.path.realpath(cwd)
    if real_cwd != worktree and not real_cwd.startswith(worktree + os.sep):
        raise Denied("the working directory is outside the worktree under review")
    al["worktree"] = worktree
    al["files"] = set(al.get("files") or [])
    al["protected"] = set(al.get("protected") or [])
    return al


def is_protected(rel, al):
    if rel in al["protected"] or PROTECTED_SEGMENTS.intersection(rel.split("/")):
        return True
    return any(fnmatch.fnmatchcase(rel, g) for g in PROTECTED_GLOBS)


def worktree_rel(path, cwd, al, resolve):
    """Path relative to the worktree, or None when it lies outside. resolve=True follows symlinks."""
    joined = os.path.join(os.path.realpath(cwd), path)
    full = os.path.realpath(joined) if resolve else os.path.normpath(joined)
    if not full.startswith(al["worktree"] + os.sep):
        return None, full
    return os.path.relpath(full, al["worktree"]).replace(os.sep, "/"), full


def bash(command, cwd, al):
    cmd = command.strip()
    if not cmd or SHELL_META.search(cmd):
        return "deny", (f"`{cmd}` is not allowed: one plain command per call, with no chaining, pipes, "
                        "redirection or substitution. Do not retry it.")
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return "deny", f"`{cmd}` could not be parsed. Do not retry it."
    if any(shlex.split(c) == tokens for c in al.get("commands") or []):
        return "allow", "on the allowlist"
    if tokens[:3] == ["git", "add", "--"] and len(tokens) > 3:
        protected, outside = [], []
        for t in tokens[3:]:
            rel, _ = worktree_rel(t, cwd, al, resolve=False)
            if rel is not None and is_protected(rel, al):
                protected.append(t)
            elif rel is None or rel not in al["files"]:
                outside.append(t)
        if not protected and not outside:
            return "allow", "stages changed files"
        # Decision 34: a protected file in the range gets the same reason as its Edit (e2e D5).
        reasons = []
        if protected:
            reasons.append(f"cannot stage {', '.join(protected)}: {'it' if len(protected) == 1 else 'each'} "
                           f"{PROTECTED_REASON}")
        if outside:
            reasons.append(f"cannot stage {', '.join(outside)}: only files changed in the review range may be "
                           "staged. Leave other files unstaged.")
        return "deny", " ".join(reasons)
    if len(tokens) == 4 and tokens[:3] == ["git", "commit", "-m"] and tokens[3] in (al.get("commitMessages") or []):
        return "allow", "commit with the run's message"
    allowed = "; ".join(f"`{c}`" for c in al.get("commands") or [])
    return "deny", (f"`{cmd}` is not on this run's allowlist. Allowed: {allowed}; `git add -- <changed files>`; "
                    "`git commit -m \"<the message in the task>\"`. Running code to check a fix is not allowed. "
                    "Do not retry this command; report what you could not do in skipped.")


def edit(path, cwd, al):
    if not path:
        return "deny", "no file path given"
    rel, full = worktree_rel(path, cwd, al, resolve=True)
    if rel is None:
        return "deny", f"`{path}` resolves outside the worktree under review. Only changed files may be edited."
    if full == PLUGIN_ROOT or full.startswith(PLUGIN_ROOT + os.sep):
        return "deny", f"`{rel}` belongs to the ccreview plugin itself and cannot be edited."
    if is_protected(rel, al):
        return "deny", f"`{rel}` {PROTECTED_REASON}"
    if rel not in al["files"]:
        return "deny", (f"`{rel}` is not a file changed in the review range. New files and other files cannot be "
                        "edited; report the finding in skipped.")
    if not os.path.exists(full):
        return "deny", f"`{rel}` does not exist in the worktree; creating files is not allowed."
    return "allow", "edits a changed file"


LOG_NAME = "hook-decisions.jsonl"


def log_targets(path):
    """Where to log a decision, best first, as (file, may create its directory).

    Next to the allowlist only when that directory exists: the path comes from the fix agent's cwd, which
    may be another repository, and its git dir is not ours to create things in. Then the plugin's data
    directory, which may be created (only an absolute one: a relative one would land in the project)."""
    targets = [(os.path.join(os.path.dirname(path), LOG_NAME), False)] if path else []
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA") or ""
    if os.path.isabs(data_dir):
        targets.append((os.path.join(data_dir, LOG_NAME), True))
    return targets


def log_decision(targets, entry):
    """Append to the first target that takes the entry. Returns None, or why nothing was logged.
    Never raises: a crash here would leave the fix agent's call with no decision (not fail-closed)."""
    errors = []
    for target, create in targets:
        try:
            if create:
                os.makedirs(os.path.dirname(target), exist_ok=True)
            elif not os.path.isdir(os.path.dirname(target)):
                errors.append(f"{target}: no allowlist directory")
                continue
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with open(target, "a", encoding="utf-8", errors="backslashreplace") as f:
                f.write(line)
            return None
        except Exception as exc:
            errors.append(f"{target}: {getattr(exc, 'strerror', None) or type(exc).__name__}")
    return "; ".join(errors) or "no git dir and no CLAUDE_PLUGIN_DATA"


def decide_and_log(decision, reason, targets, entry):
    # Log first: a decision that could not be logged says so in its reason, which the transcript keeps.
    missed = log_decision(targets, {**entry, "decision": decision, "reason": reason})
    if missed:
        reason = f"{reason} (This decision was not logged: {missed}.)"
    decide(decision, reason)


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        # Cannot tell who is calling. Fail closed only when the payload looks like the fix agent's.
        if FIXER in raw:
            decide_and_log("deny", "hook input could not be parsed; denied (fail-closed for the fix agent).",
                           log_targets(None), {"ts": time.time(), "tool": None, "input": "(unparsable hook input)",
                                               "cwd": os.getcwd()})
        return
    agent_type = data.get("agent_type") or ""
    tool = data.get("tool_name")
    if agent_type.split(":")[-1] != FIXER or (tool != "Bash" and tool not in EDIT_TOOLS):
        return
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):  # the log entry below is built outside the decision's try
        tool_input = {}
    cwd = data.get("cwd") or os.getcwd()
    # Found before the decision, so that a decision that fails is still logged next to the allowlist.
    path = path_error = None
    try:
        path = allowlist_path(cwd)
    except Exception as exc:
        path_error = exc
    try:
        if os.environ.get("CCREVIEW_GATE_BREAK") == "1":
            raise RuntimeError("forced failure for the fail-closed check")
        if path_error:
            raise path_error
        al = load_allowlist(path, cwd)
        if tool == "Bash":
            decision, reason = bash(tool_input.get("command", ""), cwd, al)
        else:
            decision, reason = edit(tool_input.get(EDIT_TOOLS[tool], ""), cwd, al)
    except Denied as exc:
        decision, reason = "deny", f"{exc}; denied (fail-closed for the fix agent)."
    except Exception as exc:  # fail closed for the fix agent type only
        decision, reason = "deny", f"the gate hook failed ({type(exc).__name__}); denied (fail-closed for the fix agent)."
    decide_and_log(decision, reason, log_targets(path),
                   {"ts": time.time(), "agent_id": data.get("agent_id"), "tool": tool,
                    "input": tool_input.get("command") or tool_input.get(EDIT_TOOLS.get(tool, ""), ""), "cwd": cwd})


if __name__ == "__main__":
    main()
