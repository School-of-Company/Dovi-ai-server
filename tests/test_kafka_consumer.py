from collections.abc import AsyncIterator

from app.kafka.consumer import ReviewRequestConsumer
from app.llm.client import ChatMessage
from app.review.pipeline import ReviewPipeline
from app.review.schema import (
    ChangedFile,
    ReviewCompletedEvent,
    ReviewFailedEvent,
    ReviewModelOutput,
    ReviewRequestedEvent,
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


class FailingLLM:
    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int = 1500
    ) -> ReviewModelOutput:
        raise RuntimeError("boom")


class FakeProducer:
    def __init__(self) -> None:
        self.completed: list[ReviewCompletedEvent] = []
        self.failed: list[ReviewFailedEvent] = []

    async def publish_completed(self, event: ReviewCompletedEvent) -> None:
        self.completed.append(event)

    async def publish_failed(self, event: ReviewFailedEvent) -> None:
        self.failed.append(event)


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
    consumer = ReviewRequestConsumer(
        FakeSource([]), _pipeline(ReviewModelOutput(summary="ok", reviews=[])), producer
    )

    await consumer.handle(_event_bytes())

    assert len(producer.completed) == 1
    assert producer.completed[0].review_job_id == "42:7:sha"
    assert len(producer.failed) == 0


async def test_handle_publishes_failed_on_pipeline_failure() -> None:
    producer = FakeProducer()
    pipeline = ReviewPipeline(FailingLLM(), model_version="v", prompt_version="v1")
    consumer = ReviewRequestConsumer(FakeSource([]), pipeline, producer)

    await consumer.handle(_event_bytes())

    assert len(producer.failed) == 1
    assert producer.failed[0].reason == "server_error"
    assert len(producer.completed) == 0


async def test_handle_invalid_payload_skips_without_publishing() -> None:
    producer = FakeProducer()
    consumer = ReviewRequestConsumer(
        FakeSource([]), _pipeline(ReviewModelOutput(summary="unused")), producer
    )

    await consumer.handle(b"not valid json")

    assert len(producer.completed) == 0
    assert len(producer.failed) == 0


async def test_run_processes_all_messages_and_commits_each() -> None:
    producer = FakeProducer()
    messages = [
        FakeMessage(_event_bytes("1:1:a")),
        FakeMessage(_event_bytes("2:2:b")),
    ]
    source = FakeSource(messages)
    consumer = ReviewRequestConsumer(
        source, _pipeline(ReviewModelOutput(summary="ok", reviews=[])), producer
    )

    await consumer.run()

    assert len(producer.completed) == 2
    assert source.commit_count == 2
