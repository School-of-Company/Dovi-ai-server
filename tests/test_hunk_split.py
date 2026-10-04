import re

from app.review.diff_lines import visible_right_lines
from app.review.hunk_split import split_hunk, split_oversized_target
from app.review.schema import ReviewTarget


def _hunk(old_start: int, new_start: int, lines: list[str]) -> str:
    old = sum(1 for line in lines if not line.startswith("+") and not line.startswith("\\"))
    new = sum(1 for line in lines if not line.startswith("-") and not line.startswith("\\"))
    return "\n".join([f"@@ -{old_start},{old} +{new_start},{new} @@ def f():", *lines])


def _body(hunk: str) -> list[str]:
    return hunk.split("\n")[1:]


def test_small_hunk_is_returned_as_is() -> None:
    hunk = _hunk(1, 1, ["+a", "+b"])

    assert split_hunk(hunk, 1000) == [hunk]


def test_split_hunk_keeps_every_line_exactly_once_in_order() -> None:
    lines = [f"+line {i}" for i in range(100)]
    hunk = _hunk(0, 1, lines)

    pieces = split_hunk(hunk, 300)

    assert len(pieces) > 1
    assert all(len(piece) <= 300 for piece in pieces)
    assert [line for piece in pieces for line in _body(piece)] == lines


def test_split_hunk_headers_keep_new_file_line_numbers_aligned() -> None:
    lines = [" ctx", "-old1", "+new1", "+new2", " ctx2"] * 30
    hunk = _hunk(10, 20, lines)

    pieces = split_hunk(hunk, 250)

    expected = visible_right_lines(hunk)
    assert len(pieces) > 1
    assert frozenset().union(*(visible_right_lines(piece) for piece in pieces)) == expected
    for piece in pieces:
        match = re.match(r"^@@ -(\d+),(\d+) \+(\d+),(\d+) @@ def f\(\):$", piece.split("\n")[0])
        assert match is not None
        body = _body(piece)
        assert int(match.group(2)) == sum(1 for line in body if not line.startswith("+"))
        assert int(match.group(4)) == sum(1 for line in body if not line.startswith("-"))


def test_split_hunk_never_separates_no_newline_marker_from_its_line() -> None:
    lines = [f"+line {i}" for i in range(20)] + ["\\ No newline at end of file"]
    hunk = _hunk(0, 1, lines)

    pieces = split_hunk(hunk, 120)

    assert all(not _body(piece)[0].startswith("\\") for piece in pieces)
    assert [line for piece in pieces for line in _body(piece)] == lines


def _target(hunks: list[str], chunks: list[str] | None = None) -> ReviewTarget:
    return ReviewTarget(
        file_path="src/big.py",
        status="modified",
        hunks=hunks,
        context_chunks=chunks or [],
    )


def _size(target: ReviewTarget) -> int:
    return len("\n".join(target.hunks)) + sum(len(c) for c in target.context_chunks) + 30


def test_target_within_limit_is_not_split() -> None:
    target = _target([_hunk(1, 1, ["+a"])])

    assert split_oversized_target(target, _size, 8000) == [target]


def test_oversized_target_is_split_at_hunk_boundaries_and_keeps_all_hunks() -> None:
    hunks = [_hunk(i * 100 + 1, i * 100 + 1, [f"+x{j}" for j in range(40)]) for i in range(6)]

    pieces = split_oversized_target(_target(hunks), _size, 600)

    assert len(pieces) > 1
    assert all(_size(piece) <= 600 for piece in pieces)
    assert [h for piece in pieces for h in piece.hunks] == hunks
    assert all(piece.file_path == "src/big.py" for piece in pieces)


def test_single_huge_hunk_is_split_by_lines() -> None:
    hunk = _hunk(0, 1, [f"+line {i}" for i in range(400)])

    pieces = split_oversized_target(_target([hunk]), _size, 1500)

    assert len(pieces) > 1
    assert all(_size(piece) <= 1500 for piece in pieces)
    covered = frozenset().union(*(visible_right_lines(h) for p in pieces for h in p.hunks))
    assert covered == visible_right_lines(hunk)


def test_context_chunks_follow_only_the_pieces_they_overlap() -> None:
    first = _hunk(1, 1, [f"+a{j}" for j in range(40)])
    second = _hunk(500, 500, [f"+b{j}" for j in range(40)])
    near_first = "### function_definition one (lines 1-30)\ndef one(): ..."
    near_second = "### function_definition two (lines 500-520)\ndef two(): ..."

    pieces = split_oversized_target(
        _target([first, second], [near_first, near_second]), _size, 400
    )

    assert len(pieces) == 2
    assert pieces[0].context_chunks == [near_first]
    assert pieces[1].context_chunks == [near_second]
