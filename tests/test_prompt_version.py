import re

import pytest

from app.review import pipeline
from app.review.pipeline import compute_prompt_version


def test_version_is_short_hash() -> None:
    assert re.fullmatch(r"sha-[0-9a-f]{8}", compute_prompt_version())


def test_version_is_stable() -> None:
    assert compute_prompt_version() == compute_prompt_version()


def test_line_number_note_changes_version() -> None:
    assert compute_prompt_version(annotate_diff_lines=True) != compute_prompt_version()


def test_each_prompt_changes_version(monkeypatch: pytest.MonkeyPatch) -> None:
    base = compute_prompt_version()
    for name in ("_SYSTEM_PROMPT", "_VERIFY_SYSTEM_PROMPT", "_SUMMARY_REDUCE_PROMPT"):
        monkeypatch.setattr(pipeline, name, getattr(pipeline, name) + " x")
        assert compute_prompt_version() != base
        monkeypatch.undo()
