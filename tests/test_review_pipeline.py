import pytest
from pydantic import ValidationError

from app.llm.client import ChatMessage
from app.review.pipeline import ReviewPipeline
from app.review.schema import (
    ChangedFile,
    ReviewComment,
    ReviewCompletedEvent,
    ReviewFailedEvent,
    ReviewModelOutput,
    ReviewRequestedEvent,
    Severity,
    make_review_job_id,
)


def _comment(
    *,
    severity: Severity = "major",
    confidence: float = 0.9,
    file_path: str = "a.py",
    line: int = 1,
    title: str = "t",
    message: str = "m",
    evidence: list[str] | None = None,
) -> ReviewComment:
    return ReviewComment(
        severity=severity,
        confidence=confidence,
        file_path=file_path,
        line=line,
        title=title,
        message=message,
        evidence=evidence if evidence is not None else ["e"],
    )


class FakeLLM:
    def __init__(
        self,
        output: ReviewModelOutput | None = None,
        error: Exception | None = None,
        sequence: list[ReviewModelOutput | Exception] | None = None,
    ) -> None:
        self._output = output
        self._error = error
        self._sequence = sequence
        self.received: list[ChatMessage] | None = None
        self.call_count = 0

    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int = 1500
    ) -> ReviewModelOutput:
        self.received = messages
        self.call_count += 1

        if self._sequence is not None:
            result = self._sequence[self.call_count - 1]
            if isinstance(result, Exception):
                raise result
            return result

        if self._error is not None:
            raise self._error
        assert self._output is not None
        return self._output


def _event() -> ReviewRequestedEvent:
    return ReviewRequestedEvent(
        review_job_id=make_review_job_id(42, 7, "abc123"),
        repository_id=42,
        pr_number=7,
        head_sha="abc123",
        base_sha="def456",
        changed_files=[
            ChangedFile(file_path="app/main.py", status="modified", patch="@@ -1 +1 @@")
        ],
    )


def _pipeline(fake: FakeLLM) -> ReviewPipeline:
    return ReviewPipeline(fake, model_version="qwen2.5-coder-14b", prompt_version="v1")


def test_make_review_job_id() -> None:
    assert make_review_job_id(42, 7, "abc123") == "42:7:abc123"


def test_event_serializes_to_camel_case() -> None:
    data = _event().model_dump(by_alias=True)
    assert data["reviewJobId"] == "42:7:abc123"
    assert data["changedFiles"][0]["filePath"] == "app/main.py"


async def test_run_returns_completed_on_success() -> None:
    output = ReviewModelOutput(summary="LGTM", reviews=[])
    fake = FakeLLM(output=output)

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewCompletedEvent)
    assert result.summary == "LGTM"
    assert result.review_job_id == "42:7:abc123"
    assert result.model_version == "qwen2.5-coder-14b"
    assert fake.received is not None


async def test_run_returns_failed_on_timeout() -> None:
    fake = FakeLLM(error=TimeoutError())

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewFailedEvent)
    assert result.reason == "timeout"
    assert result.head_sha == "abc123"
    assert fake.call_count == 1  # timeout은 재시도하지 않는다


async def test_run_returns_failed_on_parse_error() -> None:
    fake = FakeLLM(error=ValueError("bad json"))

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewFailedEvent)
    assert result.reason == "parse_error"
    assert fake.call_count == 2  # 1회 재시도 후 실패


async def test_run_retries_parse_error_then_succeeds() -> None:
    fake = FakeLLM(
        sequence=[ValueError("bad json"), ReviewModelOutput(summary="ok", reviews=[])]
    )

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewCompletedEvent)
    assert result.summary == "ok"
    assert fake.call_count == 2


async def test_run_retries_server_error_then_succeeds() -> None:
    fake = FakeLLM(
        sequence=[RuntimeError("boom"), ReviewModelOutput(summary="ok", reviews=[])]
    )

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewCompletedEvent)
    assert fake.call_count == 2


async def test_run_includes_ast_context_chunk_when_content_available() -> None:
    event = ReviewRequestedEvent(
        review_job_id=make_review_job_id(42, 7, "abc123"),
        repository_id=42,
        pr_number=7,
        head_sha="abc123",
        base_sha="def456",
        changed_files=[
            ChangedFile(
                file_path="app/main.py",
                status="modified",
                patch="@@ -1,2 +1,3 @@\n line1\n+added\n line2",
                content="def foo():\n    line1 = 1\n    added = 1\n    line2 = 1\n",
            )
        ],
    )
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))

    await _pipeline(fake).run(event)

    assert fake.received is not None
    user_message = fake.received[1]["content"]
    assert "전체 함수/클래스 컨텍스트" in user_message
    assert "def foo():" in user_message


async def test_run_moves_minor_reviews_to_summary_only() -> None:
    reviews = [
        _comment(severity="critical", line=1, title="critical finding"),
        _comment(severity="minor", line=2, title="minor finding"),
    ]
    fake = FakeLLM(output=ReviewModelOutput(summary="요약", reviews=reviews))

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewCompletedEvent)
    assert [r.severity for r in result.reviews] == ["critical"]
    assert "minor finding" in result.summary
    assert "요약" in result.summary


async def test_run_skips_when_no_changed_files() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="unused"))
    event = _event()
    event.changed_files = []

    result = await _pipeline(fake).run(event)

    assert isinstance(result, ReviewCompletedEvent)
    assert result.reviews == []
    assert fake.received is None  # LLM 호출 안 됨


async def test_run_returns_failed_on_validation_error() -> None:
    try:
        ReviewComment(
            severity="critical",
            confidence=2.0,
            file_path="x",
            line=1,
            title="t",
            message="m",
            evidence=["x"],
        )
    except ValidationError as exc:
        validation_error = exc

    fake = FakeLLM(error=validation_error)

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewFailedEvent)
    assert result.reason == "parse_error"


async def test_run_returns_failed_on_server_error() -> None:
    fake = FakeLLM(error=RuntimeError("connection refused"))

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewFailedEvent)
    assert result.reason == "server_error"
    assert fake.call_count == 2  # 1회 재시도 후 실패


@pytest.mark.parametrize("reviews", [[], None])
def test_review_model_output_defaults(reviews: list[ReviewComment] | None) -> None:
    output = (
        ReviewModelOutput(summary="s")
        if reviews is None
        else ReviewModelOutput(summary="s", reviews=reviews)
    )
    assert output.reviews == []
