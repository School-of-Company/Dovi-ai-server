import pytest
from pydantic import ValidationError

from app.llm.client import ChatMessage
from app.rag.schema import ChunkSearchResult
from app.review.pipeline import ReviewPipeline
from app.review.schema import (
    ChangedFile,
    ReviewComment,
    ReviewCompletedEvent,
    ReviewFailedEvent,
    ReviewModelOutput,
    ReviewRequestedEvent,
    ReviewVerdict,
    Severity,
    VerificationResult,
    make_review_job_id,
)

# 검증 대상 finding이 이 개수를 넘는 테스트는 없다고 가정하고, 기본 fake는
# 넉넉하게 모든 인덱스를 confirmed 처리해 기존 테스트 동작을 그대로 유지한다.
_CONFIRM_ALL = VerificationResult(
    verdicts=[ReviewVerdict(index=i, confirmed=True, reason="ok") for i in range(20)]
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
        verify_result: VerificationResult | None = None,
        verify_error: Exception | None = None,
    ) -> None:
        self._output = output
        self._error = error
        self._sequence = sequence
        self._verify_result = verify_result if verify_result is not None else _CONFIRM_ALL
        self._verify_error = verify_error
        self.received: list[ChatMessage] | None = None
        self.verify_received: list[ChatMessage] | None = None
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

    async def verify_findings(
        self, messages: list[ChatMessage], *, max_tokens: int = 800
    ) -> VerificationResult:
        self.verify_received = messages
        if self._verify_error is not None:
            raise self._verify_error
        return self._verify_result


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


def _pipeline(fake: FakeLLM, retriever: object = None) -> ReviewPipeline:
    return ReviewPipeline(
        fake,
        model_version="qwen2.5-coder-14b",
        prompt_version="v1",
        retriever=retriever,  # type: ignore[arg-type]
    )


class FakeRetriever:
    def __init__(self, results: list[ChunkSearchResult]) -> None:
        self.results = results
        self.received_queries: list[tuple[str, int, str | None]] = []

    def retrieve(
        self,
        query_text: str,
        repository_id: int,
        exclude_file_path: str | None = None,
    ) -> list[ChunkSearchResult]:
        self.received_queries.append((query_text, repository_id, exclude_file_path))
        return self.results


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


async def test_run_includes_related_project_code_from_retriever() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))
    retriever = FakeRetriever(
        [
            ChunkSearchResult(
                file_path="app/other.py",
                node_type="function_definition",
                name="helper",
                start_line=1,
                end_line=3,
                source="def helper(): return 1",
                score=0.9,
            )
        ]
    )

    await _pipeline(fake, retriever).run(_event())

    assert fake.received is not None
    user_message = fake.received[1]["content"]
    assert "관련 프로젝트 코드" in user_message
    assert "def helper(): return 1" in user_message
    # 리뷰 대상 파일 자기 자신은 제외하도록 exclude_file_path를 넘겼는지 확인
    assert retriever.received_queries == [("@@ -1 +1 @@", 42, "app/main.py")]


async def test_run_without_retriever_skips_related_context_section() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))

    await _pipeline(fake).run(_event())

    assert fake.received is not None
    user_message = fake.received[1]["content"]
    assert "관련 프로젝트 코드" not in user_message


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


async def test_run_replaces_empty_summary_with_fallback() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="   ", reviews=[]))

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewCompletedEvent)
    assert result.summary.strip() != ""
    assert "요약 생성에 실패했습니다" in result.summary


async def test_run_drops_disputed_findings() -> None:
    reviews = [
        _comment(severity="critical", line=1, title="real bug"),
        _comment(severity="major", line=2, title="false positive"),
    ]
    verify_result = VerificationResult(
        verdicts=[
            ReviewVerdict(index=0, confirmed=True, reason="실제로 문제 있음"),
            ReviewVerdict(index=1, confirmed=False, reason="구조적 타이핑이라 문제 없음"),
        ]
    )
    fake = FakeLLM(
        output=ReviewModelOutput(summary="요약", reviews=reviews),
        verify_result=verify_result,
    )

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewCompletedEvent)
    assert [r.title for r in result.reviews] == ["real bug"]
    assert fake.verify_received is not None


async def test_run_treats_missing_verdict_as_disputed() -> None:
    reviews = [_comment(severity="critical", line=1, title="finding")]
    fake = FakeLLM(
        output=ReviewModelOutput(summary="요약", reviews=reviews),
        verify_result=VerificationResult(verdicts=[]),
    )

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewCompletedEvent)
    assert result.reviews == []


async def test_run_discards_all_findings_when_verification_call_fails() -> None:
    reviews = [_comment(severity="critical", line=1, title="finding")]
    fake = FakeLLM(
        output=ReviewModelOutput(summary="요약", reviews=reviews),
        verify_error=RuntimeError("llm down"),
    )

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewCompletedEvent)
    assert result.reviews == []


async def test_run_skips_verification_when_no_inline_findings() -> None:
    reviews = [_comment(severity="minor", line=1, title="minor finding")]
    fake = FakeLLM(output=ReviewModelOutput(summary="요약", reviews=reviews))

    result = await _pipeline(fake).run(_event())

    assert isinstance(result, ReviewCompletedEvent)
    assert fake.verify_received is None


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
