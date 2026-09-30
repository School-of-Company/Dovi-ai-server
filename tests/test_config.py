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


def test_sandbox_probe_settings_defaults_keep_the_feature_off() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.sandbox_probe_consumer_enabled is False
    assert settings.kafka_sandbox_probe_request_topic == "pr.sandbox.probe.requested"
    assert settings.kafka_sandbox_probe_completed_topic == "pr.sandbox.probe.completed"
    assert settings.sandbox_probe_concurrency == 4
    assert settings.sandbox_probe_job_timeout_seconds == 900
    assert settings.sandbox_probe_max_poll_interval_ms > 900_000
