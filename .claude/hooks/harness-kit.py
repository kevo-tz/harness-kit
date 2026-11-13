#!/usr/bin/env python3
"""harness-kit: skeptic-gated git commits for Claude Code.

Subcommands:
  gate     PreToolUse hook (Bash, Edit, Write). Blocks `git commit` unless the
           pending change was approved, and blocks Claude from forging approval.
  record   SubagentStop hook. When the `skeptic` agent ends with
           `VERDICT: approved` and its REVIEWED fingerprint still matches the
           working tree, approval is recorded for exactly that change. A
           session started in a folder that is not a repo (a parent of several
           repos) works too: the repo is the one, in or one or two levels
           below the session folder, whose current fingerprint equals REVIEWED.
           The final message comes from `last_assistant_message`, or from the
           transcript in `agent_transcript_path` when the harness sends only that.
           A transcript without a VERDICT line yet is re-read for up to
           HARNESS_KIT_RECORD_WAIT seconds (default 10, at most 50), since the harness can
           run the hook before the final lines are written.
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
import time

MARKER = "claude-skeptic-approved"
SELF = "harness-kit.py"
OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
BUILTINS = {"status", "diff", "log", "show", "add", "rm", "mv", "restore", "branch", "switch",
            "checkout", "fetch", "pull", "push", "rev-parse", "ls-files", "grep", "blame", "config",
            "stash", "tag", "remote", "init", "clone", "reset", "worktree", "merge", "rebase"}


def log(text):
    """One line per hook call in ~/.claude/harness-kit.log (HARNESS_KIT_LOG overrides the path), so a hook that
    seems not to fire can be told from one that fired and recorded nothing. Never holds message content."""
    import time
    path = os.environ.get("HARNESS_KIT_LOG") or os.path.expanduser("~/.claude/harness-kit.log")
    try:
        with open(path, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


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
            shutil.copy2(real_index, tmp_index)  # keeps the stat cache (fast) and the index time (git's racy-clean check)
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


def _text_of(blocks):
    return "\n".join(b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str))


# Blocks a finished report's message may hold. Anything else (tool_use, server_tool_use, ...) means the
# message went on to do something and the agent never finished. The exception: a message that ends in
# its SubagentHandback may also hold the completed server-side tool calls (such as an advisor
# consultation: server_tool_use plus its *_tool_result) that came before it. That is how a skeptic that
# asked the advisor first writes its final message, and the handback is then the explicit report.
_QUIET_BLOCKS = ("text", "thinking", "redacted_thinking")


def _quiet(block, handback_ends_the_message):
    kind = block.get("type")
    if kind in _QUIET_BLOCKS:
        return True
    return handback_ends_the_message and (kind == "server_tool_use" or str(kind).endswith("_tool_result"))


def last_assistant_text(path):
    """The final report in a Claude Code subagent transcript (JSONL), or "".

    Newer Claude Code versions give the SubagentStop hook `agent_transcript_path`
    and no `last_assistant_message`; without reading the transcript every verdict
    looks empty and nothing is ever approved.

    The final report belongs to the message of the LAST assistant line. Claude
    Code writes every content block of a message as its own line, and tool
    results and attachments can sit between two lines of one message, so all
    lines with that message id are gathered from anywhere in the file. The report
    is the `message` of its `SubagentHandback` tool call, or else the joined text
    of those lines. It counts only if the message holds nothing but text and
    thinking (besides the handback): if it also made another tool call, or the
    last line is unreadable, the agent never finished, and an earlier draft that
    may say "approved" must not be mistaken for the report."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [line for line in f.read().splitlines() if line.strip()]
    except (OSError, TypeError):
        return ""
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            entries.append(None)  # unreadable
    last = None
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        if not isinstance(entry, dict):
            return ""  # unreadable or not an object: it may have been the real report
        message = entry.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            last = i
            break
        # tool results, attachments and the like come after the report
    if last is None:
        return ""
    message_id = entries[last]["message"].get("id")
    final = [i for i, e in enumerate(entries)
             if isinstance(e, dict) and isinstance(e.get("message"), dict)
             and e["message"].get("role") == "assistant"
             and (i == last or (message_id is not None and e["message"].get("id") == message_id))]
    if entries[last]["message"].get("stop_reason") == "tool_use" and not _has_handback(entries[last]):
        return ""  # the message stopped to call a tool
    texts, handback = [], None
    for i in final:
        content = entries[i]["message"].get("content")
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
        else:
            return ""
        for block in blocks:
            if block.get("type") == "tool_use" and block.get("name") == "SubagentHandback":
                if i != last or handback is not None:
                    return ""  # the handback must be the very last line, and only one
                handback = block
            elif not _quiet(block, _has_handback(entries[last])):
                return ""  # another tool call (or a block we do not know): not a finished report
        texts.append(_text_of(blocks))
    if handback is not None:
        report = handback.get("input")
        report = report.get("message") if isinstance(report, dict) else None
        return report if isinstance(report, str) and report.strip() else ""
    text = "\n".join(t for t in texts if t)
    return text if text.strip() else ""


def _has_handback(entry):
    content = entry["message"].get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "SubagentHandback" for b in content)


def child_repos(folder, depth=2):
    """Git repos one or two levels below `folder`, for a session started in a parent folder of its repos.
    Hidden folders and symlinks are skipped."""
    found = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return found
    for name in names:
        path = os.path.join(folder, name)
        if name.startswith(".") or os.path.islink(path) or not os.path.isdir(path):
            continue
        if os.path.exists(os.path.join(path, ".git")):  # a directory, or a file in a worktree
            found.append(path)
        elif depth > 1:
            found += child_repos(path, depth - 1)
    return found


