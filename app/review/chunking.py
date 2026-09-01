import re
from dataclasses import dataclass

import tree_sitter_javascript as tsjavascript
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

_EXTENSION_LANGUAGE = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}

_BOUNDARY_NODE_TYPES: dict[str, set[str]] = {
    "python": {"function_definition", "class_definition"},
    "javascript": {"function_declaration", "class_declaration", "method_definition"},
    "typescript": {"function_declaration", "class_declaration", "method_definition"},
}

# 부모가 이 타입이면 export/decorator까지 chunk에 포함되도록 경계를 위로 넓힌다.
_WRAPPER_NODE_TYPES = {"export_statement", "decorated_definition"}

_NAME_NODE_TYPES = {"identifier", "type_identifier", "property_identifier"}

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_parsers: dict[str, Parser] = {}


def _get_parser(language: str) -> Parser:
    if language not in _parsers:
        if language == "python":
            lang = Language(tspython.language())
        elif language == "javascript":
            lang = Language(tsjavascript.language())
        elif language == "typescript":
            lang = Language(tstypescript.language_typescript())
        else:
            raise ValueError(f"unsupported language: {language}")
        _parsers[language] = Parser(lang)
    return _parsers[language]


def detect_language(file_path: str) -> str | None:
    dot = file_path.rfind(".")
    if dot == -1:
        return None
    return _EXTENSION_LANGUAGE.get(file_path[dot:].lower())


def changed_line_numbers(patch: str) -> set[int]:
    """unified diff patch에서, 새 파일 기준으로 추가/유지된(컨텍스트) 라인 번호를 뽑는다.

    -로 시작하는 삭제 라인은 새 파일에 존재하지 않으므로 라인 번호를 진행시키지 않는다.
    """
    lines: set[int] = set()
    new_line = 0
    for raw_line in patch.splitlines():
        header = _HUNK_HEADER_RE.match(raw_line)
        if header:
            new_line = int(header.group(1))
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            lines.add(new_line)
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            continue
        elif not raw_line.startswith("@@"):
            new_line += 1
    return lines


@dataclass
class AstChunk:
    node_type: str
    name: str | None
    start_line: int
    end_line: int
    source: str


def _extract_name(node: Node) -> str | None:
    for child in node.children:
        if child.type in _NAME_NODE_TYPES and child.text is not None:
            return child.text.decode("utf-8")
    return None


def _find_enclosing_boundary(root: Node, line: int, boundary_types: set[str]) -> Node | None:
    """0-indexed line을 포함하는 boundary 타입 노드 중 가장 안쪽(가장 좁은) 것을 찾는다."""
    best: Node | None = None
    stack = [root]
    while stack:
        node = stack.pop()
        if node.start_point[0] <= line <= node.end_point[0]:
            if node.type in boundary_types:
                best = node
            stack.extend(node.children)
    return best


def extract_context_chunks(
    file_path: str, content: str, patch: str
) -> list[AstChunk] | None:
    """변경된 라인을 포함하는 함수/클래스 전체를 chunk로 추출한다.

    지원하지 않는 언어이거나 파싱에 실패하면 None을 반환해, 호출자가 기존
    diff hunk 방식으로 안전하게 fallback할 수 있게 한다.
    """
    language = detect_language(file_path)
    if language is None:
        return None

    changed_lines = changed_line_numbers(patch)
    if not changed_lines:
        return None

    try:
        parser = _get_parser(language)
        encoded = content.encode("utf-8")
        tree = parser.parse(encoded)
    except Exception:
        return None

    boundary_types = _BOUNDARY_NODE_TYPES[language]
    seen_ranges: set[tuple[int, int]] = set()
    chunks: list[AstChunk] = []

    for line in sorted(changed_lines):
        node = _find_enclosing_boundary(tree.root_node, line - 1, boundary_types)
        if node is None:
            continue

        target = node
        while target.parent is not None and target.parent.type in _WRAPPER_NODE_TYPES:
            target = target.parent

        key = (target.start_byte, target.end_byte)
        if key in seen_ranges:
            continue
        seen_ranges.add(key)

        chunks.append(
            AstChunk(
                node_type=node.type,
                name=_extract_name(node),
                start_line=target.start_point[0] + 1,
                end_line=target.end_point[0] + 1,
                source=encoded[target.start_byte : target.end_byte].decode("utf-8"),
            )
        )

    return chunks or None
