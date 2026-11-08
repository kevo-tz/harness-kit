#!/usr/bin/env bash
# harness-kit installer. Safe to re-run; existing files are backed up first.
#   ./install.sh --global                  install into ~/.claude (every project)
#   ./install.sh /path/to/repo             install into one repo's .claude/
#   ./install.sh --uninstall --global      remove from ~/.claude
#   ./install.sh --uninstall /path/to/repo remove from that repo
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)"
ts="$(date +%Y%m%d-%H%M%S)"

usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 1; }
need() { command -v "$1" >/dev/null || { echo "missing dependency: $1" >&2; exit 1; }; }
need jq; need python3; need git

mode=install
if [[ ${1:-} == --uninstall ]]; then mode=uninstall; shift; fi
target="${1:-}"; [[ -n $target ]] || usage

if [[ $target == --global ]]; then
  scope=global
  base="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  memory="$base/CLAUDE.md"
  script="$base/hooks/harness-kit.py"
  hook_cmd="python3 \"$script\""
  agent_ref="$script"
else
  root="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || { echo "Not a git repo: $target" >&2; exit 1; }
  scope=project
  base="$root/.claude"
  memory="$root/CLAUDE.md"
  hook_cmd='python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/harness-kit.py"'
  agent_ref='"$(git rev-parse --show-toplevel)/.claude/hooks/harness-kit.py"'
fi
settings="$base/settings.json"
mkdir -p "$base/hooks" "$base/agents"

backup() { [[ -e $1 ]] && cp -a "$1" "$1.bak-$ts" && echo "  backup: $1.bak-$ts" || true; }

# Drop our hook entries (and the old commit-gate.sh one) from a settings file, then tidy empties.
strip_jq='
  def strip: map(.hooks |= map(select((.command // "") | (contains("harness-kit.py") or contains("commit-gate.sh")) | not)))
             | map(select(.hooks | length > 0));
  if .hooks then
    .hooks |= (with_entries(.value |= strip) | with_entries(select(.value | length > 0)))
    | if .hooks == {} then del(.hooks) else . end
  else . end'

# Replace or remove the marked block in CLAUDE.md.
edit_memory() {  # $1 = file, $2 = block source or empty to remove
  python3 -I - "$1" "${2:-}" <<'PY'
import os, re, sys
path, block_src = sys.argv[1], sys.argv[2]
start, end = "<!-- harness-kit:start -->", "<!-- harness-kit:end -->"
text = open(path).read() if os.path.exists(path) else ""
text = re.sub(r"\n*" + re.escape(start) + r".*?" + re.escape(end) + r"\n*", "\n\n", text, flags=re.S).strip()
if block_src:
    block = f"{start}\n{open(block_src).read().strip()}\n{end}"
    text = f"{text}\n\n{block}" if text else block
if text:
    open(path, "w").write(text + "\n")
elif os.path.exists(path):
    os.remove(path)
PY
}

if [[ $mode == uninstall ]]; then
  rm -f "$base/hooks/harness-kit.py"
  for a in verifier skeptic; do
    f="$base/agents/$a.md"; grep -q 'harness-kit' "$f" 2>/dev/null && { backup "$f"; rm -f "$f"; }
  done
  if [[ -f $settings ]]; then
    backup "$settings"; tmp="$(mktemp)"; jq "$strip_jq" "$settings" > "$tmp" && mv "$tmp" "$settings"
  fi
  [[ -f $memory ]] && { backup "$memory"; edit_memory "$memory"; }
  echo "Uninstalled from $base."
  echo "Left in place (remove by hand if unwanted): model, advisorModel$([[ $scope == global ]] && echo ", permissions.defaultMode") in $settings."
  exit 0
fi

echo "Installing harness-kit ($scope) into $base"

install -m 0755 "$src/.claude/hooks/harness-kit.py" "$base/hooks/harness-kit.py"
for old in commit-gate.sh diff-hash.sh approve-diff.sh; do  # files from the previous version
  [[ -e $base/hooks/$old ]] && { rm -f "$base/hooks/$old"; echo "  removed old hooks/$old"; }
done

for a in verifier skeptic; do
  out="$base/agents/$a.md"; new="$(mktemp)"
  python3 -I - "$src/.claude/agents/$a.md" "$agent_ref" > "$new" <<'PY'
import sys
text = open(sys.argv[1]).read().replace("{{HARNESS}}", sys.argv[2])
print(text.rstrip() + "\n\n<!-- installed by harness-kit -->")
PY
  if [[ -e $out ]] && ! cmp -s "$new" "$out"; then backup "$out"; fi
  mv "$new" "$out"; chmod 0644 "$out"
done

[[ -f $settings ]] || echo '{}' > "$settings"
jq empty "$settings" 2>/dev/null || { echo "$settings is not valid JSON; fix it and re-run." >&2; exit 1; }
backup "$settings"
tmp="$(mktemp)"
jq --arg cmd "$hook_cmd" --argjson project "$([[ $scope == project ]] && echo true || echo false)" "$strip_jq"'
  | .hooks.PreToolUse   = ((.hooks.PreToolUse   // []) + [
      {matcher: "Bash",                         hooks: [{type: "command", command: ($cmd + " gate")}]},
      {matcher: "Edit|Write|MultiEdit|NotebookEdit", hooks: [{type: "command", command: ($cmd + " gate")}]}])
  | .hooks.SubagentStop = ((.hooks.SubagentStop // []) + [
      {hooks: [{type: "command", command: ($cmd + " record")}]}])
  | .model = (.model // "sonnet")
  | .advisorModel = (.advisorModel // "opus")
  | if $project then . else .permissions.defaultMode = (.permissions.defaultMode // "auto") end
' "$settings" > "$tmp" && mv "$tmp" "$settings"

[[ -f $memory ]] && backup "$memory"
edit_memory "$memory" "$src/CLAUDE.md"

# Old versions kept the marker in the repo and gitignored it; it now lives in .git/.
if [[ $scope == project && -f $root/.gitignore ]] && grep -qxF '.claude/.skeptic-approved' "$root/.gitignore"; then
  echo "  note: '.claude/.skeptic-approved' in $root/.gitignore is no longer needed."
fi

echo "Done."
if [[ $scope == global ]]; then
  echo "  model: $(jq -r .model "$settings")  advisor: $(jq -r .advisorModel "$settings")  permissions: $(jq -r .permissions.defaultMode "$settings")"
  echo "  (existing values were kept; edit $settings to change them)"
else
  echo "  note: auto permission mode can only be set in user settings (install.sh --global), not per repo."
  echo "  Start with: cd $root && claude"
fi
echo "  Disable the commit gate for one session with: HARNESS_KIT=off claude"
