#!/usr/bin/env python3
"""harness-kit: skeptic-gated git commits for Claude Code.

Subcommands:
  gate     PreToolUse hook (Bash, Edit, Write). Blocks `git commit` unless the
           pending change was approved, and blocks Claude from forging approval.
  record   SubagentStop hook. When the `skeptic` agent ends with
           `VERDICT: approved` and its REVIEWED fingerprint still matches the
           working tree, approval is recorded for exactly that change.
  hash     Print the fingerprint of the pending change (the skeptic runs this).
  approve  Human override: approve the current change by hand. Run it in your
           own terminal; Claude is blocked from running it.
  status   Say whether the current change is approved.

The fingerprint is HEAD + the tree that `git add -A` would produce, computed in
a throwaway index. It ignores what is staged, so `git add` after approval keeps
the approval valid, while any edit, new file, deletion or new commit voids it.
The approval marker lives in the git dir, so it is never committed.

Set HARNESS_KIT=off in the environment to disable the gate for a session.

This is a guardrail against an agent skipping steps, not a security boundary:
an agent determined to bypass it can.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

MARKER = "claude-skeptic-approved"
SELF = "harness-kit.py"
OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
BUILTINS = {"status", "diff", "log", "show", "add", "rm", "mv", "restore", "branch", "switch",
            "checkout", "fetch", "pull", "push", "rev-parse", "ls-files", "grep", "blame", "config",
            "stash", "tag", "remote", "init", "clone", "reset", "worktree", "merge", "rebase"}


def git(args, cwd, env=None, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout.strip()


def repo_root(cwd):
    try:
        return git(["rev-parse", "--show-toplevel"], cwd)
    except Exception:
        return None


def git_path(root, name):
    p = git(["rev-parse", "--git-path", name], root)
    return p if os.path.isabs(p) else os.path.join(root, p)


def fingerprint(root):
    head = git(["rev-parse", "--verify", "-q", "HEAD"], root, check=False) or "unborn"
    tmpdir = tempfile.mkdtemp(prefix="harness-kit-")
    try:
        tmp_index = os.path.join(tmpdir, "index")
        real_index = git_path(root, "index")
        if os.path.exists(real_index):
            shutil.copyfile(real_index, tmp_index)  # keeps the stat cache, so this is fast
        env = {**os.environ, "GIT_INDEX_FILE": tmp_index}
        git(["add", "-A", "--", ":/"], root, env)
        tree = git(["write-tree"], root, env)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return f"{head}:{tree}"


def is_approved(root):
    try:
        with open(git_path(root, MARKER)) as f:
            return f.read().strip() == fingerprint(root)
    except FileNotFoundError:
        return False


def write_marker(root, fp):
    with open(git_path(root, MARKER), "w") as f:
        f.write(fp + "\n")


def clear_marker(root):
    try:
        os.unlink(git_path(root, MARKER))
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- command parsing

def git_commits(cmd, cwd):
    """Yield the directory of every `git commit` (or alias of it) in a shell command.

    Deliberately loose: quotes, $( ), backticks and parens are flattened so that
    `sh -c "git commit"` and `(cd x && git commit)` are still seen. `cd DIR` is
    tracked across `;`/`&&` so the right repo gets checked.
    """
    text = re.sub(r"\$\(|[`\"'(){}]", " ", cmd.replace("\\\n", " "))
    for seg in re.split(r"[;&|\n]+", text):
        toks = seg.split()
        if len(toks) >= 2 and toks[0] in ("cd", "pushd"):
            cwd = os.path.join(cwd, os.path.expanduser(toks[1]))
            continue
        for i, tok in enumerate(toks):
            if os.path.basename(tok) != "git":
                continue
            cdir, inline_alias, j = cwd, False, i + 1
            while j < len(toks) and toks[j].startswith("-"):
                opt = toks[j]
                j += 1
                if opt in OPTS_WITH_ARG and j < len(toks):
                    if opt == "-C":
                        cdir = os.path.join(cdir, os.path.expanduser(toks[j]))
                    elif opt == "-c" and toks[j].startswith("alias."):
                        inline_alias = True
                    j += 1
            if j >= len(toks):
                continue
            sub = toks[j]
            if sub == "commit" or inline_alias or (sub not in BUILTINS and _alias_commits(sub, cdir)):
                yield cdir


def _alias_commits(sub, cwd):
    alias = git(["config", "--get", f"alias.{sub}"], cwd if os.path.isdir(cwd) else None, check=False)
    return bool(re.search(r"\bcommit\b", alias))


def block(msg):
    print(msg, file=sys.stderr)
    return 2


# ---------------------------------------------------------------- hook handlers

def gate(data):
    if os.environ.get("HARNESS_KIT", "").lower() in ("off", "0", "false"):
        return 0
    tool = data.get("tool_name", "")
    tin = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()

    if tool != "Bash":
        # Edit / Write / NotebookEdit: only guard the approval marker.
        if MARKER in json.dumps(tin):
            return block("BLOCKED: the skeptic approval marker is written by the harness-kit hook, not by you.")
        return 0

    cmd = tin.get("command", "")
    if MARKER in cmd or re.search(re.escape(SELF) + r"\S*\s+approve\b", cmd):
        return block("BLOCKED: approval comes from the skeptic subagent (recorded by a hook) or from the user. "
                     "Do not write the marker or run `harness-kit.py approve` yourself.")

    for cdir in git_commits(cmd, cwd):
        root = repo_root(cdir)
        if not root:
            continue
        try:
            if is_approved(root):
                continue
        except Exception as e:  # fail closed
            return block(f"BLOCKED: harness-kit could not fingerprint the change in {root}: {e}")
        return block(
            f"BLOCKED: the pending change in {root} has no skeptic approval (or changed since it was approved). "
            "Run the verifier, then the skeptic subagent on the full change, giving it the goal and the verifier "
            "output. A hook records approval automatically when it ends with `VERDICT: approved`; then retry the "
            "commit. If it rejects, fix the findings and re-run both. After 3 rejections, stop and ask the user."
        )
    return 0


def record(data):
    if (data.get("agent_type") or "").split(":")[-1] != "skeptic":
        return 0
    root = repo_root(data.get("cwd") or os.getcwd())
    if not root:
        return 0
    msg = data.get("last_assistant_message") or ""
    verdicts = [v.lower() for v in re.findall(r"^\W*VERDICT\W*(approved|rejected)\b", msg, re.I | re.M)]
    reviewed = re.findall(r"^\W*REVIEWED\W*([0-9a-z]+:[0-9a-f]+)", msg, re.I | re.M)
    serious = re.search(r"^\s*[-*]\s*\[(blocker|major)\]", msg, re.I | re.M)

    def say(text):
        print(json.dumps({"systemMessage": f"harness-kit: {text}"}))
        return 0

    if verdicts != ["approved"] or serious:
        clear_marker(root)
        return say("skeptic did not approve; commit stays blocked.")
    current = fingerprint(root)
    if not reviewed or reviewed[-1] != current:
        clear_marker(root)
        return say("skeptic approved, but the change differs from what it reviewed (or REVIEWED was missing). "
                   "Re-run the skeptic.")
    write_marker(root, current)
    return say(f"skeptic approval recorded for {current.split(':')[1][:12]}.")


# ---------------------------------------------------------------- CLI

def main():
    sub = sys.argv[1] if len(sys.argv) > 1 else ""
    if sub in ("gate", "record"):
        raw = sys.stdin.read()
        try:
            data = json.loads(raw)
        except Exception:
            if sub == "gate" and "commit" in raw:
                return block("BLOCKED: harness-kit could not parse the hook input; refusing a possible commit.")
            return 0
        return gate(data) if sub == "gate" else record(data)

    root = repo_root(os.getcwd())
    if sub in ("hash", "approve", "status"):
        if not root:
            print("not inside a git repository", file=sys.stderr)
            return 1
        if sub == "hash":
            print(fingerprint(root))
        elif sub == "approve":
            fp = fingerprint(root)
            write_marker(root, fp)
            print(f"approved {fp.split(':')[1][:12]} in {root}")
        else:
            print("approved" if is_approved(root) else "NOT approved")
        return 0

    print(__doc__.strip(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
