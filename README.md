# harness-kit

A multi-model harness for [Claude Code](https://claude.com/claude-code): Sonnet does the work, an Opus advisor weighs in on hard calls, a Haiku **verifier** runs tests, and an Opus **skeptic** must approve every commit.

## How the commit gate works

1. Before `git commit`, the main session runs the verifier, then the skeptic on the full pending change.
2. The skeptic fingerprints the change first, attacks it, and ends with `VERDICT: approved | rejected` plus the fingerprint it reviewed.
3. A `SubagentStop` hook records approval only if the verdict is `approved`, there are no `[blocker]`/`[major]` findings, and the fingerprint still matches the working tree.
4. A `PreToolUse` hook blocks `git commit` (including `sh -c "…"`, `git -C`, aliases) unless the current change is approved, and blocks the agent from writing the approval itself.

The fingerprint is `HEAD` + the tree `git add -A` would produce, so staging doesn't void approval but any edit, new file or commit does. The marker lives in `.git/`, so it is never committed.

A session may be started in a folder that holds several repos (for example `~/Work`). The hook then finds the repo whose current fingerprint equals the one the skeptic reviewed, looking in the session folder and one or two levels below it, so approval can only land on the exact change that was reviewed.

Tests: `python3 -m unittest discover -s tests -v`.

This is a guardrail against an agent skipping steps, not a security boundary.

## Install

Requires `git`, `jq`, `python3`.

```sh
./install.sh --global              # ~/.claude: every project
./install.sh /path/to/repo         # one repo's .claude/
./install.sh --uninstall --global  # or --uninstall /path/to/repo
```

The installer is idempotent and backs up every file it changes. Globally it sets, only if unset: `model: sonnet`, `advisorModel: opus`, `permissions.defaultMode: auto` (auto mode is user-settings only, so per-repo installs skip it).

## Day to day

```sh
HARNESS_KIT=off claude                              # disable the gate for one session
python3 ~/.claude/hooks/harness-kit.py status       # is the current change approved?
python3 ~/.claude/hooks/harness-kit.py approve      # human override, from your own terminal
```

The `record` hook re-reads a skeptic transcript that has no `VERDICT` line yet for up to `HARNESS_KIT_RECORD_WAIT` seconds (default 10, at most 50), because Claude Code can run SubagentStop before the final report is written. A skeptic run that never reports (an API error, for example) therefore holds the session for that long before recording nothing.

Not gated: `merge`, `rebase`, `cherry-pick`, `revert` (they replay existing commits rather than new working-tree changes).
