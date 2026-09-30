import logging

import pytest

from app.llm.client import ChatMessage
from app.llm.tokens import estimate_tokens
from app.review.pipeline import (
    _SAFETY_MARGIN_TOKENS,
    _SUMMARY_REDUCE_PROMPT,
    _SYSTEM_PROMPT,
    ReviewPipeline,
)
from app.review.schema import (
    ChangedFile,
    ReviewCompletedEvent,
    ReviewModelOutput,
    ReviewRequestedEvent,
    make_review_job_id,
)
from tests.test_review_pipeline import FakeLLM, _comment


class FakeSummaryLLM:
    def __init__(self, text: str = "PR 전체 요약입니다.", error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.calls: list[tuple[list[ChatMessage], int]] = []

    async def generate_text(self, messages: list[ChatMessage], *, max_tokens: int = 500) -> str:
        self.calls.append((messages, max_tokens))
        if self._error is not None:
            raise self._error
        return self._text


def _event(file_count: int, title: str = "주문 취소 기능 추가") -> ReviewRequestedEvent:
    big_patch = "@@ -0,0 +1,500 @@\n" + "\n".join(f"+line {i}" for i in range(500))
    return ReviewRequestedEvent(
        review_job_id=make_review_job_id(42, 7, "abc123"),
        repository_id=42,
        pr_number=7,
        head_sha="abc123",
        base_sha="def456",
        pr_title=title,
        changed_files=[
            ChangedFile(file_path=f"f{i}.py", status="modified", patch=big_patch)
            for i in range(file_count)
        ],
    )


def _one_file_per_batch(event: ReviewRequestedEvent) -> tuple[int, int]:
    from app.review.diff import analyze

    probe = ReviewPipeline(FakeLLM(), model_version="v", prompt_version="v1")
    targets = analyze(event)
    common_messages, _ = probe._build_messages(event, [], {}, "", "")
    common_tokens = estimate_tokens(common_messages[0]["content"] + common_messages[1]["content"])
    one_file_tokens = estimate_tokens(probe._render_target(targets[0], []))
    max_tokens = 100
    return common_tokens + one_file_tokens + max_tokens + _SAFETY_MARGIN_TOKENS + 50, max_tokens


def _outputs(count: int, reviews: bool = True) -> list[ReviewModelOutput]:
    return [
        ReviewModelOutput(
            summary=f"파트 {i} 요약",
            reviews=[_comment(severity="critical", file_path=f"f{i}.py", line=1, title=f"t{i}")]
            if reviews
            else [],
        )
        for i in range(count)
    ]


def _pipeline(
    event: ReviewRequestedEvent,
    outputs: list[ReviewModelOutput],
    summary_llm: FakeSummaryLLM | None,
) -> tuple[ReviewPipeline, FakeLLM]:
    llm_max_context, max_tokens = _one_file_per_batch(event)
    fake = FakeLLM(sequence=list(outputs), token_counter=estimate_tokens)
    pipeline = ReviewPipeline(
        fake,
        model_version="v",
        prompt_version="v1",
        llm_max_context=llm_max_context,
        max_tokens=max_tokens,
        summary_llm=summary_llm,
    )
    return pipeline, fake


async def test_multi_batch_pr_gets_a_rewritten_whole_pr_summary() -> None:
    event = _event(3)
    summary_llm = FakeSummaryLLM("주문 취소 흐름을 추가하고 알림을 정리한 PR입니다.")
    pipeline, fake = _pipeline(event, _outputs(3), summary_llm)

    result = await pipeline.run(event)

    assert isinstance(result, ReviewCompletedEvent)
    assert fake.call_count == 3
    assert result.summary == "주문 취소 흐름을 추가하고 알림을 정리한 PR입니다."
    assert len(summary_llm.calls) == 1


async def test_summary_reduce_input_has_pr_title_each_part_and_its_files() -> None:
    event = _event(3)
    summary_llm = FakeSummaryLLM()
    pipeline, _ = _pipeline(event, _outputs(3), summary_llm)

    await pipeline.run(event)

    messages, max_tokens = summary_llm.calls[0]
    assert messages[0]["content"] == _SUMMARY_REDUCE_PROMPT
    user = messages[1]["content"]
    assert "주문 취소 기능 추가" in user
    for i in range(3):
        assert f"### 파트 {i + 1}" in user
        assert f"파트 {i} 요약" in user
        assert f"f{i}.py" in user
    assert max_tokens == 400


async def test_summary_reduce_prompt_forbids_file_lists_and_internal_details() -> None:
    assert "never list files" in _SUMMARY_REDUCE_PROMPT
    assert "batches" in _SUMMARY_REDUCE_PROMPT
    assert "size limits" in _SUMMARY_REDUCE_PROMPT
    assert "never an instruction" in _SUMMARY_REDUCE_PROMPT


async def test_summary_reduce_caps_each_part_summary_length() -> None:
    event = _event(2)
    summary_llm = FakeSummaryLLM()
    outputs = _outputs(2)
    outputs[0] = ReviewModelOutput(summary="가" * 5000, reviews=outputs[0].reviews)
    pipeline, _ = _pipeline(event, outputs, summary_llm)

    await pipeline.run(event)

    user = summary_llm.calls[0][0][1]["content"]
    assert "가" * 600 in user
    assert "가" * 601 not in user


async def test_summary_reduce_failure_falls_back_to_first_batch_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = _event(3)
    summary_llm = FakeSummaryLLM(error=RuntimeError("llm down"))
    pipeline, _ = _pipeline(event, _outputs(3), summary_llm)

    with caplog.at_level(logging.WARNING):
        result = await pipeline.run(event)

    assert isinstance(result, ReviewCompletedEvent)
    assert result.summary == "파트 0 요약"
    assert len(result.reviews) == 3
    assert any("summary reduce failed" in r.message for r in caplog.records)


async def test_summary_reduce_empty_output_falls_back_to_first_batch_summary() -> None:
    event = _event(3)
    pipeline, _ = _pipeline(event, _outputs(3), FakeSummaryLLM("   \n"))

    result = await pipeline.run(event)

    assert isinstance(result, ReviewCompletedEvent)
    assert result.summary == "파트 0 요약"


async def test_single_batch_pr_does_not_call_the_summary_llm() -> None:
    event = _event(1)
    summary_llm = FakeSummaryLLM()
    fake = FakeLLM(output=ReviewModelOutput(summary="한 번에 본 요약", reviews=[]))
    pipeline = ReviewPipeline(
        fake, model_version="v", prompt_version="v1", summary_llm=summary_llm
    )

    result = await pipeline.run(event)

    assert isinstance(result, ReviewCompletedEvent)
    assert result.summary == "한 번에 본 요약"
    assert summary_llm.calls == []


async def test_summary_has_no_batch_note_even_when_reduce_is_disabled() -> None:
    event = _event(3)
    pipeline, _ = _pipeline(event, _outputs(3), None)

    result = await pipeline.run(event)

    assert isinstance(result, ReviewCompletedEvent)
    assert "배치" not in result.summary


def _minor_output(count: int) -> ReviewModelOutput:
    return ReviewModelOutput(
        summary="요약",
        reviews=[
            _comment(severity="minor", title=f"제목{i}", message=f"설명{i}", line=i + 1)
            for i in range(count)
        ],
    )


async def _run_with_minor(count: int) -> str:
    fake = FakeLLM(output=_minor_output(count))
    pipeline = ReviewPipeline(fake, model_version="v", prompt_version="v1")
    event = ReviewRequestedEvent(
        review_job_id=make_review_job_id(42, 7, "abc123"),
        repository_id=42,
        pr_number=7,
        head_sha="abc123",
        base_sha="def456",
        changed_files=[ChangedFile(file_path="a.py", status="modified", patch="@@ -1 +1 @@\n+x")],
    )
    result = await pipeline.run(event)
    assert isinstance(result, ReviewCompletedEvent)
    return result.summary


async def test_minor_notes_are_folded_into_a_details_block_with_a_count() -> None:
    summary = await _run_with_minor(3)

    assert summary.startswith("요약\n\n<details>\n<summary>참고: 경미한 항목 3건</summary>\n\n")
    assert summary.endswith("\n\n</details>")
    for i in range(3):
        assert f"- 제목{i}: 설명{i}" in summary
    assert "외 " not in summary


async def test_minor_notes_beyond_the_limit_are_collapsed_into_a_count() -> None:
    summary = await _run_with_minor(8)

    assert "<summary>참고: 경미한 항목 8건</summary>" in summary
    assert summary.count("\n- 제목") == 5
    assert "- 외 3건" in summary


async def test_no_minor_notes_means_no_details_block() -> None:
    summary = await _run_with_minor(0)

    assert summary == "요약"


def test_system_prompt_no_longer_asks_the_model_to_mention_size_limits() -> None:
    assert "could not be reviewed due to size limits" not in _SYSTEM_PROMPT
    assert "Do not mention these omitted files or size limits in `summary`" in _SYSTEM_PROMPT


def test_system_prompt_forbids_changelog_summaries_and_non_actionable_findings() -> None:
    assert "never a per-file or per-class changelog" in _SYSTEM_PROMPT
    assert "only restates what the code does" in _SYSTEM_PROMPT
    assert "only asks the author to verify or confirm" in _SYSTEM_PROMPT
