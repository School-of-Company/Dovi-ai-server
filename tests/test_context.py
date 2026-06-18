from app.review.context import build_context
from app.review.schema import ContextFile


def test_empty_returns_empty() -> None:
    assert build_context([]) == ""


def test_dovi_md_comes_first() -> None:
    files = [
        ContextFile(path="README.md", content="readme"),
        ContextFile(path="DOVI.md", content="dovi"),
    ]
    result = build_context(files)
    assert result.index("# DOVI.md") < result.index("# README.md")


def test_priority_order() -> None:
    files = [
        ContextFile(path="CLAUDE.md", content="c"),
        ContextFile(path="docs/guide.md", content="d"),
        ContextFile(path="README.md", content="r"),
        ContextFile(path="openapi.yaml", content="o"),
        ContextFile(path="DOVI.md", content="dovi"),
    ]
    result = build_context(files)
    expected = ["DOVI.md", "openapi.yaml", "README.md", "docs/guide.md", "CLAUDE.md"]
    order = [result.index(f"# {p}") for p in expected]
    assert order == sorted(order)


def test_excludes_secret_paths() -> None:
    files = [
        ContextFile(path=".env", content="SECRET=1"),
        ContextFile(path="secrets/token.txt", content="token"),
        ContextFile(path="key.pem", content="-----BEGIN"),
        ContextFile(path="config/private_key.json", content="pk"),
        ContextFile(path="DOVI.md", content="dovi"),
    ]
    result = build_context(files)
    assert "SECRET" not in result
    assert "token" not in result
    assert "BEGIN" not in result
    assert "# DOVI.md" in result


def test_truncates_large_file() -> None:
    files = [ContextFile(path="DOVI.md", content="x" * 10000)]
    result = build_context(files, max_file_chars=100)
    assert "...(truncated)" in result
    assert result.count("x") == 100


def test_total_char_limit() -> None:
    files = [
        ContextFile(path="DOVI.md", content="a" * 5000),
        ContextFile(path="README.md", content="b" * 5000),
    ]
    result = build_context(files, max_file_chars=5000, max_total_chars=6000)
    assert "# DOVI.md" in result
    assert "# README.md" not in result
