# Write PR

## Steps

1. `git log main..HEAD --oneline` — list commits.
2. `git diff main...HEAD --stat` — changed file stats.
3. `git diff main...HEAD` — full diff.
4. Draft PR title and body.
5. Run `gh pr create` to create the PR.

## PR Title Format

- `<type>: <description>`
- `<type>: <description> (#123)` — when there is an issue number

type: feat, fix, chore, refactor, test, docs
Max 70 characters. Use present-tense verb in description.

## PR Body Format

Fill in the sections from `.github/PULL_REQUEST_TEMPLATE.md`:

- **✨ 작업 내용**: what this PR does
- **🔍 리뷰 시 참고사항**: motivation, context, trade-offs
- **✅ 체크리스트**: check each item after verifying it
- **📎 관련 이슈**: `Close #number` if applicable

## Creating the PR

Run `gh pr create` as part of the skill execution:

```bash
gh pr create --title "<type>: <description>" --body "$(cat <<'EOF'
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
EOF
)"
```

## Rules

- Do not include anything not in the diff.
- Only check items you have actually verified.
- Branch must be pushed before running `gh pr create`.
