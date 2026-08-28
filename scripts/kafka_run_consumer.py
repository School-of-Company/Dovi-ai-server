"""
실제 Kafka를 통해 리뷰 파이프라인을 구동하는 스크립트.

pr.review.requested를 소비해 파이프라인을 실행하고, 결과를 completed/failed
토픽에 발행한다. Ctrl+C로 종료.

사전조건: 로컬 Kafka 브로커 (docker compose up kafka)

사용법:
  uv run python -m scripts.kafka_run_consumer          # fake LLM (네트워크 불필요)
  uv run python -m scripts.kafka_run_consumer --real    # 실제 llama-server 호출
"""

import argparse
import asyncio

from app.core.config import get_settings
from app.kafka.client import create_consumer, create_producer
from app.kafka.consumer import ReviewRequestConsumer
from app.kafka.producer import ReviewEventProducer
from app.llm.client import LLMClient
from app.llm.openai_compatible_client import OpenAICompatibleLLMClient
from app.review.pipeline import ReviewPipeline
from scripts._fake_llm import FakeLLM


async def _run(use_real: bool) -> None:
    settings = get_settings()

    llm: LLMClient
    real_client: OpenAICompatibleLLMClient | None = None
    if use_real:
        real_client = OpenAICompatibleLLMClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
        llm = real_client
        model_version = settings.llm_model
    else:
        llm = FakeLLM()
        model_version = "fake"

    pipeline = ReviewPipeline(llm, model_version=model_version, prompt_version="v1")

    kafka_producer = create_producer(settings)
    kafka_consumer = create_consumer(settings)
    await kafka_producer.start()
    await kafka_consumer.start()

    event_producer = ReviewEventProducer(
        kafka_producer,
        completed_topic=settings.kafka_review_completed_topic,
        failed_topic=settings.kafka_review_failed_topic,
    )
    review_consumer = ReviewRequestConsumer(kafka_consumer, pipeline, event_producer)

    print(f"컨슈머 시작: topic={settings.kafka_review_request_topic}")
    print(f"LLM: {'실제 (' + settings.llm_base_url + ')' if use_real else 'fake'}")
    print("Ctrl+C로 종료\n")

    try:
        await review_consumer.run()
    finally:
        await kafka_consumer.stop()
        await kafka_producer.stop()
        if real_client is not None:
            await real_client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="fake 대신 실제 LLM 서버 호출")
    args = parser.parse_args()

    try:
        asyncio.run(_run(args.real))
    except KeyboardInterrupt:
        print("\n종료합니다.")


if __name__ == "__main__":
    main()
