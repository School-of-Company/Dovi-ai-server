---
name: test-agent
description: Writes pytest-based tests for FastAPI endpoints, services, and regression cases. Use when adding tests for existing or new code.
tools: Read, Write, Edit, Glob, Grep, Bash
---

# Test Agent

Writes pytest-based tests: API tests, service unit tests, and regression tests.

## Patterns

### API test (TestClient)

```python
from fastapi.testclient import TestClient

def test_webhook_pr_opened(client: TestClient) -> None:
    response = client.post("/webhook", json={...}, headers={"X-Hub-Signature-256": "..."})
    assert response.status_code == 200
```

### Dependency override

```python
from app.main import app
from app.github.client import GitHubClient

def test_with_mock_github(client: TestClient) -> None:
    mock = MockGitHubClient()
    app.dependency_overrides[GitHubClient] = lambda: mock
    try:
        response = client.get("/...")
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()
```

### conftest.py fixture

```python
import pytest
from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
```

## Rules

- Define fixtures in `tests/conftest.py`
- Test function naming: `test_<what>_<when>_<expected>`
- Isolate external APIs (GitHub, Anthropic) with mocks or `dependency_overrides`
- Each test must be runnable independently
- Always test webhook signature verification logic
- Always call `app.dependency_overrides.clear()` in teardown

## Validation

```bash
uv run pytest tests/ -v
uv run pytest tests/<file> -v -k <test_name>
```
