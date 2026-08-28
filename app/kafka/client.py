from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from app.core.config import Settings

_CONSUMER_GROUP_ID = "dovi-ai-review-engine"


def create_producer(settings: Settings) -> AIOKafkaProducer:
    return AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers)


def create_consumer(settings: Settings) -> AIOKafkaConsumer:
    return AIOKafkaConsumer(
        settings.kafka_review_request_topic,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=_CONSUMER_GROUP_ID,
        enable_auto_commit=False,
        # 리뷰 요청은 워크큐 성격 — 재배포로 그룹이 끊겼다 재개돼도 밀린 요청을
        # 건너뛰지 않고 전부 처리해야 한다.
        auto_offset_reset="earliest",
    )
