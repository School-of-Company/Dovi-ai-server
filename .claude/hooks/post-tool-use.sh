#!/usr/bin/env bash
# Claude Code PostToolUse hook

INPUT=$(cat)

TOOL_NAME=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('tool_name', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

COMMAND=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('tool_input', {}).get('command', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

if [ "$TOOL_NAME" = "Bash" ] && echo "$COMMAND" | grep -qE '^git push'; then
  >&2 echo "[hook] git push executed — confirm this was intentional."
fi

exit 0
