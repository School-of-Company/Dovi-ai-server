import pytest

from tests.test_main import _captured_pipeline_kwargs


async def test_pipeline_has_no_timing_sink_without_the_evaluation_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _captured_pipeline_kwargs(monkeypatch)

    assert captured["timing_sink"] is None
