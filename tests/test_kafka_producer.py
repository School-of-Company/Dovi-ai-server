import json

from app.kafka.producer import ReviewEventProducer, SandboxProbeEventProducer
from app.review.schema import ReviewCompletedEvent, ReviewFailedEvent
from app.sandbox_probe.schema import Finding, SandboxProbeCompletedEvent


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes | None]] = []

    async def send_and_wait(
        self, topic: str, value: bytes, key: bytes | None = None
    ) -> None:
        self.sent.append((topic, value, key))


def _completed_event() -> ReviewCompletedEvent:
    return ReviewCompletedEvent(
        review_job_id="1:2:sha",
        repository_id=1,
        pr_number=2,
        head_sha="sha",
        summary="ok",
        reviews=[],
        model_version="qwen2.5-coder-32b",
        prompt_version="v1",
    )


def _failed_event() -> ReviewFailedEvent:
    return ReviewFailedEvent(review_job_id="1:2:sha", head_sha="sha", reason="timeout")


def _producer(sender: FakeSender) -> ReviewEventProducer:
    return ReviewEventProducer(
        sender, completed_topic="pr.review.completed", failed_topic="pr.review.failed"
    )


async def test_publish_completed_sends_to_completed_topic() -> None:
    sender = FakeSender()
    await _producer(sender).publish_completed(_completed_event())

    assert len(sender.sent) == 1
    topic, value, key = sender.sent[0]
    assert topic == "pr.review.completed"
    assert key == b"1:2:sha"
    payload = json.loads(value)
    assert payload["reviewJobId"] == "1:2:sha"
    assert payload["modelVersion"] == "qwen2.5-coder-32b"


async def test_publish_failed_sends_to_failed_topic() -> None:
    sender = FakeSender()
    await _producer(sender).publish_failed(_failed_event())

    assert len(sender.sent) == 1
    topic, value, key = sender.sent[0]
    assert topic == "pr.review.failed"
    assert key == b"1:2:sha"
    payload = json.loads(value)
    assert payload["reason"] == "timeout"


async def test_sandbox_probe_producer_sends_camel_case_event_keyed_by_review_job_id() -> None:
    sender = FakeSender()
    producer = SandboxProbeEventProducer(sender, completed_topic="pr.sandbox.probe.completed")
    event = SandboxProbeCompletedEvent(
        review_job_id="1:2:sha",
        repository_id=1,
        pr_number=2,
        head_sha="sha",
        status="found_issue",
        findings=[
            Finding(probe="lifecycle", title="t", message="m", evidence="e", line=None)
        ],
    )

    await producer.publish_completed(event)

    topic, value, key = sender.sent[0]
    assert topic == "pr.sandbox.probe.completed"
    assert key == b"1:2:sha"
    payload = json.loads(value)
    assert payload["status"] == "found_issue"
    assert payload["findings"][0]["probe"] == "lifecycle"
    assert payload["findings"][0]["filePath"] is None
