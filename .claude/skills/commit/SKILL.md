# Commit

## When to Commit

Only when the user explicitly requests it: "커밋해줘", "commit 해줘", "commit this", etc.
Do not auto-commit after completing work.

## Pre-Commit Checks

1. `git status` — verify staged/unstaged state.
2. `git diff --staged` — confirm what will be committed.
3. Block if any of these are staged:
   - `.env`, `.env.*`
   - `.claude/.logs/`
   - `*.pem`, `*.p8`, `private-key.*`
4. Block if on a protected branch (`main`).

## Staging

Add files by name. Never use `git add -A` or `git add .`.

```bash
git add app/api/health.py tests/test_health.py
```

Leave unrelated files unstaged. Notify the user if any are skipped.

## Message Format

```
type :: 한국어 설명
```

- type: `feat`, `fix`, `chore`, `refactor`, `test`, `docs`
- Korean description only
- No trailing period
- Under 70 characters total

Examples:
- `feat :: GitHub webhook 서명 검증 추가`
- `fix :: installation token 만료 처리 누락 수정`
- `test :: health endpoint 테스트 추가`

## Commit Execution

```bash
git commit -m "$(cat <<'EOF'
type :: 한국어 설명
EOF
)"
```

## Verification

```bash
git log --oneline -3
```

## Push Protocol

Push only when the user explicitly requests it. Never combine push with commit.
Never push to `main`.

```bash
git push origin <branch-name>
```
