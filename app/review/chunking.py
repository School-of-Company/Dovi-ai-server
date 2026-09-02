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


def _find_enclosing_boundary(
    root: Node, encoded_lines: list[bytes], line: int, boundary_types: set[str]
) -> Node | None:
    """0-indexed line을 포함하는 boundary 타입 조상 노드를 찾는다.

    node.descendant_for_point_range로 해당 라인의 leaf 노드를 바로 찾은 뒤
    parent 체인만 타고 올라간다 — 트리 전체를 순회하지 않아 파일이 커져도 빠르다.
    라인의 마지막 문자를 기준점으로 쓴다: 첫 컬럼(0)은 들여쓰기 공백이라 leaf가
    없는 경우가 많아, 실제 토큰이 있는 라인 끝 쪽을 찾아야 정확히 걸린다.
    """
    if line >= len(encoded_lines):
        return None
    col = max(len(encoded_lines[line]) - 1, 0)
    node: Node | None = root.descendant_for_point_range((line, col), (line, col))
    while node is not None:
        if node.type in boundary_types:
            return node
        node = node.parent
    return None


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

    encoded = content.encode("utf-8")
    try:
        parser = _get_parser(language)
        tree = parser.parse(encoded)
    except Exception:
        # tree-sitter 바인딩이 무엇을 던질지 문서화가 부실해 구체적 예외로 좁히기
        # 어렵다. 이 함수 전체가 "실패하면 기존 diff hunk 방식으로 안전하게
        # fallback"하는 best-effort 보강 기능이라, 여기서 넓게 잡아 삼키는 게 맞다.
        return None

    # detect_language()가 반환하는 언어는 현재 항상 _BOUNDARY_NODE_TYPES에 정의돼
    # 있지만, 새 확장자/언어를 한쪽에만 추가하는 실수를 막기 위해 방어적으로 처리한다.
    boundary_types = _BOUNDARY_NODE_TYPES.get(language)
    if boundary_types is None:
        return None
    encoded_lines = encoded.split(b"\n")
    seen_ranges: set[tuple[int, int]] = set()
    chunks: list[AstChunk] = []

    for line in sorted(changed_lines):
        node = _find_enclosing_boundary(tree.root_node, encoded_lines, line - 1, boundary_types)
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
