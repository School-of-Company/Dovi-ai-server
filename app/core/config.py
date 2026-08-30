from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Dovi AI Server"
    debug: bool = False

    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_review_request_topic: str = "pr.review.requested"
    kafka_review_completed_topic: str = "pr.review.completed"
    kafka_review_failed_topic: str = "pr.review.failed"
    # 기본 False: 테스트/CI에서 TestClient가 앱을 기동해도 실제 Kafka/LLM에
    # 연결을 시도하지 않는다. 운영 배포 시 .env에서 명시적으로 true로 켠다.
    kafka_consumer_enabled: bool = False

    llm_profile: str = "dual_gpu_32gb"
    llm_base_url: str = "http://localhost:8001/v1"
    llm_model: str = "qwen2.5-coder-32b-instruct-q4_k_m.gguf"
    llm_max_context: int = 8192
    llm_gpu_layers: int = -1
    llm_timeout_seconds: float = 120.0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
