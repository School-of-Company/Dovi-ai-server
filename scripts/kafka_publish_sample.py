"""
샘플 ReviewRequestedEvent를 실제 Kafka에 발행하는 스크립트.

사전조건: 로컬 Kafka 브로커 (docker compose up kafka)

사용법:
  uv run python -m scripts.kafka_publish_sample
  uv run python -m scripts.kafka_publish_sample --event path/to.json
"""

import argparse
import asyncio
import json
from pathlib import Path

from aiokafka import AIOKafkaProducer

from app.core.config import get_settings

_DEFAULT_EVENT_PATH = (
    Path(__file__).resolve().parent.parent / "sample_events" / "pr_review_requested.json"
)


async def _publish(event_path: Path) -> None:
    settings = get_settings()
    data = event_path.read_bytes()
    review_job_id = json.loads(data)["reviewJobId"]

    producer = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers)
    await producer.start()
    try:
        await producer.send_and_wait(
            settings.kafka_review_request_topic,
            value=data,
            key=review_job_id.encode("utf-8"),
        )
        print(
            f"발행 완료: topic={settings.kafka_review_request_topic} "
            f"reviewJobId={review_job_id}"
        )
    finally:
        await producer.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event", type=Path, default=_DEFAULT_EVENT_PATH, help="샘플 이벤트 JSON 경로"
    )
    args = parser.parse_args()
    asyncio.run(_publish(args.event))


if __name__ == "__main__":
    main()
