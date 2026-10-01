import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.review.schema import ChangedFile, ReviewComment

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _counts_as_new_file_line(line: str) -> bool:
    # 삭제(-) 줄과 "\ No newline at end of file"은 새 파일의 줄이 아니다.
    return not line.startswith(("-", "\\"))


def annotate_hunk(hunk: str) -> str:
    """추가(+)·유지( ) 줄 앞에 새 파일 기준 줄 번호 `R<n> `을 붙인다.

    모델이 `@@` 헤더에서 줄 번호를 직접 계산하다 틀리는 것을 막기 위한 렌더링이다.
    """
    out: list[str] = []
    new_line: int | None = None
    for line in hunk.split("\n"):
        header = _HUNK_HEADER_RE.match(line)
        if header:
            new_line = int(header.group(1))
            out.append(line)
        elif new_line is None or not _counts_as_new_file_line(line):
            out.append(line)
        else:
            out.append(f"R{new_line} {line}")
            new_line += 1
    return "\n".join(out)


def visible_right_lines(patch: str) -> frozenset[int]:
    """GitHub가 인라인 코멘트를 받아주는 새 파일 기준 줄(추가 + 유지) 집합."""
    lines: set[int] = set()
    new_line: int | None = None
    for line in patch.splitlines():
        header = _HUNK_HEADER_RE.match(line)
        if header:
            new_line = int(header.group(1))
        elif new_line is not None and _counts_as_new_file_line(line):
            lines.add(new_line)
            new_line += 1
    return frozenset(lines)


def classify_finding_lines(
    reviews: Iterable[ReviewComment], changed_files: Iterable[ChangedFile]
) -> dict[str, int]:
    visible = {f.file_path: visible_right_lines(f.patch) for f in changed_files}
    counts = {"ok": 0, "line_not_in_diff": 0, "file_not_in_diff": 0}
    for review in reviews:
        lines = visible.get(review.file_path)
        if lines is None:
            counts["file_not_in_diff"] += 1
        elif review.line in lines:
            counts["ok"] += 1
        else:
            counts["line_not_in_diff"] += 1
    return counts


@dataclass(frozen=True)
class LineCheckRecord:
    """리뷰 한 건의 finding 줄 번호 측정 결과(이슈 #122). llm은 필터 전 모델 원본,
    final은 실제로 게시될 finding 기준이며 키는 classify_finding_lines()와 같다."""

    review_job_id: str
    annotated: bool
    llm: dict[str, int]
    final: dict[str, int]