def candidate_roots(cwd):
    """The repo of `cwd` first, then the repos below it, without duplicates."""
    roots = []
    for path in [cwd, *child_repos(cwd)]:
        root = repo_root(path)
        if root and root not in roots:
            roots.append(root)
    return roots


def head_of(root):
    return git(["rev-parse", "--verify", "-q", "HEAD"], root, check=False) or "unborn"


VERDICT_RE = r"^\W*VERDICT\W*(approved|rejected)\b"


def transcript_report(path):
    """The final report from the transcript, re-read for a short while when it has no VERDICT line yet.

    Claude Code can run the SubagentStop hook before it has flushed the skeptic's last lines (the
    SubagentHandback) to the transcript; the hook then saw an earlier message and recorded nothing
    (`verdicts=[]` in the log). Waiting up to HARNESS_KIT_RECORD_WAIT seconds (default 10, at most 50,
    under the 60 s hook timeout) lets the write land. A report that is still missing after that is no
    report, as before."""
    msg = last_assistant_text(path)
    try:
        wait = float(os.environ.get("HARNESS_KIT_RECORD_WAIT", "10"))
    except ValueError:
        wait = 10.0
    wait = min(max(wait, 0.0), 50.0) if wait == wait else 0.0  # NaN -> 0; capped below the hook timeout
    deadline = time.monotonic() + wait
    while path and not re.search(VERDICT_RE, msg, re.I | re.M) and time.monotonic() < deadline:
        time.sleep(0.25)
        msg = last_assistant_text(path)
    return msg


def record(data):
    agent = data.get("agent_type") or ""
    log(f"record agent_type={agent!r} cwd={data.get('cwd')!r} keys={sorted(data)}")
    if agent.split(":")[-1] != "skeptic":
        return 0
    cwd = data.get("cwd") or os.getcwd()
    msg = data.get("last_assistant_message")
    if not (isinstance(msg, str) and msg.strip()):
        msg = transcript_report(data.get("agent_transcript_path"))
    verdicts = [v.lower() for v in re.findall(VERDICT_RE, msg, re.I | re.M)]
    reviewed = re.findall(r"^\W*REVIEWED\W*([0-9a-z]+:[0-9a-f]+)", msg, re.I | re.M)
    # A finding is serious when a line starts with [blocker] or [major], after anything that is not a letter or
    # digit (list markers of any kind, dashes, bullets, "#", ">", "|", emphasis, emoji, spaces) and optionally
    # "FINDINGS:" or "1." - so "- **[major]**", "> - [major]", "— [major]", "- 🔴 [major]", "[ major ]" all count.
    # A mention in the middle of a sentence ("no [major] findings") is not a finding.
    serious = re.search(r"^[^A-Za-z0-9\[\]\n]*(?:findings\s*:[^A-Za-z0-9\[\]\n]*)?(?:\d+[.)][^A-Za-z0-9\[\]\n]*)?"
                        r"\[\s*(blocker|major)\s*\]", msg, re.I | re.M)

    def say(text):
        log(f"record verdicts={verdicts} serious={bool(serious)} reviewed={reviewed or None} -> {text}")
        print(json.dumps({"systemMessage": f"harness-kit: {text}"}))
        return 0

    # The session may have been started in a folder that is not itself a repo
    # (a parent of several repos). The skeptic's REVIEWED fingerprint is HEAD
    # plus the tree, so it names exactly one repo state: use the repos whose
    # current fingerprint equals it, never a guess. Only repos whose HEAD is the
    # reviewed one are fingerprinted, so sibling repos are left alone.
    roots = candidate_roots(cwd)
    if not roots:
        return say(f"the skeptic finished, but no git repository was found in or below {cwd}; "
                   "nothing was recorded. Start Claude Code inside the repo, or approve by hand.")
    reviewed_fp = reviewed[0] if len(set(reviewed)) == 1 else None  # REVIEWED lines must agree
    reviewed_head = reviewed_fp.split(":")[0] if reviewed_fp else None
    same_head = [r for r in roots if reviewed_head and head_of(r) == reviewed_head]
    # The repos this review was about; when REVIEWED cannot be used, any of them might be.
    subjects = same_head or ([] if reviewed_fp else roots)
    match = []
    for root in same_head:
        try:
            if fingerprint(root) == reviewed_fp:
                match.append(root)
        except Exception:
            continue

    if verdicts != ["approved"] or serious:
        # A rejection clears the repos it was about; if REVIEWED matches none of them, any might be.
        for root in same_head or roots:
            clear_marker(root)
        return say("skeptic did not approve; commit stays blocked.")
    if not match:
        for root in subjects:
            clear_marker(root)
        return say("skeptic approved, but the change differs from what it reviewed (or REVIEWED was missing). "
                   "Re-run the skeptic.")
    for root in match:
        write_marker(root, reviewed_fp)
    return say(f"skeptic approval recorded for {reviewed_fp.split(':')[1][:12]} in {', '.join(match)}.")


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
        if sub == "gate":
            return gate(data)
        try:
            return record(data)
        except Exception as e:  # fail closed: nothing is approved, and the log says why
            log(f"record crashed: {type(e).__name__}: {e}")
            try:
                if (data.get("agent_type") or "").split(":")[-1] == "skeptic":
                    for root in candidate_roots(data.get("cwd") or os.getcwd()):
                        clear_marker(root)  # an earlier approval must not outlive a review that failed
                    print(json.dumps({"systemMessage": "harness-kit: the approval hook failed; approvals were "
                                      "cleared and the commit stays blocked. See ~/.claude/harness-kit.log."}))
            except Exception:
                pass
            return 0

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
