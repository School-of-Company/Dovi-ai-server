from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Dovi AI Server"
    debug: bool = False

    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_review_request_topic: str = "pr.review.requested"
    kafka_review_completed_topic: str = "pr.review.completed"
    kafka_review_failed_topic: str = "pr.review.failed"

    llm_profile: str = "single_gpu_16gb"
    llm_base_url: str = "http://localhost:8001/v1"
    llm_model: str = "qwen2.5-coder-14b-instruct-q5_k_m.gguf"
    llm_max_context: int = 8192
    llm_gpu_layers: int = -1

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
