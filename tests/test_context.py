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
        ContextFile(path="analytics.keyboard.tsx", content="keyboard_content"),
        ContextFile(path="DOVI.md", content="dovi"),
    ]
    result = build_context(files)
    assert "SECRET" not in result
    assert "token" not in result
    assert "BEGIN" not in result
    assert "keyboard_content" in result
    assert "# DOVI.md" in result


def test_excludes_sdd_planning_docs() -> None:
    files = [
        ContextFile(
            path="docs/superpowers/plans/2026-09-04-notion-api-spec-sync-plan.md",
            content="has_openapi_spec / extract_notion_api_spec_link plan",
        ),
        ContextFile(
            path="docs/superpowers/specs/2026-09-04-notion-api-spec-sync-design.md",
            content="api_spec_link_store design",
        ),
        ContextFile(path="docs/guide.md", content="normal docs content"),
        ContextFile(path="DOVI.md", content="dovi"),
    ]
    result = build_context(files)
    assert "has_openapi_spec" not in result
    assert "api_spec_link_store" not in result
    assert "docs/superpowers" not in result
    assert "normal docs content" in result


def test_truncates_large_file() -> None:
    files = [ContextFile(path="DOVI.md", content="x" * 10000)]
    result = build_context(files, max_file_chars=100)
    assert "...(truncated)" in result
    assert result.count("x") == 100 - len("\n...(truncated)")


def test_total_char_limit() -> None:
    files = [
        ContextFile(path="DOVI.md", content="a" * 5000),
        ContextFile(path="README.md", content="b" * 5000),
    ]
    result = build_context(files, max_file_chars=5000, max_total_chars=6000)
    assert "# DOVI.md" in result
    assert "# README.md" in result
    assert "...(truncated)" in result


def test_has_openapi_spec_true_for_openapi_yaml() -> None:
    from app.review.context import has_openapi_spec

    files = [ContextFile(path="openapi.yaml", content="")]
    assert has_openapi_spec(files) is True


def test_has_openapi_spec_true_for_swagger_json() -> None:
    from app.review.context import has_openapi_spec

    files = [ContextFile(path="swagger.json", content="")]
    assert has_openapi_spec(files) is True


def test_has_openapi_spec_false_when_absent() -> None:
    from app.review.context import has_openapi_spec

    files = [ContextFile(path="README.md", content="")]
    assert has_openapi_spec(files) is False


def test_extract_notion_api_spec_link_from_dovi_md() -> None:
    from app.review.context import extract_notion_api_spec_link

    dovi = ContextFile(
        path="DOVI.md",
        content="## API Specification\n- Notion API Spec: https://www.notion.so/abcdef1234567890abcdef1234567890\n",
    )
    assert (
        extract_notion_api_spec_link([dovi])
        == "https://www.notion.so/abcdef1234567890abcdef1234567890"
    )


def test_extract_notion_api_spec_link_returns_none_without_dovi_md() -> None:
    from app.review.context import extract_notion_api_spec_link

    assert extract_notion_api_spec_link([ContextFile(path="README.md", content="x")]) is None


def test_extract_notion_api_spec_link_returns_none_without_link_line() -> None:
    from app.review.context import extract_notion_api_spec_link

    dovi = ContextFile(path="DOVI.md", content="## API Specification\n(아직 없음)\n")
    assert extract_notion_api_spec_link([dovi]) is None
