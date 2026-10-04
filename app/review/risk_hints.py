import re
from collections.abc import Iterable

from app.review.schema import ReviewTarget

_MAX_AREAS = 4

# 앞에 있을수록 우선순위가 높다 — 상한(4개)을 넘으면 뒤쪽이 빠진다.
_PATH_AREAS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "security and permission boundaries",
        re.compile(r"auth(?!or)|secur|crypt|secret|permission|token|password|oauth|jwt|acl"),
    ),
    (
        "data integrity and migrations",
        re.compile(r"migrat|schema|repositor|\.sql$|entity|dao"),
    ),
    (
        "concurrency and background processing",
        re.compile(r"queue|worker|job|cache|lock|consumer|scheduler|async"),
    ),
    (
        "API contracts",
        re.compile(r"controller|handler|router|openapi|swagger|\.proto$|endpoint"),
    ),
    (
        "deployment and supply chain",
        re.compile(
            r"\.github/workflows/|dockerfile|docker-compose|package\.json|"
            r"lock(file)?\.|\.lock$|build\.gradle|pom\.xml|pyproject\.toml|requirements.*\.txt"
        ),
    ),
)

_DELETION_AREA = "impact of deleted or renamed files"


def detect_risk_areas(targets: Iterable[ReviewTarget]) -> list[str]:
    targets = list(targets)
    paths = [t.file_path.lower() for t in targets]
    areas = [name for name, pattern in _PATH_AREAS if any(pattern.search(p) for p in paths)]
    if any(t.status in ("removed", "renamed") for t in targets):
        areas.append(_DELETION_AREA)
    return areas[:_MAX_AREAS]


def build_risk_hint(targets: Iterable[ReviewTarget]) -> str:
    areas = detect_risk_areas(targets)
    if not areas:
        return ""
    return (
        "Risk areas detected from the changed file paths: "
        + "; ".join(areas)
        + ". In this review, also check the failure conditions specific to these areas, "
        "but report a finding only if the diff itself shows it — a matching path alone "
        "is not evidence.\n\n"
    )
