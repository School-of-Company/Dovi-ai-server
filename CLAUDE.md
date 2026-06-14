# Dovi AI Server — Claude Code Operating Guide

## Project Overview

Dovi is a GitHub App-based AI code review service.
It receives PR events via GitHub webhooks, analyzes diffs, generates LLM reviews, and posts them as PR comments.

**Package manager:** uv
**Python:** 3.13
**Status:** Early development

> **Response language:** Always respond in Korean.

---

## Validation Commands

```bash
# Required
uv run pytest
uv run ruff check .

# Recommended
uv run mypy .

# Run
uv run uvicorn app.main:app --reload
```

---

## Project Structure

```
dovi-ai-server/
├── app/
│   ├── main.py                # FastAPI app creation and router registration
│   ├── core/
│   │   └── config.py          # Pydantic BaseSettings (env-based)
│   ├── api/
│   │   └── health.py          # GET /health
│   ├── webhook/               # GitHub webhook ingestion (planned)
│   │   ├── router.py
│   │   ├── service.py
│   │   └── schema.py
│   ├── github/                # GitHub API client (planned)
│   │   ├── client.py
│   │   ├── auth.py
│   │   └── schema.py
│   ├── review/                # AI review orchestration (planned)
│   │   ├── service.py
│   │   ├── diff.py
│   │   └── schema.py
│   ├── llm/                   # LLM integration (planned)
│   │   ├── client.py
│   │   └── prompt.py
│   └── common/
│       ├── exceptions.py
│       └── logger.py
└── tests/
    ├── conftest.py
    ├── test_health.py
    ├── test_webhook.py        (planned)
    └── test_review.py         (planned)
```

---

## Dovi Domain Flow

```
GitHub → POST /webhook
  → Verify webhook signature (HMAC-SHA256)
  → Identify pull_request event
  → Fetch PR diff (GitHub API)
  → Analyze changed files
  → Request LLM review (Anthropic API)
  → Generate review comments
  → Post comments to GitHub PR
  → Retry or log on failure
```

---

## Agent Routing

| Task | Agent |
|------|-------|
| Add new feature | `feature-agent` |
| Fix a bug | `fix-agent` |
| Write tests | `test-agent` |
| Code review | `review-agent` |
| Write PR description | `pr-agent` |
| Apply review feedback | `feedback-agent` |

---

## Feature Development Flow

1. `git status` — check current state
2. Create branch: `git checkout -b feat/<scope>`
3. Implement with `feature-agent`
4. Verify with `uv run pytest`
5. Verify with `uv run ruff check .`
6. Review diff with `review-agent`
7. Draft PR with `pr-agent`

## Bug Fix Flow

1. Reproduce the bug
2. `git checkout -b fix/<scope>`
3. Apply minimal fix with `fix-agent`
4. Add regression test
5. Verify with `uv run pytest`

## Test Writing Flow

- Write pytest-based tests with `test-agent`
- Use `TestClient` or `AsyncClient`
- Replace dependencies via `app.dependency_overrides`
- Define fixtures in `tests/conftest.py`

---

## Git Rules

- No direct commits to `main`
- Do not commit or push without explicit request
- Always run `git status` before starting work
- Branch naming: `feat/<scope>`, `fix/<scope>`, `chore/<scope>`

## Coding Standards

- Write schemas with Pydantic v2
- Access env vars only through `Settings` in `app/core/config.py`
- Use `async def` + `await` for async functions
- Type hints required
- Write comments only when the WHY is non-obvious

## Security Rules Summary

> Full rules: `.claude/rules/security.md`

- Do not read or print `.env`, `.env.*` files
- Never output GitHub App private key, webhook secret, or LLM API key
- Never hardcode credentials in code
- Never include tokens, keys, or personal data in logs

## Architecture Rules Summary

> Full rules: `.claude/rules/architecture.md`

- Separate `router`, `service`, `schema` per domain
- Business logic goes in service; I/O goes in client
- Do not introduce abstractions before a second real use case
