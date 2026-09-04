import pytest
from pydantic import ValidationError

from app.llm.client import ChatMessage
from app.rag.api_spec_schema import ApiSpecSearchResult
from app.rag.schema import ChunkSearchResult
from app.review.pipeline import ReviewPipeline, _truncate_diff_blocks
from app.review.schema import (
    ChangedFile,
    ContextFile,
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


class FakeNotionLinkStore:
    def __init__(self) -> None:
        self.saved: list[tuple[int, str]] = []

    async def save(self, *, repository_id: int, notion_database_url: str) -> None:
        self.saved.append((repository_id, notion_database_url))

    async def get(self, *, repository_id: int) -> str | None:
        return None

    async def list_all(self) -> list[tuple[int, str]]:
        return []


class FakeApiSpecRetriever:
    def __init__(self, results: list[ApiSpecSearchResult]) -> None:
        self.results = results
        self.received: tuple[str, int] | None = None

    def retrieve(self, query_text: str, repository_id: int) -> list[ApiSpecSearchResult]:
        self.received = (query_text, repository_id)
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


def test_truncate_diff_blocks_truncates_a_single_block_over_file_limit() -> None:
    huge_block = "x" * 9000

    result = _truncate_diff_blocks([huge_block], max_file_chars=8000, max_total_chars=20000)

    assert len(result) <= 8000
    assert result.endswith("...(truncated)")


def test_truncate_diff_blocks_leaves_small_blocks_untouched() -> None:
    blocks = ["small block a", "small block b"]

    result = _truncate_diff_blocks(blocks, max_file_chars=8000, max_total_chars=20000)

    assert result == "small block a\n\nsmall block b"


def test_truncate_diff_blocks_drops_later_blocks_once_total_limit_reached() -> None:
    blocks = ["a" * 7000, "b" * 7000, "c" * 7000, "d" * 7000]

    result = _truncate_diff_blocks(blocks, max_file_chars=8000, max_total_chars=20000)

    assert "a" * 7000 in result
    assert "b" * 7000 in result
    assert "d" * 7000 not in result
    assert len(result) <= 20000 + len("\n...(truncated)") * 4  # 마커 여유분


async def test_run_truncates_huge_single_new_file_diff() -> None:
    # PR #66에서 실제로 발생한 시나리오: 1300줄짜리 새 markdown 파일 하나가 diff로
    # 통째로 들어오면 LLM_MAX_CONTEXT를 넘겨 server_error로 조용히 실패했다.
    huge_patch = "@@ -0,0 +1,2000 @@\n" + "\n".join(f"+line {i}" for i in range(2000))
    event = ReviewRequestedEvent(
        review_job_id=make_review_job_id(42, 7, "abc123"),
        repository_id=42,
        pr_number=7,
        head_sha="abc123",
        base_sha="def456",
        changed_files=[
            ChangedFile(file_path="docs/huge.md", status="added", patch=huge_patch)
        ],
    )
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))

    result = await _pipeline(fake).run(event)

    assert isinstance(result, ReviewCompletedEvent)
    assert fake.received is not None
    user_message = fake.received[1]["content"]
    assert len(user_message) < len(huge_patch)
    assert "...(truncated)" in user_message


async def test_run_shares_diff_budget_with_project_context() -> None:
    # context와 diff를 각자 독립적으로 20000자씩 자르면 합쳐서 40000자까지 나갈 수
    # 있다 — 실제로 지켜야 하는 건 "둘을 합쳐서" 20000자다 (review-agent 지적).
    large_context_content = "line\n" * 3000  # 15000자
    huge_patch = "@@ -0,0 +1,3000 @@\n" + "\n".join(f"+line {i}" for i in range(3000))
    event = ReviewRequestedEvent(
        review_job_id=make_review_job_id(42, 7, "abc123"),
        repository_id=42,
        pr_number=7,
        head_sha="abc123",
        base_sha="def456",
        context_files=[ContextFile(path="DOVI.md", content=large_context_content)],
        changed_files=[
            ChangedFile(file_path="docs/huge.md", status="added", patch=huge_patch)
        ],
    )
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))

    await _pipeline(fake).run(event)

    assert fake.received is not None
    user_message = fake.received[1]["content"]
    # 헤더 라벨("## Project Context"/"## Changes")과 잘림 마커 정도의 여유만 두고,
    # context+diff 합계가 대략 20000자 안쪽이어야 한다 (context 혼자 20000, diff
    # 혼자 20000까지 각각 허용되던 예전 동작이었다면 최대 40000까지 나갔을 것).
    assert len(user_message) < 20500
    assert "...(truncated)" in user_message


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


