"""
pr.review.completed / pr.review.failed 토픽을 소비해 출력하는 확인용 스크립트.

scripts.kafka_run_consumer가 발행한 결과를 눈으로 확인할 때 사용한다. Ctrl+C로 종료.

사전조건: 로컬 Kafka 브로커 (docker compose up kafka)

사용법:
  uv run python -m scripts.kafka_consume_results
"""

import asyncio
import json
import sys

from aiokafka import AIOKafkaConsumer

from app.core.config import get_settings


async def _consume() -> None:
    settings = get_settings()
    consumer = AIOKafkaConsumer(
        settings.kafka_review_completed_topic,
        settings.kafka_review_failed_topic,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id="dovi-result-viewer",
        auto_offset_reset="earliest",
    )
    await consumer.start()
    print(
        f"구독 중: {settings.kafka_review_completed_topic}, "
        f"{settings.kafka_review_failed_topic}"
    )
    print("Ctrl+C로 종료\n")

    try:
        async for message in consumer:
            payload = json.loads(message.value)
            print(f"[{message.topic}]")
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            print()
    finally:
        await consumer.stop()


def main() -> None:
    # nohup 등으로 파일에 리다이렉트할 때 블록 버퍼링 때문에 로그가 늦게(또는 안) 보이는
    # 문제를 막는다 — 오래 도는 데몬성 스크립트라 실시간 관찰이 중요하다.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

    try:
        asyncio.run(_consume())
    except KeyboardInterrupt:
        print("\n종료합니다.")


if __name__ == "__main__":
    main()
