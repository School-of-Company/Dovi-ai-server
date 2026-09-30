from app.review.diff_lines import (
    annotate_hunk,
    classify_finding_lines,
    visible_right_lines,
)
from app.review.schema import ChangedFile, ReviewComment

_PATCH = "\n".join(
    [
        "@@ -10,4 +12,5 @@ class OrderService {",
        "   async cancel(order: Order) {",
        "-    order.status = 'X';",
        "+    order.status = 'CANCELED';",
        "+    await this.stock.release(order);",
        "   }",
        "@@ -40,2 +50,3 @@ class A {",
        "   keep();",
        "+  added();",
        "   done();",
    ]
)


def test_annotate_hunk_numbers_added_and_context_lines_from_new_file_start() -> None:
    hunk = "\n".join(_PATCH.split("\n")[:6])

    assert annotate_hunk(hunk).split("\n") == [
        "@@ -10,4 +12,5 @@ class OrderService {",
        "R12    async cancel(order: Order) {",
        "-    order.status = 'X';",
        "R13 +    order.status = 'CANCELED';",
        "R14 +    await this.stock.release(order);",
        "R15    }",
    ]


def test_annotate_hunk_keeps_original_line_content_after_the_marker() -> None:
    annotated = annotate_hunk("@@ -1 +1 @@\n+const b = 1;")

    assert annotated == "@@ -1 +1 @@\nR1 +const b = 1;"


def test_annotate_hunk_does_not_count_no_newline_marker() -> None:
    hunk = "@@ -1,2 +1,2 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file"

    assert annotate_hunk(hunk).split("\n") == [
        "@@ -1,2 +1,2 @@",
        "-old",
        "\\ No newline at end of file",
        "R1 +new",
        "\\ No newline at end of file",
    ]


def test_annotate_hunk_handles_new_file_and_blank_context_lines() -> None:
    hunk = "@@ -0,0 +1,3 @@\n+a\n+\n+c"
    assert annotate_hunk(hunk).split("\n") == ["@@ -0,0 +1,3 @@", "R1 +a", "R2 +", "R3 +c"]

    blank_context = "@@ -5,3 +5,3 @@\n x\n\n y"
    assert annotate_hunk(blank_context).split("\n") == [
        "@@ -5,3 +5,3 @@", "R5  x", "R6 ", "R7  y",
    ]  # fmt: skip


def test_annotate_hunk_restarts_numbering_at_each_hunk_header() -> None:
    second = "\n".join(_PATCH.split("\n")[6:])

    assert annotate_hunk(second).split("\n")[1:] == [
        "R50    keep();",
        "R51 +  added();",
        "R52    done();",
    ]


def test_annotate_hunk_leaves_text_before_a_header_untouched() -> None:
    assert annotate_hunk("not a hunk\n+x") == "not a hunk\n+x"


def test_visible_right_lines_includes_added_and_context_but_not_deleted() -> None:
    assert visible_right_lines(_PATCH) == {12, 13, 14, 15, 50, 51, 52}


def test_visible_right_lines_ignores_no_newline_marker_and_empty_patch() -> None:
    patch = "@@ -1 +1,2 @@\n+a\n\\ No newline at end of file\n+b"

    assert visible_right_lines(patch) == {1, 2}
    assert visible_right_lines("") == frozenset()


def _review(path: str, line: int) -> ReviewComment:
    return ReviewComment.model_validate(
        {
            "severity": "major",
            "confidence": 0.9,
            "file_path": path,
            "line": line,
            "title": "t",
            "message": "m",
            "evidence": ["e"],
        }
    )


def test_classify_finding_lines_splits_ok_wrong_line_and_unknown_file() -> None:
    files = [ChangedFile(file_path="src/a.ts", status="modified", patch=_PATCH)]
    reviews = [
        _review("src/a.ts", 13),
        _review("src/a.ts", 14),
        _review("src/a.ts", 99),
        _review("src/a.ts", 11),
        _review("src/other.ts", 1),
    ]

    assert classify_finding_lines(reviews, files) == {
        "ok": 2,
        "line_not_in_diff": 2,
        "file_not_in_diff": 1,
    }


def test_classify_finding_lines_handles_empty_inputs() -> None:
    assert classify_finding_lines([], []) == {
        "ok": 0,
        "line_not_in_diff": 0,
        "file_not_in_diff": 0,
    }
