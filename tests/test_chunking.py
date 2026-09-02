from app.review.chunking import (
    AstChunk,
    changed_line_numbers,
    detect_language,
    extract_all_chunks,
    extract_context_chunks,
    merge_small_chunks,
)

_PY_CONTENT = """import os


def unrelated():
    return os.getcwd()


class Foo:
    def bar(self, x: int) -> int:
        return x + 1
"""

_PY_PATCH_BAR = (
    "@@ -8,3 +8,3 @@\n class Foo:\n     def bar(self, x: int) -> int:\n"
    "-        return x\n+        return x + 1"
)

_TS_CONTENT = """import { Injectable } from "@nestjs/common";

@Injectable()
export class FooService {
  bar(x: number): number {
    return x + 1;
  }
}
"""

_TS_PATCH_BAR = (
    "@@ -4,3 +4,3 @@\n export class FooService {\n   bar(x: number): number {\n"
    "-    return x;\n+    return x + 1;"
)


def test_detect_language_by_extension() -> None:
    assert detect_language("app/main.py") == "python"
    assert detect_language("src/foo.ts") == "typescript"
    assert detect_language("src/foo.tsx") == "typescript"
    assert detect_language("src/foo.js") == "javascript"
    assert detect_language("README.md") is None
    assert detect_language("no_extension") is None


def test_changed_line_numbers_counts_added_lines_only() -> None:
    patch = "@@ -1,2 +1,3 @@\n line1\n+added\n line2"
    assert changed_line_numbers(patch) == {2}


def test_changed_line_numbers_ignores_deleted_lines() -> None:
    patch = "@@ -1,3 +1,2 @@\n line1\n-removed\n line2"
    # new file 기준 2번째 줄(line2)만 유지되고, removed는 새 파일에 없다
    assert changed_line_numbers(patch) == set()


def test_extract_context_chunks_python_returns_enclosing_function() -> None:
    chunks = extract_context_chunks("app/foo.py", _PY_CONTENT, _PY_PATCH_BAR)

    assert chunks is not None
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.node_type == "function_definition"
    assert chunk.name == "bar"
    assert "def bar(self, x: int) -> int:" in chunk.source
    assert "unrelated" not in chunk.source


def test_extract_context_chunks_typescript_includes_export_wrapper() -> None:
    chunks = extract_context_chunks("src/foo.service.ts", _TS_CONTENT, _TS_PATCH_BAR)

    assert chunks is not None
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.node_type == "method_definition"
    assert chunk.name == "bar"
    assert "return x + 1;" in chunk.source


def test_extract_context_chunks_returns_none_for_unsupported_language() -> None:
    assert extract_context_chunks("app.rb", "def foo; end", "@@ -1 +1 @@\n+x") is None


def test_extract_context_chunks_returns_none_without_changed_lines() -> None:
    assert extract_context_chunks("app/foo.py", _PY_CONTENT, "") is None


def test_extract_context_chunks_returns_none_when_no_enclosing_boundary() -> None:
    # 모듈 최상단 import 라인은 함수/클래스에 속하지 않는다
    patch = "@@ -1,1 +1,1 @@\n-import os\n+import os, sys"
    assert extract_context_chunks("app/foo.py", _PY_CONTENT, patch) is None


def test_extract_all_chunks_returns_every_function_and_class() -> None:
    chunks = extract_all_chunks("app/foo.py", _PY_CONTENT)

    assert chunks is not None
    names = {c.name for c in chunks}
    assert names == {"unrelated", "Foo", "bar"}


def test_extract_all_chunks_orders_by_start_line() -> None:
    chunks = extract_all_chunks("app/foo.py", _PY_CONTENT)

    assert chunks is not None
    starts = [c.start_line for c in chunks]
    assert starts == sorted(starts)


def test_extract_all_chunks_returns_none_for_unsupported_language() -> None:
    assert extract_all_chunks("app.rb", "def foo; end") is None


def test_extract_all_chunks_returns_none_when_no_boundary_nodes() -> None:
    assert extract_all_chunks("app/foo.py", "import os\nX = 1\n") is None


def test_merge_small_chunks_combines_adjacent_small_chunks() -> None:
    small_a = AstChunk(
        node_type="function_definition", name="a", start_line=1, end_line=2, source="def a(): pass"
    )
    small_b = AstChunk(
        node_type="function_definition", name="b", start_line=3, end_line=4, source="def b(): pass"
    )

    merged = merge_small_chunks([small_a, small_b], min_chars=20)

    assert len(merged) == 1
    assert merged[0].node_type == "merged"
    assert "def a(): pass" in merged[0].source
    assert "def b(): pass" in merged[0].source


def test_merge_small_chunks_keeps_large_chunk_standalone() -> None:
    large = AstChunk(
        node_type="function_definition", name="big", start_line=1, end_line=10, source="x" * 500
    )

    merged = merge_small_chunks([large], min_chars=200)

    assert merged == [large]


def test_merge_small_chunks_flushes_trailing_small_buffer() -> None:
    small = AstChunk(
        node_type="function_definition", name="a", start_line=1, end_line=2, source="def a(): pass"
    )

    merged = merge_small_chunks([small], min_chars=200)

    assert merged == [small]


def test_merge_small_chunks_empty_list() -> None:
    assert merge_small_chunks([]) == []


def test_merge_small_chunks_does_not_duplicate_overlapping_chunks() -> None:
    # class와 그 안의 nested method는 extract_all_chunks가 겹치는 범위로 함께 낸다.
    # 겹치는 chunk끼리 합치면 method 소스가 class 소스 안에 중복 포함되므로,
    # 이 경우 합쳐지지 않고 각자 원본 그대로 남아야 한다.
    content = "class Foo:\n    def bar(self):\n        return 1\n"
    chunks = extract_all_chunks("app/foo.py", content)
    assert chunks is not None

    merged = merge_small_chunks(chunks, min_chars=200)

    assert len(merged) == len(chunks)
    for original, result in zip(sorted(chunks, key=lambda c: c.start_line), merged):
        assert result.source == original.source


def test_merge_small_chunks_merges_non_overlapping_siblings_but_not_ancestor() -> None:
    content = (
        "class Foo:\n"
        "    def a(self):\n"
        "        return 1\n"
        "\n"
        "    def b(self):\n"
        "        return 2\n"
    )
    chunks = extract_all_chunks("app/foo.py", content)
    assert chunks is not None
    assert {c.name for c in chunks} == {"Foo", "a", "b"}

    merged = merge_small_chunks(chunks, min_chars=1000)

    # class(조상)는 nested method와 겹치므로 단독으로 flush되고, 겹치지 않는 형제
    # method a/b끼리는 여전히 하나로 합쳐진다.
    assert any(c.node_type == "merged" for c in merged)
