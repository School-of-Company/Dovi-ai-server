from __future__ import annotations

import re
from dataclasses import dataclass

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@")
_NODE_MODULES_KEY = re.compile(r'"node_modules/(?P<path>[^"]+)":\s*\{')
_VERSION_LINE = re.compile(r'^\+\s*"version":\s*"(?P<version>[^"]+)",?\s*$')


@dataclass
class DependencyChange:
    name: str
    version: str
    evidence_line: str
    new_file_line: int


def extract_dependency_changes(patch: str) -> list[DependencyChange]:
    """package-lock.json(lockfileVersion 2/3) patch에서 새로 추가/변경된
    (패키지명, 버전) 쌍을 뽑는다.

    지원하지 않는 포맷(yarn.lock, lockfileVersion 1 등)이나 빈 입력은 예외 없이
    빈 리스트를 반환한다 — "node_modules/<name>": { 키 구조를 못 찾으면 그냥
    아무것도 안 잡힐 뿐이다.
    """
    changes: list[DependencyChange] = []
    new_line = 0
    current_name: str | None = None

    for raw_line in patch.splitlines():
        header_match = _HUNK_HEADER.match(raw_line)
        if header_match:
            new_line = int(header_match.group("new_start"))
            current_name = None
            continue

        if not raw_line:
            continue

        prefix = raw_line[0]
        content = raw_line[1:] if prefix in ("+", "-", " ") else raw_line

        key_match = _NODE_MODULES_KEY.search(content)
        if key_match:
            # 중첩 transitive dependency("node_modules/parent/node_modules/child")는
            # 마지막 node_modules/ 다음부터가 실제 패키지명이다.
            current_name = key_match.group("path").rsplit("node_modules/", 1)[-1]

        if prefix == "+":
            version_match = _VERSION_LINE.match(raw_line)
            if version_match and current_name is not None:
                changes.append(
                    DependencyChange(
                        name=current_name,
                        version=version_match.group("version"),
                        evidence_line=raw_line,
                        new_file_line=new_line,
                    )
                )
            new_line += 1
        elif prefix == " ":
            new_line += 1
        # prefix == "-": 삭제된 라인은 new-file에 없으므로 new_line을 건드리지 않는다.

    return changes
