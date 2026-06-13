# Implement Feature

## Steps

1. **Understand** — Read related existing files. Check the same domain's router, service, and schema.
2. **Plan** — List which files to create or modify.
3. **Schema first** — Define input/output shapes with Pydantic models.
4. **Implement service** — Business logic. Delegate external calls to client.
5. **Wire router** — Add endpoint, connect schema.
6. **Write tests** — Add pytest cases for new behavior.
7. **Validate** — `uv run pytest && uv run ruff check .`
8. **Report** — Changed files, validation result, remaining risks.

## Notes

- Follow existing patterns. Explain if introducing a new pattern.
- Access env vars only through the `Settings` class.
- Isolate external APIs with mocks or `dependency_overrides` in tests.
