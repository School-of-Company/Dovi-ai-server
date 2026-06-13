# Write PR

## Steps

1. `git log main..HEAD --oneline` — list commits.
2. `git diff main...HEAD --stat` — changed file stats.
3. `git diff main...HEAD` — full diff.
4. Propose 3 Korean PR title candidates and ask the user to pick one.
5. Wait for the user to select a title.
6. Write the PR body to `/tmp/pr_body.md`.
7. Run `gh pr create` with `--body-file /tmp/pr_body.md`.

## PR Title Format

- Korean only. No type prefix (`feat:`, `chore:`, etc.).
- Concise, present-tense description of what this PR does.
- Max 50 characters.

Example candidates:
```
1. FastAPI 환경 초기화 및 Claude Code 하네스 구축
2. 개발 환경 세팅 및 코드 리뷰 자동화 하네스 추가
3. FastAPI 기본 구조와 Claude Code 운영 설정 추가
```

## PR Body Format

Fill in the sections from `.github/PULL_REQUEST_TEMPLATE.md`:

- **✨ 작업 내용**: what this PR does
- **🔍 리뷰 시 참고사항**: motivation, context, trade-offs
- **✅ 체크리스트**: check each item after verifying it
- **📎 관련 이슈**: `Close #number` if applicable

## Creating the PR

Write body to a temp file, then create the PR:

```bash
cat > /tmp/pr_body.md << 'BODY'
## ✨ 작업 내용
> ...

---

## 🔍 리뷰 시 참고사항
- ...

---

## ✅ 체크리스트
- [ ] 문서(README, `.env.example` 등) 변경이 필요한 경우 작성 또는 수정했나요?
- [ ] 작업한 코드가 정상적으로 동작하는 것을 직접 확인했나요?
- [ ] 필요한 경우 테스트 코드를 작성하거나 수정했나요?
- [ ] Merge 대상 브랜치를 올바르게 설정했나요?
- [ ] PR에 관련 없는 작업이 포함되지 않았나요?
- [ ] 적절한 라벨과 리뷰어를 설정했나요?

---

## 📎 관련 이슈(선택)
- Close #
BODY

gh pr create --title "<선택한 제목>" --body-file /tmp/pr_body.md
```

## Rules

- Do not include anything not in the diff.
- Only check items you have actually verified.
- Always propose 3 title candidates and wait for user selection before creating the PR.
- Branch must be pushed before running `gh pr create`.
- Always use `--body-file` (never inline heredoc) to avoid hook parse errors.
