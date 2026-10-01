import logging

import pytest

from app.review.diff_lines import LineCheckRecord
from app.review.pipeline import ReviewPipeline
from app.review.schema import (
    ChangedFile,
    ReviewCompletedEvent,
    ReviewModelOutput,
    ReviewRequestedEvent,
    make_review_job_id,
)
from tests.test_review_pipeline import FakeLLM, _comment


def _event(patch: str = "@@ -1 +1 @@\n+b") -> ReviewRequestedEvent:
    return ReviewRequestedEvent(
        review_job_id=make_review_job_id(42, 7, "abc123"),
        repository_id=42,
        pr_number=7,
        head_sha="abc123",
        base_sha="def456",
        changed_files=[ChangedFile(file_path="app/main.py", status="modified", patch=patch)],
    )


class RecordingSink:
    def __init__(self, error: Exception | None = None) -> None:
        self.records: list[LineCheckRecord] = []
        self._error = error

    async def __call__(self, record: LineCheckRecord) -> None:
        if self._error is not None:
            raise self._error
        self.records.append(record)


def _pipeline(
    fake: FakeLLM, sink: RecordingSink | None, *, annotate: bool = False
) -> ReviewPipeline:
    return ReviewPipeline(
        fake,
        model_version="qwen",
        prompt_version="v1",
        annotate_diff_lines=annotate,
        line_check_sink=sink,
    )


async def test_sink_receives_llm_and_final_counts_once_per_review() -> None:
    patch = "@@ -1,2 +1,3 @@\n a\n+b\n c"
    reviews = [
        _comment(file_path="app/main.py", line=2, title="ok"),
        _comment(file_path="app/main.py", line=9, title="wrong line"),
        _comment(file_path="app/main.py", line=77, title="low", confidence=0.1),
    ]
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=reviews))
    sink = RecordingSink()

    await _pipeline(fake, sink, annotate=True).run(_event(patch))

    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.review_job_id == "42:7:abc123"
    assert record.annotated is True
    assert record.llm == {"ok": 1, "line_not_in_diff": 2, "file_not_in_diff": 0}
    assert record.final == {"ok": 1, "line_not_in_diff": 1, "file_not_in_diff": 0}


async def test_sink_failure_does_not_fail_the_review(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeLLM(
        output=ReviewModelOutput(summary="ok", reviews=[_comment(file_path="app/main.py", line=1)])
    )
    pipeline = _pipeline(fake, RecordingSink(error=RuntimeError("db down")))

    with caplog.at_level(logging.WARNING):
        result = await pipeline.run(_event())

    assert isinstance(result, ReviewCompletedEvent)
    assert len(result.reviews) == 1
    assert any("failed to persist line check" in r.message for r in caplog.records)


async def test_nothing_is_recorded_when_the_review_fails() -> None:
    fake = FakeLLM(error=ValueError("bad json"))
    sink = RecordingSink()

    result = await _pipeline(fake, sink).run(_event())

    assert not isinstance(result, ReviewCompletedEvent)
    assert sink.records == []


async def test_pipeline_without_a_sink_still_completes() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))

    result = await _pipeline(fake, None).run(_event())

    assert isinstance(result, ReviewCompletedEvent)
