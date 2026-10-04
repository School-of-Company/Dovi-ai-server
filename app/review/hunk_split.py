import re
from collections.abc import Callable

from app.review.schema import ReviewTarget

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")
_CHUNK_RANGE_RE = re.compile(r"\(lines (\d+)-(\d+)\)")
_HEADER_RESERVE = 48
_MIN_HUNK_LIMIT = 1000


def split_hunk(hunk: str, max_chars: int) -> list[str]:
    """max_chars를 넘는 hunk를 줄 경계에서 나누고 조각마다 `@@` 헤더를 다시 만든다."""
    if len(hunk) <= max_chars:
        return [hunk]
    lines = hunk.split("\n")
    header = _HUNK_HEADER_RE.match(lines[0])
    if header is None:
        return [hunk]
    old_start = int(header.group(1))
    new_start = int(header.group(2))
    tail = header.group(3)

    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    old_count = new_count = 0

    def flush() -> None:
        nonlocal current, current_len, old_start, new_start, old_count, new_count
        pieces.append(
            "\n".join(
                [f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{tail}", *current]
            )
        )
        old_start += old_count
        new_start += new_count
        current = []
        current_len = 0
        old_count = new_count = 0

    limit = max(max_chars - _HEADER_RESERVE, 1)
    for line in lines[1:]:
        is_marker = line.startswith("\\")
        if current and not is_marker and current_len + len(line) + 1 > limit:
            flush()
        current.append(line)
        current_len += len(line) + 1
        if is_marker:
            continue
        if line.startswith("-"):
            old_count += 1
        elif line.startswith("+"):
            new_count += 1
        else:
            old_count += 1
            new_count += 1
    if current:
        flush()
    return pieces


def _hunk_new_range(hunks: list[str]) -> tuple[int, int] | None:
    start: int | None = None
    end = 0
    for hunk in hunks:
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", hunk)
        if match is None:
            return None
        first = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        start = first if start is None else min(start, first)
        end = max(end, first + max(count, 1) - 1)
    return None if start is None else (start, end)


def _chunks_for(chunks: list[str], hunks: list[str]) -> list[str]:
    hunk_range = _hunk_new_range(hunks)
    if hunk_range is None:
        return []
    kept: list[str] = []
    for chunk in chunks:
        match = _CHUNK_RANGE_RE.search(chunk.split("\n", 1)[0])
        if match is None:
            continue
        if int(match.group(1)) <= hunk_range[1] and int(match.group(2)) >= hunk_range[0]:
            kept.append(chunk)
    return kept


def split_oversized_target(
    target: ReviewTarget, size_of: Callable[[ReviewTarget], int], max_chars: int
) -> list[ReviewTarget]:
    """렌더링 크기가 max_chars를 넘는 파일을 hunk 단위(필요하면 줄 단위)로 쪼갠다.

    한 조각에는 그 hunk가 겹치는 함수/클래스 컨텍스트만 붙인다.
    """
    if size_of(target) <= max_chars:
        return [target]

    overhead = size_of(target.model_copy(update={"hunks": []}))
    hunk_limit = max(max_chars - overhead, _MIN_HUNK_LIMIT)
    hunks = [piece for hunk in target.hunks for piece in split_hunk(hunk, hunk_limit)]

    def make(group: list[str]) -> ReviewTarget:
        return target.model_copy(
            update={"hunks": group, "context_chunks": _chunks_for(target.context_chunks, group)}
        )

    pieces: list[ReviewTarget] = []
    group: list[str] = []
    for hunk in hunks:
        if group and size_of(make([*group, hunk])) > max_chars:
            pieces.append(make(group))
            group = []
        group.append(hunk)
    if group:
        pieces.append(make(group))
    return pieces
