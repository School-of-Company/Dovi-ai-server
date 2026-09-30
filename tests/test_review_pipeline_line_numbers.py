import logging

import pytest

from app.review.pipeline import _SYSTEM_PROMPT, ReviewPipeline
from app.review.schema import (
    ChangedFile,
    ReviewCompletedEvent,
    ReviewModelOutput,
    ReviewRequestedEvent,
    make_review_job_id,
)
from tests.test_review_pipeline import FakeLLM, FakeRetriever, _comment


def _event(patch: str = "@@ -1 +1 @@\n+b", path: str = "app/main.py") -> ReviewRequestedEvent:
    return ReviewRequestedEvent(
        review_job_id=make_review_job_id(42, 7, "abc123"),
        repository_id=42,
        pr_number=7,
        head_sha="abc123",
        base_sha="def456",
        changed_files=[ChangedFile(file_path=path, status="modified", patch=patch)],
    )


def _pipeline(
    fake: FakeLLM, *, annotate: bool, retriever: object = None
) -> ReviewPipeline:
    return ReviewPipeline(
        fake,
        model_version="qwen",
        prompt_version="v1",
        annotate_diff_lines=annotate,
        retriever=retriever,  # type: ignore[arg-type]
    )


def _empty_output() -> ReviewModelOutput:
    return ReviewModelOutput(summary="ok", reviews=[])


async def test_prompt_is_unchanged_when_line_numbers_are_disabled() -> None:
    fake = FakeLLM(output=_empty_output())

    await _pipeline(fake, annotate=False).run(_event())

    assert fake.received is not None
    system, user = fake.received[0]["content"], fake.received[1]["content"]
    assert system == _SYSTEM_PROMPT
    assert "+b" in user
    assert "R1 " not in user


async def test_enabled_prompt_marks_added_lines_and_explains_the_marker() -> None:
    fake = FakeLLM(output=_empty_output())

    await _pipeline(fake, annotate=True).run(_event("@@ -1 +1,2 @@\n a\n+b"))

    assert fake.received is not None
    system, user = fake.received[0]["content"], fake.received[1]["content"]
    assert system.startswith(_SYSTEM_PROMPT)
    assert "`R<n>`" in system
    assert "leave it out of `evidence`" in system
    assert "@@ -1 +1,2 @@" in user
    assert "R1  a" in user
    assert "R2 +b" in user


async def test_retrieval_queries_keep_the_original_hunks() -> None:
    retriever = FakeRetriever([])
    fake = FakeLLM(output=_empty_output())

    await _pipeline(fake, annotate=True, retriever=retriever).run(_event())

    assert retriever.received_queries[0][0] == "@@ -1 +1 @@\n+b"


async def test_enabled_verify_messages_reuse_the_annotated_diff() -> None:
    fake = FakeLLM(
        output=ReviewModelOutput(
            summary="ok", reviews=[_comment(file_path="app/main.py", line=1)]
        )
    )

    await _pipeline(fake, annotate=True).run(_event())

    assert fake.verify_received is not None
    assert "R1 +b" in fake.verify_received[1]["content"]


def _lines_logs(caplog: pytest.LogCaptureFixture) -> dict[str, str]:
    return {
        record.message.split("stage=")[1].split()[0]: record.message
        for record in caplog.records
        if "finding lines checked" in record.message
    }


async def test_measurement_logs_llm_and_final_counts_without_changing_findings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    patch = "@@ -1,2 +1,3 @@\n a\n+b\n c"
    reviews = [
        _comment(file_path="app/main.py", line=2, title="ok"),
        _comment(file_path="app/main.py", line=9, title="wrong line"),
        _comment(file_path="other.py", line=1, title="wrong file"),
    ]
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=reviews))

    with caplog.at_level(logging.INFO):
        result = await _pipeline(fake, annotate=False).run(_event(patch))

    assert isinstance(result, ReviewCompletedEvent)
    assert {r.title for r in result.reviews} == {"ok", "wrong line", "wrong file"}
    logs = _lines_logs(caplog)
    for stage in ("llm", "final"):
        assert "total=3 ok=1 line_not_in_diff=1 file_not_in_diff=1" in logs[stage]
        assert "annotated=False" in logs[stage]
        assert "reviewJobId=42:7:abc123" in logs[stage]


async def test_measurement_log_reports_when_line_numbers_are_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeLLM(output=_empty_output())

    with caplog.at_level(logging.INFO):
        await _pipeline(fake, annotate=True).run(_event())

    assert "annotated=True" in _lines_logs(caplog)["llm"]
    assert "total=0 ok=0" in _lines_logs(caplog)["llm"]


async def test_final_stage_counts_only_findings_that_survived_filtering(
    caplog: pytest.LogCaptureFixture,
) -> None:
    patch = "@@ -1 +1 @@\n+b"
    reviews = [
        _comment(file_path="app/main.py", line=1, severity="major"),
        _comment(file_path="app/main.py", line=77, severity="major", confidence=0.1),
    ]
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=reviews))

    with caplog.at_level(logging.INFO):
        await _pipeline(fake, annotate=False).run(_event(patch))

    logs = _lines_logs(caplog)
    assert "total=2 ok=1 line_not_in_diff=1" in logs["llm"]
    assert "total=1 ok=1 line_not_in_diff=0" in logs["final"]
