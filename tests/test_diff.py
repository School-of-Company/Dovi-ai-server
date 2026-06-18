from app.review.diff import analyze
from app.review.schema import ChangedFile, ReviewRequestedEvent

_PATCH = "@@ -1,2 +1,3 @@\n line1\n+added\n line2"


def _event(*files: ChangedFile) -> ReviewRequestedEvent:
    return ReviewRequestedEvent(
        review_job_id="1:2:sha",
        repository_id=1,
        pr_number=2,
        head_sha="sha",
        base_sha="base",
        changed_files=list(files),
    )


def test_keeps_normal_file() -> None:
    event = _event(ChangedFile(file_path="app/main.py", status="modified", patch=_PATCH))
    targets = analyze(event)
    assert len(targets) == 1
    assert targets[0].file_path == "app/main.py"


def test_skips_lockfile() -> None:
    event = _event(ChangedFile(file_path="uv.lock", status="modified", patch=_PATCH))
    assert analyze(event) == []


def test_skips_binary_extension() -> None:
    event = _event(ChangedFile(file_path="assets/logo.png", status="added", patch=_PATCH))
    assert analyze(event) == []


def test_skips_empty_patch() -> None:
    event = _event(ChangedFile(file_path="app/x.py", status="modified", patch=""))
    assert analyze(event) == []


def test_skips_removed_file() -> None:
    event = _event(ChangedFile(file_path="app/old.py", status="removed", patch=_PATCH))
    assert analyze(event) == []


def test_skips_generated_dir() -> None:
    event = _event(
        ChangedFile(file_path="frontend/node_modules/x/y.js", status="added", patch=_PATCH)
    )
    assert analyze(event) == []


def test_skips_generated_suffix() -> None:
    event = _event(ChangedFile(file_path="static/app.min.js", status="modified", patch=_PATCH))
    assert analyze(event) == []


def test_splits_multiple_hunks() -> None:
    patch = "@@ -1 +1 @@\n-a\n+b\n@@ -10 +10 @@\n-c\n+d"
    event = _event(ChangedFile(file_path="app/main.py", status="modified", patch=patch))
    targets = analyze(event)
    assert len(targets[0].hunks) == 2
    assert targets[0].hunks[0].startswith("@@ -1 +1 @@")
    assert targets[0].hunks[1].startswith("@@ -10 +10 @@")


def test_filters_mixed_changeset() -> None:
    event = _event(
        ChangedFile(file_path="app/main.py", status="modified", patch=_PATCH),
        ChangedFile(file_path="uv.lock", status="modified", patch=_PATCH),
        ChangedFile(file_path="logo.png", status="added", patch=_PATCH),
        ChangedFile(file_path="app/util.py", status="added", patch=_PATCH),
    )
    targets = analyze(event)
    assert [t.file_path for t in targets] == ["app/main.py", "app/util.py"]


def test_skips_binary_extension_uppercase() -> None:
    event = _event(ChangedFile(file_path="assets/logo.PNG", status="added", patch=_PATCH))
    assert analyze(event) == []


def test_keeps_false_positive_directories() -> None:
    event = _event(
        ChangedFile(file_path="distributor/main.py", status="modified", patch=_PATCH),
        ChangedFile(file_path="vendor_api/main.py", status="modified", patch=_PATCH),
    )
    targets = analyze(event)
    assert len(targets) == 2
