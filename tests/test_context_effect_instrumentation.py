from app.context.release_notes_cache import CachedReleaseNotes, RedisReleaseNotesCache
from app.evaluation.context_effect.instrumentation import (
    _EVIDENCE_HEADER_MARKER,
    CountingReleaseNotesCache,
    InMemoryRedis,
    PromptCapturingLLM,
    TimedOfficialDocsWorkflow,
    changed_npm_packages,
    strip_to_diff_only,
)
from app.llm.client import ChatMessage
from app.review.schema import (
    ChangedFile,
    ContextFile,
    ReviewModelOutput,
    ReviewRequestedEvent,
    VerificationResult,
    make_review_job_id,
)


def _event() -> ReviewRequestedEvent:
    return ReviewRequestedEvent(
        review_job_id=make_review_job_id(1, 1, "sha"),
        repository_id=1,
        pr_number=1,
        head_sha="sha",
        base_sha="base",
        pr_title="title",
        pr_body="body",
        context_files=[ContextFile(path="DOVI.md", content="context")],
        changed_files=[
            ChangedFile(
                file_path="a.py",
                status="modified",
                patch="@@ -1 +1 @@\n-old\n+new",
                content="new content",
                previous_content="old content",
            )
        ],
    )


class TestStripToDiffOnly:
    def test_removes_context_files_and_file_contents(self) -> None:
        event = _event()

        stripped = strip_to_diff_only(event)

        assert stripped.context_files == []
        assert stripped.changed_files[0].content is None
        assert stripped.changed_files[0].previous_content is None
        assert stripped.changed_files[0].patch == event.changed_files[0].patch

    def test_keeps_pr_title_and_body(self) -> None:
        event = _event()

        stripped = strip_to_diff_only(event)

        assert stripped.pr_title == "title"
        assert stripped.pr_body == "body"

    def test_does_not_mutate_original_event(self) -> None:
        event = _event()
        original_changed_files = event.changed_files
        original_first_file = event.changed_files[0]

        strip_to_diff_only(event)

        assert event.context_files != []
        assert event.changed_files is original_changed_files
        assert event.changed_files[0] is original_first_file
        assert event.changed_files[0].content == "new content"
        assert event.changed_files[0].previous_content == "old content"


class TestCountingReleaseNotesCache:
    async def test_counts_hit_and_miss(self) -> None:
        cache = CountingReleaseNotesCache(RedisReleaseNotesCache(InMemoryRedis()))

        assert await cache.get("axios/axios", "1.0.0") is None
        assert cache.misses == 1
        assert cache.hits == 0

        await cache.set("axios/axios", "1.0.0", "notes")
        result = await cache.get("axios/axios", "1.0.0")

        assert result == CachedReleaseNotes(notes="notes")
        assert cache.hits == 1
        assert cache.misses == 1

    async def test_set_delegates_to_inner(self) -> None:
        inner_redis = InMemoryRedis()
        cache = CountingReleaseNotesCache(RedisReleaseNotesCache(inner_redis))

        await cache.set("axios/axios", "1.0.0", "notes")

        assert "ai-review:release-notes:axios/axios@1.0.0" in inner_redis._store


class FakeOfficialDocsBuilder:
    def __init__(self, evidence: str = "evidence") -> None:
        self._evidence = evidence
        self.received_changed_files: list[ChangedFile] | None = None

    async def build_evidence(self, changed_files: list[ChangedFile]) -> str:
        self.received_changed_files = changed_files
        return self._evidence


class TestTimedOfficialDocsWorkflow:
    async def test_records_latency_and_returns_inner_result(self) -> None:
        inner = FakeOfficialDocsBuilder("evidence text")
        timed = TimedOfficialDocsWorkflow(inner)

        result = await timed.build_evidence([])

        assert result == "evidence text"
        assert timed.last_latency_ms is not None
        assert timed.last_latency_ms >= 0.0


class FakeReviewLLM:
    def __init__(self, output: ReviewModelOutput) -> None:
        self._output = output
        self.generate_calls: list[list[ChatMessage]] = []

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1500,
        max_reviews: int | None = None,
    ) -> ReviewModelOutput:
        self.generate_calls.append(messages)
        return self._output

    async def verify_findings(
        self, messages: list[ChatMessage], *, max_tokens: int = 800
    ) -> VerificationResult:
        return VerificationResult(verdicts=[])

    async def count_tokens(self, text: str) -> int:
        return 1

    async def get_context_window(self) -> int | None:
        return None


class TestPromptCapturingLLM:
    async def test_detects_evidence_header_marker(self) -> None:
        inner = FakeReviewLLM(ReviewModelOutput(summary="ok", reviews=[]))
        capturing = PromptCapturingLLM(inner)
        messages: list[ChatMessage] = [
            {"role": "system", "content": "sys"},
            {
                "role": "user",
                "content": (
                    "## Changes\n\n#### 의존성 버전 변경 근거 (공식 릴리즈 노트)\n"
                    "axios@1.0.0"
                ),
            },
        ]

        await capturing.generate(messages)

        assert capturing.evidence_seen is True
        assert inner.generate_calls == [messages]

    async def test_does_not_flag_when_no_evidence_header(self) -> None:
        inner = FakeReviewLLM(ReviewModelOutput(summary="ok", reviews=[]))
        capturing = PromptCapturingLLM(inner)
        messages: list[ChatMessage] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "## Changes\n\nplain diff, no evidence"},
        ]

        await capturing.generate(messages)

        assert capturing.evidence_seen is False


def test_evidence_header_marker_stays_in_sync_with_official_docs_workflow() -> None:
    """_EVIDENCE_HEADER_MARKER는 official_docs_workflow._HEADER(private)의 부분
    문자열을 수동으로 복제한 값이다. 원본 헤더 텍스트가 바뀌면 이 회귀 테스트가
    실패해야 한다 — 그렇지 않으면 PromptCapturingLLM.evidence_seen이 항상
    False가 되어 evidence_prompt_rate가 조용히 0으로 나온다.
    """
    from app.context.official_docs_workflow import _HEADER

    assert _EVIDENCE_HEADER_MARKER in _HEADER


class TestChangedNpmPackages:
    def test_extracts_and_dedups_package_names(self) -> None:
        patch = (
            '@@ -1,3 +1,6 @@\n'
            ' {\n'
            '+    "node_modules/axios": {\n'
            '+      "version": "1.2.0",\n'
            "+    },\n"
        )
        event = ReviewRequestedEvent(
            review_job_id=make_review_job_id(1, 1, "sha"),
            repository_id=1,
            pr_number=1,
            head_sha="sha",
            base_sha="base",
            changed_files=[
                ChangedFile(file_path="package-lock.json", status="modified", patch=patch),
                ChangedFile(
                    file_path="nested/package-lock.json", status="modified", patch=patch
                ),
                ChangedFile(file_path="app/service.py", status="modified", patch="@@ -1 +1 @@"),
            ],
        )

        packages = changed_npm_packages(event)

        assert packages == ["axios"]

    def test_returns_empty_list_when_no_lockfile_changed(self) -> None:
        event = ReviewRequestedEvent(
            review_job_id=make_review_job_id(1, 1, "sha"),
            repository_id=1,
            pr_number=1,
            head_sha="sha",
            base_sha="base",
            changed_files=[
                ChangedFile(file_path="app/service.py", status="modified", patch="@@ -1 +1 @@")
            ],
        )

        assert changed_npm_packages(event) == []
