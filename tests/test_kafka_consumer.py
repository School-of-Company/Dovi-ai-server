import asyncio
from collections.abc import AsyncIterator

import pytest

from app.kafka.consumer import ReviewRequestConsumer
from app.llm.client import ChatMessage
from app.review.pipeline import ReviewPipeline
from app.review.schema import (
    ChangedFile,
    ReviewCompletedEvent,
    ReviewFailedEvent,
    ReviewModelOutput,
    ReviewRequestedEvent,
    VerificationResult,
)


class FakeMessage:
    def __init__(self, value: bytes) -> None:
        self.value = value


class FakeSource:
    def __init__(self, messages: list[FakeMessage]) -> None:
        self._messages = messages
        self.commit_count = 0

    async def __aiter__(self) -> AsyncIterator[FakeMessage]:
        for message in self._messages:
            yield message

    async def commit(self) -> None:
        self.commit_count += 1


class FakeLLM:
    def __init__(self, output: ReviewModelOutput) -> None:
        self._output = output

    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int = 1500
    ) -> ReviewModelOutput:
        return self._output

    async def verify_findings(
        self, messages: list[ChatMessage], *, max_tokens: int = 800
    ) -> VerificationResult:
        return VerificationResult(verdicts=[])


class FailingLLM:
    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int = 1500
    ) -> ReviewModelOutput:
        raise RuntimeError("boom")

    async def verify_findings(
        self, messages: list[ChatMessage], *, max_tokens: int = 800
    ) -> VerificationResult:
        return VerificationResult(verdicts=[])


class CancellingLLM:
    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int = 1500
    ) -> ReviewModelOutput:
        raise asyncio.CancelledError()

    async def verify_findings(
        self, messages: list[ChatMessage], *, max_tokens: int = 800
    ) -> VerificationResult:
        return VerificationResult(verdicts=[])


class FakeProducer:
    def __init__(self) -> None:
        self.completed: list[ReviewCompletedEvent] = []
        self.failed: list[ReviewFailedEvent] = []

    async def publish_completed(self, event: ReviewCompletedEvent) -> None:
        self.completed.append(event)

    async def publish_failed(self, event: ReviewFailedEvent) -> None:
        self.failed.append(event)


class FakeDedupStore:
    def __init__(self) -> None:
        self._started: set[str] = set()
        self.completed: list[str] = []
        self.failed: list[str] = []

    async def try_start(self, review_job_id: str) -> bool:
        if review_job_id in self._started:
            return False
        self._started.add(review_job_id)
        return True

    async def mark_completed(self, review_job_id: str) -> None:
        self.completed.append(review_job_id)

    async def mark_failed(self, review_job_id: str) -> None:
        self.failed.append(review_job_id)
        self._started.discard(review_job_id)


def _event_bytes(job_id: str = "42:7:sha") -> bytes:
    event = ReviewRequestedEvent(
        review_job_id=job_id,
        repository_id=42,
        pr_number=7,
        head_sha="sha",
        base_sha="base",
        changed_files=[
            ChangedFile(file_path="a.py", status="modified", patch="@@ -1 +1 @@\n-a\n+b")
        ],
    )
    return event.model_dump_json(by_alias=True).encode("utf-8")


def _pipeline(output: ReviewModelOutput) -> ReviewPipeline:
    return ReviewPipeline(FakeLLM(output), model_version="v", prompt_version="v1")


async def test_handle_publishes_completed_on_success() -> None:
    producer = FakeProducer()
    dedup = FakeDedupStore()
    consumer = ReviewRequestConsumer(
        FakeSource([]),
        _pipeline(ReviewModelOutput(summary="ok", reviews=[])),
        producer,
        dedup,
    )

    await consumer.handle(_event_bytes())

    assert len(producer.completed) == 1
    assert producer.completed[0].review_job_id == "42:7:sha"
    assert len(producer.failed) == 0
    assert dedup.completed == ["42:7:sha"]


async def test_handle_publishes_failed_on_pipeline_failure() -> None:
    producer = FakeProducer()
    dedup = FakeDedupStore()
    pipeline = ReviewPipeline(FailingLLM(), model_version="v", prompt_version="v1")
    consumer = ReviewRequestConsumer(FakeSource([]), pipeline, producer, dedup)

    await consumer.handle(_event_bytes())

    assert len(producer.failed) == 1
    assert producer.failed[0].reason == "server_error"
    assert len(producer.completed) == 0
    assert dedup.failed == ["42:7:sha"]


async def test_handle_invalid_payload_skips_without_publishing() -> None:
    producer = FakeProducer()
    consumer = ReviewRequestConsumer(
        FakeSource([]),
        _pipeline(ReviewModelOutput(summary="unused")),
        producer,
        FakeDedupStore(),
    )

    await consumer.handle(b"not valid json")

    assert len(producer.completed) == 0
    assert len(producer.failed) == 0


async def test_handle_skips_duplicate_review_job_id() -> None:
    producer = FakeProducer()
    dedup = FakeDedupStore()
    consumer = ReviewRequestConsumer(
        FakeSource([]),
        _pipeline(ReviewModelOutput(summary="ok", reviews=[])),
        producer,
        dedup,
    )

    await consumer.handle(_event_bytes())
    await consumer.handle(_event_bytes())  # 같은 reviewJobId 재전달

    assert len(producer.completed) == 1  # 두 번째는 skip


async def test_handle_releases_dedup_lock_on_cancellation() -> None:
    producer = FakeProducer()
    dedup = FakeDedupStore()
    pipeline = ReviewPipeline(CancellingLLM(), model_version="v", prompt_version="v1")
    consumer = ReviewRequestConsumer(FakeSource([]), pipeline, producer, dedup)

    with pytest.raises(asyncio.CancelledError):
        await consumer.handle(_event_bytes())

    # graceful shutdown 유예시간을 넘겨 강제 취소돼도 락은 풀려야 재전달된
    # 메시지를 새 인스턴스가 TTL 만료를 기다리지 않고 재처리할 수 있다.
    assert dedup.failed == ["42:7:sha"]
    assert len(producer.completed) == 0
    assert len(producer.failed) == 0


async def test_run_stops_after_current_message_when_shutdown_requested() -> None:
    producer = FakeProducer()
    messages = [
        FakeMessage(_event_bytes("1:1:a")),
        FakeMessage(_event_bytes("2:2:b")),
    ]
    source = FakeSource(messages)
    consumer = ReviewRequestConsumer(
        source,
        _pipeline(ReviewModelOutput(summary="ok", reviews=[])),
        producer,
        FakeDedupStore(),
    )
    shutdown = asyncio.Event()
    shutdown.set()

    await consumer.run(shutdown)

    # shutdown이 이미 설정돼 있었으므로 첫 메시지만 마무리하고 커밋한 뒤 반환한다.
    assert len(producer.completed) == 1
    assert source.commit_count == 1


async def test_run_processes_all_messages_and_commits_each() -> None:
    producer = FakeProducer()
    messages = [
        FakeMessage(_event_bytes("1:1:a")),
        FakeMessage(_event_bytes("2:2:b")),
    ]
    source = FakeSource(messages)
    consumer = ReviewRequestConsumer(
        source,
        _pipeline(ReviewModelOutput(summary="ok", reviews=[])),
        producer,
        FakeDedupStore(),
    )

    await consumer.run()

    assert len(producer.completed) == 2
    assert source.commit_count == 2
