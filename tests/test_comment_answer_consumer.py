import asyncio
from collections.abc import AsyncIterator

import pytest

from app.comment_answer.consumer import CommentAnswerConsumer
from app.comment_answer.pipeline import CommentAnswerPipeline
from app.comment_answer.schema import (
    CommentAnswerCompletedEvent,
    CommentAnswerFailedEvent,
    CommentAnswerRequestedEvent,
)
from app.llm.client import ChatMessage


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


class FakeTextLLM:
    def __init__(self, text: str = "ok") -> None:
        self._text = text

    async def generate_text(
        self, messages: list[ChatMessage], *, max_tokens: int = 500
    ) -> str:
        return self._text


class FailingTextLLM:
    async def generate_text(
        self, messages: list[ChatMessage], *, max_tokens: int = 500
    ) -> str:
        raise RuntimeError("boom")


class CancellingTextLLM:
    async def generate_text(
        self, messages: list[ChatMessage], *, max_tokens: int = 500
    ) -> str:
        raise asyncio.CancelledError()


class FakeProducer:
    def __init__(self) -> None:
        self.completed: list[CommentAnswerCompletedEvent] = []
        self.failed: list[CommentAnswerFailedEvent] = []

    async def publish_completed(self, event: CommentAnswerCompletedEvent) -> None:
        self.completed.append(event)

    async def publish_failed(self, event: CommentAnswerFailedEvent) -> None:
        self.failed.append(event)


class FakeDedupStore:
    def __init__(self) -> None:
        self._started: set[str] = set()
        self.completed: list[str] = []
        self.failed: list[str] = []

    async def try_start(self, comment_job_id: str) -> bool:
        if comment_job_id in self._started:
            return False
        self._started.add(comment_job_id)
        return True

    async def mark_completed(self, comment_job_id: str) -> None:
        self.completed.append(comment_job_id)

    async def mark_failed(self, comment_job_id: str) -> None:
        self.failed.append(comment_job_id)
        self._started.discard(comment_job_id)


def _event_bytes(job_id: str = "qa:1:2:100") -> bytes:
    event = CommentAnswerRequestedEvent(
        comment_job_id=job_id,
        repository_id=1,
        pr_number=2,
        path="src/foo.ts",
        line=12,
        diff_hunk="@@ -1 +1 @@\n-old\n+new",
        thread=[],
    )
    return event.model_dump_json(by_alias=True).encode("utf-8")


def _pipeline(text: str = "ok") -> CommentAnswerPipeline:
    return CommentAnswerPipeline(FakeTextLLM(text))


async def test_handle_publishes_completed_on_success() -> None:
    producer = FakeProducer()
    dedup = FakeDedupStore()
    consumer = CommentAnswerConsumer(FakeSource([]), _pipeline("답변"), producer, dedup)

    await consumer.handle(_event_bytes())

    assert len(producer.completed) == 1
    assert producer.completed[0].comment_job_id == "qa:1:2:100"
    assert producer.completed[0].answer == "답변"
    assert len(producer.failed) == 0
    assert dedup.completed == ["qa:1:2:100"]


async def test_handle_publishes_failed_on_pipeline_failure() -> None:
    producer = FakeProducer()
    dedup = FakeDedupStore()
    pipeline = CommentAnswerPipeline(FailingTextLLM())
    consumer = CommentAnswerConsumer(FakeSource([]), pipeline, producer, dedup)

    await consumer.handle(_event_bytes())

    assert len(producer.failed) == 1
    assert producer.failed[0].reason == "server_error"
    assert len(producer.completed) == 0
    assert dedup.failed == ["qa:1:2:100"]


async def test_handle_invalid_payload_skips_without_publishing() -> None:
    producer = FakeProducer()
    consumer = CommentAnswerConsumer(
        FakeSource([]), _pipeline(), producer, FakeDedupStore()
    )

    await consumer.handle(b"not valid json")

    assert len(producer.completed) == 0
    assert len(producer.failed) == 0


async def test_handle_skips_duplicate_comment_job_id() -> None:
    producer = FakeProducer()
    dedup = FakeDedupStore()
    consumer = CommentAnswerConsumer(FakeSource([]), _pipeline(), producer, dedup)

    await consumer.handle(_event_bytes())
    await consumer.handle(_event_bytes())  # 같은 commentJobId 재전달

    assert len(producer.completed) == 1  # 두 번째는 skip


async def test_handle_releases_dedup_lock_on_cancellation() -> None:
    producer = FakeProducer()
    dedup = FakeDedupStore()
    pipeline = CommentAnswerPipeline(CancellingTextLLM())
    consumer = CommentAnswerConsumer(FakeSource([]), pipeline, producer, dedup)

    with pytest.raises(asyncio.CancelledError):
        await consumer.handle(_event_bytes())

    assert dedup.failed == ["qa:1:2:100"]
    assert len(producer.completed) == 0
    assert len(producer.failed) == 0


async def test_run_stops_after_current_message_when_shutdown_requested() -> None:
    producer = FakeProducer()
    messages = [
        FakeMessage(_event_bytes("qa:1:2:100")),
        FakeMessage(_event_bytes("qa:1:2:101")),
    ]
    source = FakeSource(messages)
    consumer = CommentAnswerConsumer(source, _pipeline(), producer, FakeDedupStore())
    shutdown = asyncio.Event()
    shutdown.set()

    await consumer.run(shutdown)

    assert len(producer.completed) == 1
    assert source.commit_count == 1


async def test_run_processes_all_messages_and_commits_each() -> None:
    producer = FakeProducer()
    messages = [
        FakeMessage(_event_bytes("qa:1:2:100")),
        FakeMessage(_event_bytes("qa:1:2:101")),
    ]
    source = FakeSource(messages)
    consumer = CommentAnswerConsumer(source, _pipeline(), producer, FakeDedupStore())

    await consumer.run()

    assert len(producer.completed) == 2
    assert source.commit_count == 2
