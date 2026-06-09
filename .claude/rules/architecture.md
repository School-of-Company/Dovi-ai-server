# Dovi Architecture Rules

## Layer Structure

```
webhook/ — Entry point. Request validation and event routing only.
github/  — GitHub API I/O. Auth, diff fetching, comment posting.
review/  — Core orchestration. diff → LLM → comment assembly.
llm/     — Anthropic API I/O. Prompt generation and response parsing.
common/  — Shared exceptions and logger.
```

## Dependency Direction

```
webhook → review → github
                 → llm
```

Reverse dependencies are forbidden (e.g., github → webhook).

## File Responsibilities

- `router.py`: endpoint definitions, request/response types only. No business logic.
- `service.py`: business logic and orchestration. No direct HTTP calls.
- `client.py`: external API calls only. No logic.
- `schema.py`: Pydantic models. No transformation logic.
- `auth.py`: auth token creation and refresh only.

## Abstraction Principle

- Do not introduce abstractions before a second real use case exists
- Move shared utilities to `common/` only when used in three or more places

## Async

- FastAPI endpoints default to `async def`
- Use `httpx.AsyncClient` for external API calls
- Offload CPU-bound work (e.g., model inference) to `asyncio.run_in_executor` or a separate worker

## Configuration

- All settings go through the `Settings` class in `app/core/config.py`
- Inject via `get_settings()` dependency
- Design for `dependency_overrides` replacement in tests
