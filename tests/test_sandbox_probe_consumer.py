import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from app.sandbox_probe.consumer import SandboxProbeConsumer
from app.sandbox_probe.schema import SandboxProbeCompletedEvent, SandboxProbeRequestedEvent


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


class FakeRunner:
    def __init__(self, status: str = "passed", error: BaseException | None = None) -> None:
        self.events: list[SandboxProbeRequestedEvent] = []
        self._status = status
        self._error = error

    async def run(self, event: SandboxProbeRequestedEvent) -> SandboxProbeCompletedEvent:
        self.events.append(event)
        if self._error is not None:
            raise self._error
        return SandboxProbeCompletedEvent(
            review_job_id=event.review_job_id,
            repository_id=event.repository_id,
            pr_number=event.pr_number,
            head_sha=event.head_sha,
            status=self._status,  # type: ignore[arg-type]
        )


class FakeProducer:
    def __init__(self, error: Exception | None = None) -> None:
        self.completed: list[SandboxProbeCompletedEvent] = []
        self._error = error

    async def publish_completed(self, event: SandboxProbeCompletedEvent) -> None:
        if self._error is not None:
            raise self._error
        self.completed.append(event)


class FakeDedup:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[tuple[str, str]] = []

    async def try_start(self, job_id: str) -> bool:
        self.calls.append(("start", job_id))
        return self.allow

    async def mark_completed(self, job_id: str) -> None:
        self.calls.append(("completed", job_id))

    async def mark_failed(self, job_id: str) -> None:
        self.calls.append(("failed", job_id))


class FakeCounter:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.expired: list[str] = []

    async def incr(self, name: str) -> int:
        self.counts[name] = self.counts.get(name, 0) + 1
        return self.counts[name]

    async def expire(self, name: str, time: int) -> object:
        self.expired.append(name)
        return True


def _payload(job_id: str = "42:7:abc") -> bytes:
    return json.dumps(
        {
            "reviewJobId": job_id,
            "repositoryId": 42,
            "installationId": 99,
            "repoFullName": "org/repo",
            "prNumber": 7,
            "headSha": "abc",
            "baseSha": "def",
        }
    ).encode()


def _consumer(
    runner: FakeRunner | None = None,
    producer: FakeProducer | None = None,
    dedup: FakeDedup | None = None,
    counter: FakeCounter | None = None,
    source: FakeSource | None = None,
    max_attempts: int = 2,
) -> SandboxProbeConsumer:
    return SandboxProbeConsumer(
        source or FakeSource([]),
        runner or FakeRunner(),
        producer or FakeProducer(),
        dedup or FakeDedup(),
        counter or FakeCounter(),
        max_attempts=max_attempts,
    )


async def test_handle_runs_the_probe_publishes_and_marks_completed() -> None:
    runner, producer, dedup = FakeRunner("found_issue"), FakeProducer(), FakeDedup()

    await _consumer(runner, producer, dedup).handle(_payload())

    assert [e.installation_id for e in runner.events] == [99]
    assert [e.status for e in producer.completed] == ["found_issue"]
    assert dedup.calls == [("start", "42:7:abc"), ("completed", "42:7:abc")]


async def test_handle_skips_duplicate_or_in_progress_jobs() -> None:
    runner, producer = FakeRunner(), FakeProducer()

    await _consumer(runner, producer, FakeDedup(allow=False)).handle(_payload())

    assert runner.events == []
    assert producer.completed == []


async def test_handle_skips_invalid_payloads_without_touching_dedup() -> None:
    dedup = FakeDedup()

    await _consumer(dedup=dedup).handle(b'{"reviewJobId": "x"}')

    assert dedup.calls == []


async def test_attempt_counter_is_incremented_before_running_and_expires() -> None:
    counter = FakeCounter()

    await _consumer(counter=counter).handle(_payload())

    assert counter.counts == {"ai-review:sandbox-probe-attempts:42:7:abc": 1}
    assert counter.expired == ["ai-review:sandbox-probe-attempts:42:7:abc"]


async def test_job_that_exceeds_max_attempts_is_closed_as_inconclusive_without_running() -> None:
    runner, producer, counter = FakeRunner(), FakeProducer(), FakeCounter()
    counter.counts["ai-review:sandbox-probe-attempts:42:7:abc"] = 2

    await _consumer(runner, producer, counter=counter, max_attempts=2).handle(_payload())

    assert runner.events == []
    assert [e.status for e in producer.completed] == ["inconclusive"]


async def test_publish_failure_releases_the_lock_and_propagates_for_redelivery() -> None:
    dedup = FakeDedup()

    with pytest.raises(RuntimeError):
        await _consumer(
            producer=FakeProducer(error=RuntimeError("kafka down")), dedup=dedup
        ).handle(_payload())

    assert dedup.calls[-1] == ("failed", "42:7:abc")


async def test_cancellation_releases_the_lock() -> None:
    dedup = FakeDedup()

    with pytest.raises(asyncio.CancelledError):
        await _consumer(
            runner=FakeRunner(error=asyncio.CancelledError()), dedup=dedup
        ).handle(_payload())

    assert dedup.calls[-1] == ("failed", "42:7:abc")


async def test_run_commits_after_each_message_and_stops_on_shutdown() -> None:
    source = FakeSource([FakeMessage(_payload("1:1:a")), FakeMessage(_payload("2:2:b"))])
    runner = FakeRunner()
    shutdown = asyncio.Event()
    shutdown.set()

    await _consumer(runner, source=source).run(shutdown)

    assert source.commit_count == 1
    assert len(runner.events) == 1
