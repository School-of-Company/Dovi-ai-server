from collections.abc import AsyncIterator

import pytest

from app.kafka.consumer import ReviewRequestConsumer
from app.review.schema import ReviewRequestedEvent
from app.review.superseded import PrHeadTracker, PrHeadTrackerConsumer


class FakeRedis:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.hashes: dict[str, dict[bytes, bytes]] = {}
        self.ttls: dict[str, int] = {}
        self.fail_hgetall = False

    async def incr(self, name: str) -> int:
        self.counters[name] = self.counters.get(name, 0) + 1
        return self.counters[name]

    async def hsetnx(self, name: str, key: str, value: str) -> int:
        fields = self.hashes.setdefault(name, {})
        if key.encode() in fields:
            return 0
        fields[key.encode()] = value.encode()
        return 1

    async def hgetall(self, name: str) -> dict[bytes, bytes]:
        if self.fail_hgetall:
            raise RuntimeError("redis down")
        return dict(self.hashes.get(name, {}))

    async def expire(self, name: str, time: int) -> bool:
        self.ttls[name] = time
        return True


def _event(head: str, *, repo: int = 1, pr: int = 7, job_suffix: str = "") -> ReviewRequestedEvent:
    return ReviewRequestedEvent(
        review_job_id=f"{repo}:{pr}:{head}{job_suffix}",
        repository_id=repo,
        pr_number=pr,
        head_sha=head,
        base_sha="base",
    )


async def test_older_head_is_superseded_once_newer_head_is_recorded() -> None:
    tracker = PrHeadTracker(FakeRedis())
    await tracker.record(_event("a"))
    await tracker.record(_event("b"))

    assert await tracker.is_superseded(_event("a")) is True
    assert await tracker.is_superseded(_event("b")) is False


async def test_latest_head_alone_is_not_superseded() -> None:
    tracker = PrHeadTracker(FakeRedis())
    await tracker.record(_event("a"))

    assert await tracker.is_superseded(_event("a")) is False


async def test_unrecorded_head_is_never_skipped() -> None:
    tracker = PrHeadTracker(FakeRedis())
    await tracker.record(_event("b"))

    assert await tracker.is_superseded(_event("a")) is False


async def test_prs_are_tracked_independently() -> None:
    tracker = PrHeadTracker(FakeRedis())
    await tracker.record(_event("a", pr=7))
    await tracker.record(_event("b", pr=8))

    assert await tracker.is_superseded(_event("a", pr=7)) is False


async def test_redelivered_head_keeps_its_original_position() -> None:
    tracker = PrHeadTracker(FakeRedis())
    await tracker.record(_event("a"))
    await tracker.record(_event("b"))
    await tracker.record(_event("a"))

    assert await tracker.is_superseded(_event("a")) is True
    assert await tracker.is_superseded(_event("b")) is False


async def test_mention_requests_are_neither_recorded_nor_skipped() -> None:
    redis = FakeRedis()
    tracker = PrHeadTracker(redis)
    await tracker.record(_event("a"))
    await tracker.record(_event("b"))
    mention = _event("a", job_suffix="_c99")

    await tracker.record(mention)

    assert await tracker.is_superseded(mention) is False
    assert await tracker.is_superseded(_event("a")) is True


async def test_mention_on_newest_head_does_not_supersede_the_head_request() -> None:
    tracker = PrHeadTracker(FakeRedis())
    await tracker.record(_event("a"))
    await tracker.record(_event("b", job_suffix="_c1"))

    assert await tracker.is_superseded(_event("a")) is False


async def test_record_sets_expiry() -> None:
    redis = FakeRedis()
    tracker = PrHeadTracker(redis, ttl_seconds=60)
    await tracker.record(_event("a"))

    assert list(redis.ttls.values()) == [60]


async def test_redis_failure_on_lookup_does_not_skip() -> None:
    redis = FakeRedis()
    tracker = PrHeadTracker(redis)
    await tracker.record(_event("a"))
    await tracker.record(_event("b"))
    redis.fail_hgetall = True

    assert await tracker.is_superseded(_event("a")) is False


class _Message:
    def __init__(self, value: bytes) -> None:
        self.value = value


class _Source:
    def __init__(self, messages: list[bytes]) -> None:
        self._messages = messages
        self.commits = 0

    async def __aiter__(self) -> AsyncIterator[_Message]:
        for raw in self._messages:
            yield _Message(raw)

    async def commit(self) -> None:
        self.commits += 1


async def test_tracker_consumer_records_and_commits_and_survives_bad_payloads() -> None:
    redis = FakeRedis()
    tracker = PrHeadTracker(redis)
    source = _Source(
        [
            _event("a").model_dump_json(by_alias=True).encode(),
            b"not json",
            _event("b").model_dump_json(by_alias=True).encode(),
        ]
    )

    await PrHeadTrackerConsumer(source, tracker).run()

    assert source.commits == 3
    assert await tracker.is_superseded(_event("a")) is True


class _Pipeline:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, event: ReviewRequestedEvent) -> None:
        self.calls.append(event.review_job_id)
        raise AssertionError("must not run for a skipped event")


class _Dedup:
    def __init__(self) -> None:
        self.tried: list[str] = []

    async def try_start(self, review_job_id: str) -> bool:
        self.tried.append(review_job_id)
        return True

    async def mark_completed(self, review_job_id: str) -> None: ...

    async def mark_failed(self, review_job_id: str) -> None: ...


class _Producer:
    async def publish_completed(self, event: object) -> None:
        raise AssertionError("nothing should be published")

    async def publish_failed(self, event: object) -> None:
        raise AssertionError("nothing should be published")


class _AlwaysSuperseded:
    async def is_superseded(self, event: ReviewRequestedEvent) -> bool:
        return True


async def test_review_consumer_skips_superseded_without_dedup_or_pipeline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pipeline = _Pipeline()
    dedup = _Dedup()
    consumer = ReviewRequestConsumer(
        _Source([]),
        pipeline,  # type: ignore[arg-type]
        _Producer(),
        dedup,
        superseded=_AlwaysSuperseded(),
    )

    with caplog.at_level("INFO"):
        await consumer.handle(_event("a").model_dump_json(by_alias=True).encode())

    assert pipeline.calls == []
    assert dedup.tried == []
    assert "skipping superseded review reviewJobId=1:7:a" in caplog.text
