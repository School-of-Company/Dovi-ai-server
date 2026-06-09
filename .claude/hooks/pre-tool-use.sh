#!/usr/bin/env bash
# Claude Code PreToolUse hook — block dangerous commands

INPUT=$(cat)

PARSED=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    tool = d.get('tool_name', '')
    cmd = d.get('tool_input', {}).get('command', '')
    print(tool + '|||' + cmd)
except Exception:
    print('|||')
")

TOOL_NAME="${PARSED%%|||*}"
COMMAND="${PARSED#*|||}"

if [ "$TOOL_NAME" != "Bash" ]; then
  exit 0
fi

# Block dangerous commands
BLOCKED_PATTERNS=(
  "git push --force"
  "git push -f "
  "git reset --hard"
  "git clean -f"
  "rm -rf"
  "docker compose down -v"
  "docker volume rm"
  "docker volume prune"
)

for pattern in "${BLOCKED_PATTERNS[@]}"; do
  if echo "$COMMAND" | grep -qF "$pattern"; then
    echo "BLOCKED: '$pattern' is not allowed."
    exit 2
  fi
done

# Block direct reading of .env files
if echo "$COMMAND" | grep -qE '(cat|head|tail|less|more)\s+[^ ]*\.env([. ]|$)'; then
  echo "BLOCKED: Reading .env files directly is not allowed."
  exit 2
fi

# Block reading secrets or private keys
if echo "$COMMAND" | grep -qE '(cat|head|tail|less|more|open)\s+[^ ]*(secrets/|private[_.-]key|\.pem|\.p8)'; then
  echo "BLOCKED: Reading secrets or private key files is not allowed."
  exit 2
fi

# Block dangerous GitHub API mutation requests
if echo "$COMMAND" | grep -qE 'gh\s+api.*-X\s+(DELETE|PATCH|PUT)'; then
  echo "BLOCKED: GitHub API mutation requests (DELETE/PATCH/PUT) require explicit approval."
  exit 2
fi

exit 0
