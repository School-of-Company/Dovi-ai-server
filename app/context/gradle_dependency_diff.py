from __future__ import annotations

import re

from app.context.npm_lockfile_diff import DependencyChange

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@")
_GAV_COORDINATE = re.compile(
    r"""(['"])(?P<group>[\w.\-]+):(?P<artifact>[\w.\-]+):(?P<version>[\w.\-]+)(?::[\w.\-]+)?\1"""
)


def extract_dependency_changes(patch: str) -> list[DependencyChange]:
    """build.gradle/build.gradle.kts patch에서 새로 추가/변경된
    "groupId:artifactId:version" 좌표를 뽑는다.

    npm의 package-lock.json과 달리 이름과 버전이 한 줄짜리 문자열 리터럴 안에
    함께 있어(`implementation("group:artifact:version")`), npm_lockfile_diff처럼
    여러 줄에 걸친 이름 상태를 추적할 필요가 없다.

    지원하지 않는 선언 방식(version catalog의 `libs.versions.toml`, plugin의
    `id("...") version "..."` 형태)은 예외 없이 빈 리스트를 반환한다 — GAV
    좌표 패턴을 못 찾으면 그냥 아무것도 안 잡힐 뿐이다.
    """
    changes: list[DependencyChange] = []
    new_line = 0

    for raw_line in patch.splitlines():
        header_match = _HUNK_HEADER.match(raw_line)
        if header_match:
            new_line = int(header_match.group("new_start"))
            continue

        if not raw_line:
            continue

        prefix = raw_line[0]

        if prefix == "+":
            match = _GAV_COORDINATE.search(raw_line)
            if match:
                changes.append(
                    DependencyChange(
                        name=f"{match.group('group')}:{match.group('artifact')}",
                        version=match.group("version"),
                        evidence_line=raw_line,
                        new_file_line=new_line,
                    )
                )
            new_line += 1
        elif prefix == " ":
            new_line += 1
        # prefix == "-": 삭제된 라인은 new-file에 없으므로 new_line을 건드리지 않는다.

    return changes
