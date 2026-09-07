#!/usr/bin/env bash
# install.sh — copy the workflow skills into agent skill directories and put
# the `flow` command on PATH. POSIX shells and Git Bash on Windows.
#
#   bash install.sh                        # every skill → claude, codex, agents; plus the flow shim
#   bash install.sh --agent claude         # one destination: claude | codex | agents | all
#   bash install.sh --skill flow --skill model-debate
#   bash install.sh --no-shim              # skills only
#   bash install.sh --uninstall            # remove what this script installs
#
# Destinations: ~/.claude/skills (Claude Code), ~/.codex/skills (Codex),
# ~/.agents/skills (Kimi Code, Grok Build). The installer REPLACES a same-name
# skill directory at each destination; keep local edits elsewhere.
# On Windows prefer install.ps1, which links one canonical copy instead of
# copying three times.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="$REPO_DIR/skills"
BIN_DIR="${FLOW_BIN_DIR:-$HOME/.local/bin}"

agent=all
shim=1
uninstall=0
skills=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent) agent="$2"; shift 2 ;;
    --skill) skills+=("$2"); shift 2 ;;
    --no-shim) shim=0; shift ;;
    --uninstall) uninstall=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ ${#skills[@]} -eq 0 ]]; then
  for d in "$SKILLS_DIR"/*/; do
    [[ -f "$d/SKILL.md" ]] && skills+=("$(basename "$d")")
  done
fi

case "$agent" in
  claude) dests=("$HOME/.claude/skills") ;;
  codex)  dests=("$HOME/.codex/skills") ;;
  agents|kimi|grok) dests=("$HOME/.agents/skills") ;;
  all)    dests=("$HOME/.claude/skills" "$HOME/.codex/skills" "$HOME/.agents/skills") ;;
  *) echo "unknown agent: $agent (claude | codex | agents | all)" >&2; exit 1 ;;
esac

if [[ $uninstall -eq 1 ]]; then
  for s in "${skills[@]}"; do
    for d in "${dests[@]}"; do
      if [[ -e "$d/$s" ]]; then rm -rf "$d/$s"; echo "removed  $d/$s"; fi
    done
  done
  for f in "$BIN_DIR/flow" "$BIN_DIR/flow.cmd"; do
    if [[ -e "$f" ]]; then rm -f "$f"; echo "removed  $f"; fi
  done
  echo "Per-project files (.gate/, TASK_QUEUE.md, CONTEXT.md, .git/hooks/pre-commit) are untouched."
  exit 0
fi

for s in "${skills[@]}"; do
  src="$SKILLS_DIR/$s"
  if [[ ! -f "$src/SKILL.md" ]]; then
    echo "no such skill: $s (expected $src/SKILL.md)" >&2; exit 1
  fi
  for d in "${dests[@]}"; do
    mkdir -p "$d"
    rm -rf "$d/$s"
    cp -r "$src" "$d/$s"
    echo "installed $s -> $d/$s"
  done
done

if [[ $shim -eq 1 ]]; then
  py=""
  for candidate in python python3; do
    if command -v "$candidate" >/dev/null 2>&1; then py="$candidate"; break; fi
  done
  if [[ -z "$py" ]]; then
    echo "python not found on PATH; skipping the flow shim (install Python 3.10+ and re-run)" >&2
  else
    existing="$(command -v flow 2>/dev/null || true)"
    if [[ -n "$existing" && "$existing" != "$BIN_DIR/flow" && "$existing" != "$BIN_DIR/flow.cmd" ]]; then
      echo "WARNING: another 'flow' is already on PATH at $existing (Facebook's Flow type checker uses the same name)." >&2
      echo "         Put $BIN_DIR ahead of it in PATH, or call: python \"$REPO_DIR/flow/flow.py\"" >&2
    fi
    mkdir -p "$BIN_DIR"
    printf '#!/bin/sh\nexec %s "%s/flow/flow.py" "$@"\n' "$py" "$REPO_DIR" > "$BIN_DIR/flow"
    chmod +x "$BIN_DIR/flow"
    echo "shim      $BIN_DIR/flow"
    if command -v cygpath >/dev/null 2>&1; then
      win_repo="$(cygpath -w "$REPO_DIR")"
      printf '@%s "%s\\flow\\flow.py" %%*\r\n' "$py" "$win_repo" > "$BIN_DIR/flow.cmd"
      echo "shim      $BIN_DIR/flow.cmd"
    fi
    case ":$PATH:" in
      *":$BIN_DIR:"*) ;;
      *) echo "NOTE: add $BIN_DIR to PATH so that \`flow\` resolves." ;;
    esac
  fi
fi

cat <<EOF

Invoke the same skill in each CLI (the name is identical; only the sigil differs):
  Claude Code   /flow        /flow-run        /run-queue        /model-debate
  Codex         \$flow        \$flow-run        \$run-queue        \$model-debate
  Kimi Code     /skill:flow  /skill:flow-run  /skill:run-queue  /skill:model-debate
  Grok Build    /flow        /flow-run        /run-queue        /model-debate

Per project, once:  flow init --repo <project> --verify-cmd "<acceptance command>"
Start a new agent session if a skill does not appear yet.
EOF
