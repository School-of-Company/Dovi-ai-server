from app.core.config import Settings


def test_evaluation_settings_defaults() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.evaluation_enabled is False
    assert settings.database_url == "postgresql+asyncpg://dovi:dovi@localhost:5432/dovi"
    assert settings.kafka_review_feedback_topic == "pr.comment.reflected"


def test_consumer_flags_default_to_backward_compatible_values() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.review_consumer_enabled is True
    assert settings.comment_answer_consumer_enabled is True
