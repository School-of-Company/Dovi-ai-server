from collections.abc import AsyncIterator

from app.evaluation.feedback_consumer import ReviewFeedbackConsumer
from app.evaluation.schema import ReviewFeedbackEvent


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


class FakeRepository:
    def __init__(self) -> None:
        self.upserted: list[ReviewFeedbackEvent] = []

    async def save_completed(self, event: object) -> None:
        raise AssertionError("not used by this consumer")

    async def save_failed(self, event: object) -> None:
        raise AssertionError("not used by this consumer")

    async def upsert_feedback(self, feedback: ReviewFeedbackEvent) -> None:
        self.upserted.append(feedback)


async def test_handle_valid_event_upserts_feedback() -> None:
    repository = FakeRepository()
    consumer = ReviewFeedbackConsumer(FakeSource([]), repository)
    raw = (
        b'{"reviewJobId": "1:2:sha", "findingIndex": 0, '
        b'"reflected": true, "reason": "applied"}'
    )

    await consumer.handle(raw)

    assert len(repository.upserted) == 1
    assert repository.upserted[0].review_job_id == "1:2:sha"
    assert repository.upserted[0].reflected is True


async def test_handle_invalid_payload_skips_without_raising() -> None:
    repository = FakeRepository()
    consumer = ReviewFeedbackConsumer(FakeSource([]), repository)

    await consumer.handle(b"not json")

    assert repository.upserted == []


async def test_run_processes_all_messages_and_commits() -> None:
    repository = FakeRepository()
    valid = (
        b'{"reviewJobId": "1:2:sha", "findingIndex": 0, '
        b'"reflected": false, "reason": null}'
    )
    source = FakeSource([FakeMessage(valid), FakeMessage(b"bad")])
    consumer = ReviewFeedbackConsumer(source, repository)

    await consumer.run()

    assert len(repository.upserted) == 1
    assert source.commit_count == 2
