import argparse
import json
from pathlib import Path

from app.core.config import get_settings
from app.evaluation.context_effect.schema import CaseLabels, Condition
from app.llm.client import ChatMessage
from app.review.pipeline import ReviewPipeline
from app.review.schema import ReviewModelOutput, VerificationResult
from scripts.evaluate_context_effect import (
    _parse_conditions,
    _report_command,
    load_dataset,
    run_cases,
    save_results,
)


class FakeLLM:
    """항상 같은 finding 하나를 반환하는 고정 fake — 조건별 프롬프트 차이와
    무관하게 결정적으로 동작해야 하는 end-to-end 테스트용이다."""

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1500,
        max_reviews: int | None = None,
    ) -> ReviewModelOutput:
        return ReviewModelOutput(
            summary="요약",
            reviews=[],
        )

    async def verify_findings(
        self, messages: list[ChatMessage], *, max_tokens: int = 800
    ) -> VerificationResult:
        return VerificationResult(verdicts=[])

    async def count_tokens(self, text: str) -> int:
        return 1

    async def get_context_window(self) -> int | None:
        return None


def _write_dataset(dataset_dir: Path) -> None:
    dataset_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "reviewJobId": "1:1:sha1",
        "repositoryId": 1,
        "prNumber": 1,
        "headSha": "sha1",
        "baseSha": "base1",
        "changedFiles": [
            {
                "filePath": "app/service.py",
                "status": "modified",
                "patch": "@@ -1,2 +1,2 @@\n-old\n+new",
                "content": "new content",
                "previousContent": "old content",
            }
        ],
    }
    labels = {
        "caseId": "case1",
        "category": "repo_specific",
        "expected": [
            {
                "id": "e1",
                "filePath": "app/service.py",
                "lineStart": 1,
                "lineEnd": 1,
                "severity": "major",
                "description": "설명",
            }
        ],
        "judgments": {},
    }
    (dataset_dir / "case1.event.json").write_text(
        json.dumps(event, ensure_ascii=False), encoding="utf-8"
    )
    (dataset_dir / "case1.labels.json").write_text(
        json.dumps(labels, ensure_ascii=False), encoding="utf-8"
    )


def _fake_pipelines() -> dict[Condition, ReviewPipeline]:
    conditions: list[Condition] = ["A", "B", "C"]
    return {
        condition: ReviewPipeline(FakeLLM(), model_version="fake", prompt_version="v1")
        for condition in conditions
    }


class TestParseConditions:
    def test_parses_comma_separated_list(self) -> None:
        assert _parse_conditions("A,B,C") == ["A", "B", "C"]

    def test_trims_whitespace_and_uppercases(self) -> None:
        assert _parse_conditions(" a , b ") == ["A", "B"]

    def test_rejects_unknown_condition(self) -> None:
        try:
            _parse_conditions("A,X")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for unknown condition")


class TestLoadDataset:
    def test_loads_event_and_label_pairs(self, tmp_path: Path) -> None:
        _write_dataset(tmp_path)

        cases = load_dataset(tmp_path)

        assert len(cases) == 1
        event, labels = cases[0]
        assert event.review_job_id == "1:1:sha1"
        assert labels.case_id == "case1"

    def test_fills_missing_repository_id_from_default(self, tmp_path: Path) -> None:
        dataset_dir = tmp_path
        dataset_dir.mkdir(exist_ok=True)
        event = {
            "reviewJobId": "1:1:sha1",
            "prNumber": 1,
            "headSha": "sha1",
            "baseSha": "base1",
        }
        labels = {"caseId": "case1", "category": "repo_specific"}
        (dataset_dir / "case1.event.json").write_text(json.dumps(event), encoding="utf-8")
        (dataset_dir / "case1.labels.json").write_text(json.dumps(labels), encoding="utf-8")

        cases = load_dataset(dataset_dir, default_repository_id=999)

        assert cases[0][0].repository_id == 999

    def test_event_repository_id_takes_priority_over_default(self, tmp_path: Path) -> None:
        _write_dataset(tmp_path)

        cases = load_dataset(tmp_path, default_repository_id=999)

        assert cases[0][0].repository_id == 1


class TestRunCasesAndReportEndToEnd:
    async def test_run_cases_produces_result_per_condition(self, tmp_path: Path) -> None:
        _write_dataset(tmp_path)
        cases = load_dataset(tmp_path)
        pipelines = _fake_pipelines()

        results = await run_cases(
            cases, pipelines, instrumentation={}, conditions=["A", "B", "C"]
        )

        assert len(results) == 3
        assert {r.condition for r in results} == {"A", "B", "C"}
        assert all(r.completed is not None for r in results)
        assert all(r.case_id == "case1" for r in results)

    async def test_run_and_report_end_to_end_creates_report_files(self, tmp_path: Path) -> None:
        dataset_dir = tmp_path / "dataset"
        out_dir = tmp_path / "out"
        _write_dataset(dataset_dir)

        cases = load_dataset(dataset_dir)
        pipelines = _fake_pipelines()
        results = await run_cases(
            cases, pipelines, instrumentation={}, conditions=["A", "B", "C"]
        )
        save_results(out_dir, results)

        _report_command(
            argparse.Namespace(dataset=dataset_dir, out=out_dir, json=False)
        )

        report_path = out_dir / "report.md"
        pending_path = out_dir / "pending_judgments.json"
        assert report_path.exists()
        assert pending_path.exists()
        assert "| A |" in report_path.read_text(encoding="utf-8")

        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        assert isinstance(pending, list)

    async def test_report_json_flag_skips_report_md(self, tmp_path: Path) -> None:
        dataset_dir = tmp_path / "dataset"
        out_dir = tmp_path / "out"
        _write_dataset(dataset_dir)

        cases = load_dataset(dataset_dir)
        pipelines = _fake_pipelines()
        results = await run_cases(
            cases, pipelines, instrumentation={}, conditions=["A", "B", "C"]
        )
        save_results(out_dir, results)

        _report_command(argparse.Namespace(dataset=dataset_dir, out=out_dir, json=True))

        assert not (out_dir / "report.md").exists()
        assert (out_dir / "pending_judgments.json").exists()


def test_case_labels_round_trips_through_camel_case() -> None:
    labels = CaseLabels(
        case_id="c1",
        category="dependency",
        expected=[],
        judgments={"a.py:1:t": "valid"},
    )
    data = labels.model_dump(by_alias=True)
    assert data["caseId"] == "c1"
    restored = CaseLabels.model_validate(data)
    assert restored == labels


def test_settings_still_loadable() -> None:
    # 스크립트가 get_settings()를 참조하므로, Settings 자체가 정상 로드되는지도
    # 함께 확인한다(다른 테스트가 이미 검증하지만 회귀를 조기에 잡기 위한 저비용 확인).
    settings = get_settings()
    assert settings.llm_model
