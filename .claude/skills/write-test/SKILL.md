# Write Test

## Steps

1. **Understand** — Read the code under test. Identify inputs, outputs, and side effects.
2. **Check fixtures** — See if `tests/conftest.py` already has reusable fixtures.
3. **Write cases** — Happy path → error cases → edge cases.
4. **Isolate external deps** — Mock GitHub API and Anthropic API, or use `dependency_overrides`.
5. **Run** — `uv run pytest tests/<file> -v`

## TestClient Pattern

```python
from fastapi.testclient import TestClient

def test_<what>(client: TestClient) -> None:
    response = client.post("/endpoint", json={...})
    assert response.status_code == 200
    assert response.json()["key"] == "expected"
```

## Dependency Override Pattern

```python
def test_with_override(client: TestClient) -> None:
    from app.main import app
    app.dependency_overrides[SomeDep] = lambda: MockDep()
    try:
        response = client.get("/...")
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()
```

## Notes

- Each test must be runnable independently.
- Test webhook signature verification alongside the handler logic.
