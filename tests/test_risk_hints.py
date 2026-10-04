from app.review.risk_hints import build_risk_hint, detect_risk_areas
from app.review.schema import ReviewTarget


def _t(path: str, status: str = "modified") -> ReviewTarget:
    return ReviewTarget(file_path=path, status=status, hunks=["@@ -1 +1 @@\n+x"])  # type: ignore[arg-type]


def test_no_risk_area_for_plain_files() -> None:
    assert detect_risk_areas([_t("src/utils/format.py"), _t("README.md")]) == []
    assert build_risk_hint([_t("src/utils/format.py")]) == ""


def test_detects_each_area_from_paths() -> None:
    cases = {
        "src/auth/JwtFilter.kt": "security and permission boundaries",
        "db/migrations/0002_add.sql": "data integrity and migrations",
        "app/worker/queue.py": "concurrency and background processing",
        "src/UserController.java": "API contracts",
        ".github/workflows/cd.yml": "deployment and supply chain",
    }
    for path, area in cases.items():
        assert detect_risk_areas([_t(path)]) == [area], path


def test_author_is_not_mistaken_for_auth() -> None:
    assert detect_risk_areas([_t("docs/authors.md")]) == []


def test_deleted_or_renamed_files_are_flagged() -> None:
    assert detect_risk_areas([_t("a.txt", "removed")]) == ["impact of deleted or renamed files"]
    assert detect_risk_areas([_t("a.txt", "renamed")]) == ["impact of deleted or renamed files"]


def test_areas_are_capped_at_four_in_priority_order() -> None:
    targets = [
        _t("auth/a.py"),
        _t("migrations/b.sql"),
        _t("worker/c.py"),
        _t("api/router.py"),
        _t("Dockerfile"),
        _t("gone.py", "removed"),
    ]

    assert detect_risk_areas(targets) == [
        "security and permission boundaries",
        "data integrity and migrations",
        "concurrency and background processing",
        "API contracts",
    ]


def test_hint_text_says_path_alone_is_not_evidence() -> None:
    hint = build_risk_hint([_t("auth/a.py")])

    assert hint.startswith("Risk areas detected from the changed file paths: security")
    assert "a matching path alone is not evidence" in hint
