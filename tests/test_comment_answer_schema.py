from app.comment_answer.schema import (
    CommentAnswerCompletedEvent,
    CommentAnswerFailedEvent,
    CommentAnswerRequestedEvent,
    ThreadComment,
)


def test_requested_event_serializes_to_camel_case() -> None:
    event = CommentAnswerRequestedEvent(
        comment_job_id="qa:1:2:100",
        repository_id=1,
        pr_number=2,
        path="src/foo.ts",
        line=12,
        diff_hunk="@@ -1 +1 @@",
        thread=[
            ThreadComment(
                comment_id=99,
                author="dovi-code-assist[bot]",
                body="원본 지적",
                created_at="2026-09-01T00:00:00Z",
            )
        ],
    )

    data = event.model_dump(by_alias=True)

    assert data["commentJobId"] == "qa:1:2:100"
    assert data["repositoryId"] == 1
    assert data["diffHunk"] == "@@ -1 +1 @@"
    assert data["thread"][0]["commentId"] == 99
    assert data["thread"][0]["createdAt"] == "2026-09-01T00:00:00Z"


def test_requested_event_parses_camel_case_json() -> None:
    raw = (
        '{"commentJobId": "qa:1:2:100", "repositoryId": 1, "prNumber": 2, '
        '"path": "src/foo.ts", "line": null, "diffHunk": "@@ -1 +1 @@", '
        '"thread": []}'
    )

    event = CommentAnswerRequestedEvent.model_validate_json(raw)

    assert event.comment_job_id == "qa:1:2:100"
    assert event.line is None
    assert event.thread == []


def test_completed_event_serializes_to_camel_case() -> None:
    event = CommentAnswerCompletedEvent(comment_job_id="qa:1:2:100", answer="답변")
    assert event.model_dump(by_alias=True) == {
        "commentJobId": "qa:1:2:100",
        "answer": "답변",
    }


def test_failed_event_serializes_to_camel_case() -> None:
    event = CommentAnswerFailedEvent(comment_job_id="qa:1:2:100", reason="timeout")
    assert event.model_dump(by_alias=True) == {
        "commentJobId": "qa:1:2:100",
        "reason": "timeout",
    }
