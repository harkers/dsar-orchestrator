#!/usr/bin/env bash
# PostToolUse hook: after edits to the orchestrator's stage/CLI surface,
# inject a reminder to audit the operator runbook + guide and to run the
# drift check before finishing.
#
# Fires for Edit|Write|MultiEdit (per the matcher in .claude/settings.json).
# Reads the tool input JSON on stdin; emits a JSON additionalContext payload
# on stdout only when the touched file is one of the watched surfaces.
# Exits silently otherwise.

set -euo pipefail

input="$(cat)"
file_path="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')"
[ -z "$file_path" ] && exit 0

case "$file_path" in
  */src/dsar_orchestrator/pipeline.py | \
  */src/dsar_orchestrator/cli.py | \
  */src/dsar_orchestrator/stages.py)
    ;;
  *)
    exit 0
    ;;
esac

cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "You edited the orchestrator's stage / CLI surface (pipeline.py, cli.py, or stages.py). Before finishing this task, audit:\n  - docs/runbooks/dsar-operator-loop.md (stage ladder table, retry policy addenda, fast path)\n  - docs/operator-guide.md (stage-count claim, env vars, troubleshooting table)\n  - docs/runbooks/ralph-dsar-prompt.md (only if completion criteria or hard rules need to change)\nThen run `python tools/check_runbook_drift.py` and confirm it exits 0."
  }
}
JSON
