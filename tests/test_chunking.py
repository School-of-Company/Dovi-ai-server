from app.review.chunking import changed_line_numbers, detect_language, extract_context_chunks

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
