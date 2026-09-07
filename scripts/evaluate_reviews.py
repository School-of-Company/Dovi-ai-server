"""review_jobs/review_records/review_feedback을 집계해 리뷰 품질 지표를 낸다.

사용법:
    uv run python -m scripts.evaluate_reviews
    uv run python -m scripts.evaluate_reviews --json
"""

import argparse
import asyncio
import json
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.evaluation.models import ReviewFeedbackRow, ReviewRecordRow

_Row = tuple[str, bool, str, str]  # (severity, reflected, model_version, prompt_version)


async def _fetch_rows(session: AsyncSession) -> list[_Row]:
    result = await session.execute(
        select(
            ReviewRecordRow.reviews,
            ReviewFeedbackRow.finding_index,
            ReviewFeedbackRow.reflected,
            ReviewRecordRow.model_version,
            ReviewRecordRow.prompt_version,
        ).join(
            ReviewFeedbackRow,
            ReviewFeedbackRow.review_job_id == ReviewRecordRow.review_job_id,
        )
    )
    rows: list[_Row] = []
    for reviews, finding_index, reflected, model_version, prompt_version in result.all():
        if not (0 <= finding_index < len(reviews)):
            continue
        severity = reviews[finding_index]["severity"]
        rows.append((severity, reflected, model_version, prompt_version))
    return rows


async def _count_total_findings(session: AsyncSession) -> int:
    """피드백이 아직 없는 finding까지 포함한 전체 finding 수.

    _fetch_rows()는 INNER JOIN이라 피드백이 붙은 finding만 보인다 — 반영률이
    "전체 중 얼마나 평가됐는지"와 무관하게 높아 보이는 것을 막으려면 분모가 필요하다.
    """
    result = await session.execute(select(ReviewRecordRow.reviews))
    return sum(len(reviews) for (reviews,) in result.all())


def _acceptance_rate(flags: Sequence[bool]) -> float | None:
    if not flags:
        return None
    return sum(1 for flag in flags if flag) / len(flags)


def _by_severity(rows: list[_Row]) -> dict[str, float | None]:
    grouped: dict[str, list[bool]] = defaultdict(list)
    for severity, reflected, _, _ in rows:
        grouped[severity].append(reflected)
    return {severity: _acceptance_rate(flags) for severity, flags in grouped.items()}


def _by_version(rows: list[_Row]) -> dict[str, float | None]:
    grouped: dict[str, list[bool]] = defaultdict(list)
    for _, reflected, model_version, prompt_version in rows:
        grouped[f"{model_version}::{prompt_version}"].append(reflected)
    return {key: _acceptance_rate(flags) for key, flags in grouped.items()}


async def build_report(session: AsyncSession) -> dict[str, object]:
    rows = await _fetch_rows(session)
    total_finding_count = await _count_total_findings(session)
    evaluated_count = len(rows)
    return {
        "total_feedback_count": evaluated_count,
        "total_finding_count": total_finding_count,
        "evaluated_ratio": (
            (evaluated_count / total_finding_count) if total_finding_count else None
        ),
        "overall_acceptance_rate": _acceptance_rate([reflected for _, reflected, _, _ in rows]),
        "by_severity": _by_severity(rows),
        "by_model_prompt_version": _by_version(rows),
    }


async def collect_report() -> dict[str, object]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        report = await build_report(session)
    await engine.dispose()
    return report


def _format_rate(rate: float | None) -> str:
    return f"{rate:.1%}" if rate is not None else "N/A"


def _print_report(report: dict[str, object]) -> None:
    print(f"전체 feedback 수: {report['total_feedback_count']}")
    print(f"전체 finding 수: {report['total_finding_count']}")
    print(
        f"평가된 finding 비율: {_format_rate(report['evaluated_ratio'])}"  # type: ignore[arg-type]
        f" ({report['total_feedback_count']}/{report['total_finding_count']})"
    )
    print(f"전체 acceptance rate: {_format_rate(report['overall_acceptance_rate'])}")  # type: ignore[arg-type]
    print()
    print("severity별 반영률:")
    for severity, rate in report["by_severity"].items():  # type: ignore[attr-defined]
        print(f"  {severity.ljust(12)} {_format_rate(rate)}")
    print()
    print("모델/프롬프트 버전별 반영률:")
    for key, rate in report["by_model_prompt_version"].items():  # type: ignore[attr-defined]
        print(f"  {key.ljust(40)} {_format_rate(rate)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="리뷰 평가 지표 집계")
    parser.add_argument("--json", action="store_true", help="JSON으로 출력")
    args = parser.parse_args()

    report = asyncio.run(collect_report())

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)


if __name__ == "__main__":
    main()