async def test_run_logs_warning_when_long_summary_has_no_reviews(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # reviews[]는 비었는데 summary만 비정상적으로 길면, finding이 reviews[]
    # 대신 summary 프로즈에 새어 들어갔을 가능성이 있다는 관측 신호를 남긴다.
    long_summary = "x" * 500
    fake = FakeLLM(output=ReviewModelOutput(summary=long_summary, reviews=[]))

    with caplog.at_level("WARNING"):
        await _pipeline(fake).run(_event())

    assert any("summary unusually long" in record.message for record in caplog.records)


async def test_run_does_not_warn_for_normal_short_summary_with_no_reviews(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 가장 흔한 정상 케이스: 발견사항이 없어서 reviews도 비고 summary도 짧은 경우.
    fake = FakeLLM(
        output=ReviewModelOutput(summary="특이사항이 발견되지 않았습니다.", reviews=[])
    )

    with caplog.at_level("WARNING"):
        await _pipeline(fake).run(_event())

    assert not any("summary unusually long" in record.message for record in caplog.records)


async def test_run_does_not_warn_at_exact_length_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 정확히 임계값(400자)이면 "초과"가 아니므로 경고가 뜨면 안 된다.
    exact_summary = "x" * 400
    fake = FakeLLM(output=ReviewModelOutput(summary=exact_summary, reviews=[]))

    with caplog.at_level("WARNING"):
        await _pipeline(fake).run(_event())

    assert not any("summary unusually long" in record.message for record in caplog.records)


async def test_run_does_not_warn_when_long_summary_has_reviews(
    caplog: pytest.LogCaptureFixture,
) -> None:
    long_summary = "x" * 500
    reviews = [_comment(severity="critical", line=1, title="real bug")]
    fake = FakeLLM(output=ReviewModelOutput(summary=long_summary, reviews=reviews))

    with caplog.at_level("WARNING"):
        await _pipeline(fake).run(_event())

    assert not any("summary unusually long" in record.message for record in caplog.records)


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


async def test_run_saves_notion_link_when_no_swagger_present() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))
    link_store = FakeNotionLinkStore()
    event = _event()
    event.context_files = [
        ContextFile(
            path="DOVI.md",
            content="## API Specification\n- Notion API Spec: https://notion.so/abc\n",
        )
    ]
    pipeline = ReviewPipeline(
        fake, model_version="v", prompt_version="v1", notion_link_store=link_store
    )

    await pipeline.run(event)

    assert link_store.saved == [(42, "https://notion.so/abc")]


async def test_run_survives_notion_link_store_save_failure() -> None:
    class BoomNotionLinkStore:
        async def save(self, *, repository_id: int, notion_database_url: str) -> None:
            raise RuntimeError("redis unreachable")

        async def get(self, *, repository_id: int) -> str | None:
            return None

        async def list_all(self) -> list[tuple[int, str]]:
            return []

    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))
    event = _event()
    event.context_files = [
        ContextFile(
            path="DOVI.md",
            content="## API Specification\n- Notion API Spec: https://notion.so/abc\n",
        )
    ]
    pipeline = ReviewPipeline(
        fake,
        model_version="v",
        prompt_version="v1",
        notion_link_store=BoomNotionLinkStore(),
    )

    result = await pipeline.run(event)

    assert isinstance(result, ReviewCompletedEvent)
    assert result.summary == "ok"


async def test_run_does_not_save_notion_link_when_swagger_present() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))
    link_store = FakeNotionLinkStore()
    event = _event()
    event.context_files = [
        ContextFile(path="openapi.yaml", content="..."),
        ContextFile(
            path="DOVI.md",
            content="## API Specification\n- Notion API Spec: https://notion.so/abc\n",
        ),
    ]
    pipeline = ReviewPipeline(
        fake, model_version="v", prompt_version="v1", notion_link_store=link_store
    )

    await pipeline.run(event)

    assert link_store.saved == []


async def test_run_includes_api_spec_when_no_swagger_present() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))
    api_spec_retriever = FakeApiSpecRetriever(
        [
            ApiSpecSearchResult(
                method="GET",
                path="/api/x",
                summary="s",
                request_schema="",
                response_schema="",
                auth="",
                score=0.9,
            )
        ]
    )
    event = _event()  # context_files에 openapi/swagger 없음

    pipeline = ReviewPipeline(
        fake, model_version="v", prompt_version="v1", api_spec_retriever=api_spec_retriever
    )
    await pipeline.run(event)

    assert fake.received is not None
    user_message = fake.received[1]["content"]
    assert "관련 API 명세" in user_message
    assert "GET /api/x" in user_message


async def test_run_skips_api_spec_when_swagger_present() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))
    api_spec_retriever = FakeApiSpecRetriever(
        [
            ApiSpecSearchResult(
                method="GET",
                path="/api/x",
                summary="s",
                request_schema="",
                response_schema="",
                auth="",
                score=0.9,
            )
        ]
    )
    event = _event()
    event.context_files = [ContextFile(path="openapi.yaml", content="...")]

    pipeline = ReviewPipeline(
        fake, model_version="v", prompt_version="v1", api_spec_retriever=api_spec_retriever
    )
    await pipeline.run(event)

    assert api_spec_retriever.received is None
    assert fake.received is not None
    user_message = fake.received[1]["content"]
    assert "관련 API 명세" not in user_message


@pytest.mark.parametrize("reviews", [[], None])
def test_review_model_output_defaults(reviews: list[ReviewComment] | None) -> None:
    output = (
        ReviewModelOutput(summary="s")
        if reviews is None
        else ReviewModelOutput(summary="s", reviews=reviews)
    )
    assert output.reviews == []
