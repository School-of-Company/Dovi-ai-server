from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Dovi AI Server"
    debug: bool = False
    log_level: str = "INFO"

    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_review_request_topic: str = "pr.review.requested"
    kafka_review_completed_topic: str = "pr.review.completed"
    kafka_review_failed_topic: str = "pr.review.failed"
    kafka_comment_answer_request_topic: str = "pr.comment.answer.requested"
    kafka_comment_answer_completed_topic: str = "pr.comment.answer.completed"
    kafka_comment_answer_failed_topic: str = "pr.comment.answer.failed"
    # 기본 False: 테스트/CI에서 TestClient가 앱을 기동해도 실제 Kafka/LLM에
    # 연결을 시도하지 않는다. 운영 배포 시 .env에서 명시적으로 true로 켠다.
    kafka_consumer_enabled: bool = False
    # 배포로 종료 신호를 받았을 때, 처리 중인 리뷰를 강제 취소하기 전에 기다려주는
    # 최대 시간. llm_timeout_seconds보다 넉넉해야 정상 완료를 강제 취소로 놓치지 않는다.
    graceful_shutdown_seconds: float = 130.0

    llm_profile: str = "dual_gpu_32gb"
    llm_base_url: str = "http://localhost:8001/v1"
    llm_model: str = "qwen2.5-coder-32b-instruct-q4_k_m.gguf"
    llm_max_context: int = 8192
    llm_gpu_layers: int = -1
    llm_timeout_seconds: float = 120.0

    redis_url: str = "redis://localhost:6379"
    # headSha는 불변이므로 TTL을 길게 잡아도 무방하다 (기본 24시간)
    review_dedup_ttl_seconds: int = 86400
    # 코멘트 Q&A는 리뷰보다 훨씬 가벼운 단발성 작업이라 TTL을 짧게 잡는다 (기본 1시간)
    comment_answer_dedup_ttl_seconds: int = 3600

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
