"""RAG/릴리즈노트 근거가 리뷰 품질에 실제로 도움이 되는지 사람 라벨 평가셋으로
측정하는 오프라인 하네스 (이슈 #104).

app 패키지를 import하므로 반드시 -m으로 모듈 실행해야 한다 (레포 루트에서).

사용법:
  # 조건 A/B/C 전부 실행 (fake LLM, 네트워크 불필요 — 스모크 테스트용)
  uv run python -m scripts.evaluate_context_effect run \\
      --dataset eval-data/my-set --out eval-results/run1 --fake-llm

  # 실제 스테이징 LLM으로 조건 B/C만 실행
  uv run python -m scripts.evaluate_context_effect run \\
      --dataset eval-data/my-set --out eval-results/run1 \\
      --conditions B,C --repository-id 12345

  # 저장된 실행 결과와 라벨을 비교해 지표 리포트 생성
  uv run python -m scripts.evaluate_context_effect report \\
      --dataset eval-data/my-set --out eval-results/run1

  uv run python -m scripts.evaluate_context_effect report \\
      --dataset eval-data/my-set --out eval-results/run1 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from app.core.config import Settings, get_settings
from app.evaluation.context_effect.instrumentation import (
    CountingReleaseNotesCache,
    InMemoryRedis,
    PromptCapturingLLM,
    TimedOfficialDocsWorkflow,
    changed_npm_packages,
    strip_to_diff_only,
)
from app.evaluation.context_effect.metrics import (
    compute_metrics,
    pending_judgments,
    render_markdown,
)
from app.evaluation.context_effect.schema import CaseLabels, CaseRunResult, Condition
from app.llm.openai_compatible_client import OpenAICompatibleLLMClient
from app.review.pipeline import ContextRetriever, ReviewLLM, ReviewPipeline
from app.review.schema import ReviewCompletedEvent, ReviewFailedEvent, ReviewRequestedEvent
from scripts._fake_llm import FakeLLM

logger = logging.getLogger(__name__)

_ALL_CONDITIONS: list[Condition] = ["A", "B", "C"]


class _Closeable(Protocol):
    async def aclose(self) -> None: ...


@dataclass
class ConditionInstrumentation:
    """조건 하나를 실행하는 동안 evidence_in_prompt/latency/cache_hits 등을
    읽어올 수 있는 훅 — 실제 계측기는 조건별 파이프라인 조립 시 생성된다.
    """

    llm: PromptCapturingLLM | None = None
    official_docs: TimedOfficialDocsWorkflow | None = None
    cache: CountingReleaseNotesCache | None = None


async def run_cases(
    cases: list[tuple[ReviewRequestedEvent, CaseLabels]],
    pipelines: dict[Condition, ReviewPipeline],
    *,
    instrumentation: dict[Condition, ConditionInstrumentation],
    conditions: list[Condition],
) -> list[CaseRunResult]:
    """케이스마다 conditions에 있는 각 조건의 파이프라인을 순회 실행한다.

    조건 A는 항상 strip_to_diff_only()로 컨텍스트/파일 전체 내용을 제거한
    이벤트로 실행한다. pipelines/instrumentation에 없는 조건은 건너뛴다(부분
    조건만 준 호출자를 배려).
    """
    results: list[CaseRunResult] = []
    for event, labels in cases:
        changed_packages = changed_npm_packages(event)
        for condition in conditions:
            pipeline = pipelines.get(condition)
            if pipeline is None:
                continue
            inst = instrumentation.get(condition)

            if inst is not None and inst.llm is not None:
                inst.llm.evidence_seen = False
            if inst is not None and inst.official_docs is not None:
                inst.official_docs.last_latency_ms = None
            if inst is not None and inst.cache is not None:
                inst.cache.hits = 0
                inst.cache.misses = 0

            run_event = strip_to_diff_only(event) if condition == "A" else event
            outcome = await pipeline.run(run_event)

            evidence_in_prompt = inst.llm.evidence_seen if inst and inst.llm else False
            evidence_latency_ms = (
                inst.official_docs.last_latency_ms if inst and inst.official_docs else None
            )
            cache_hits = inst.cache.hits if inst and inst.cache else 0
            cache_misses = inst.cache.misses if inst and inst.cache else 0

            results.append(
                CaseRunResult(
                    case_id=labels.case_id,
                    condition=condition,
                    completed=outcome if isinstance(outcome, ReviewCompletedEvent) else None,
                    failed=outcome if isinstance(outcome, ReviewFailedEvent) else None,
                    evidence_in_prompt=evidence_in_prompt,
                    evidence_latency_ms=evidence_latency_ms,
                    cache_hits=cache_hits,
                    cache_misses=cache_misses,
                    changed_packages=changed_packages,
                )
            )
    return results


def _load_case_ids(dataset_dir: Path) -> list[str]:
    suffix = ".event.json"
    return sorted(p.name[: -len(suffix)] for p in dataset_dir.glob(f"*{suffix}"))


def load_dataset(
    dataset_dir: Path, *, default_repository_id: int | None = None
) -> list[tuple[ReviewRequestedEvent, CaseLabels]]:
    """<case_id>.event.json + <case_id>.labels.json 쌍들을 로드한다.

    default_repository_id가 주어지고 이벤트 JSON에 repositoryId가 없으면
    그 값을 채워 넣는다 — 이벤트에 이미 있으면 이벤트 값이 우선한다.
    """
    cases: list[tuple[ReviewRequestedEvent, CaseLabels]] = []
    for case_id in _load_case_ids(dataset_dir):
        event_path = dataset_dir / f"{case_id}.event.json"
        labels_path = dataset_dir / f"{case_id}.labels.json"
        if not labels_path.exists():
            raise FileNotFoundError(f"라벨 파일이 없습니다: {labels_path}")

        event_data = json.loads(event_path.read_text(encoding="utf-8"))
        if "repositoryId" not in event_data and default_repository_id is not None:
            event_data["repositoryId"] = default_repository_id
        event = ReviewRequestedEvent.model_validate(event_data)

        labels_data = json.loads(labels_path.read_text(encoding="utf-8"))
        labels = CaseLabels.model_validate(labels_data)
        cases.append((event, labels))
    return cases


def load_labels(dataset_dir: Path) -> dict[str, CaseLabels]:
    return {labels.case_id: labels for _event, labels in load_dataset(dataset_dir)}


def save_results(out_dir: Path, results: list[CaseRunResult]) -> None:
    for result in results:
        condition_dir = out_dir / result.condition
        condition_dir.mkdir(parents=True, exist_ok=True)
        path = condition_dir / f"{result.case_id}.json"
        path.write_text(
            json.dumps(result.model_dump(by_alias=True), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_results(out_dir: Path, conditions: list[Condition]) -> list[CaseRunResult]:
    results: list[CaseRunResult] = []
    for condition in conditions:
        condition_dir = out_dir / condition
        if not condition_dir.exists():
            continue
        for path in sorted(condition_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            results.append(CaseRunResult.model_validate(data))
    return results


def _save_meta(
    out_dir: Path, *, settings: Settings, conditions: list[Condition], use_fake_llm: bool
) -> None:
    meta = {
        "llmModel": "fake" if use_fake_llm else settings.llm_model,
        "llmBaseUrl": None if use_fake_llm else settings.llm_base_url,
        "llmMaxContext": settings.llm_max_context,
        "conditions": conditions,
        "ranAt": datetime.now(UTC).isoformat(),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _build_retriever(settings: Settings) -> ContextRetriever | None:
    if not settings.rag_enabled:
        logger.warning(
            "settings.rag_enabled=false — 조건 B/C를 프로젝트 컨텍스트(RAG) 없이 실행합니다"
        )
        return None

    # qdrant-client는 numpy를 끌어오므로, RAG를 안 쓰는 실행 경로에까지 그
    # 의존성 위험을 지우지 않도록 지연 import한다 (app/main.py와 동일한 패턴).
    from qdrant_client import QdrantClient

    from app.rag.embeddings import CodeRankEmbedClient
    from app.rag.reranker import CrossEncoderReranker
    from app.rag.retriever import ProjectContextRetriever
    from app.rag.vector_store import QdrantVectorStore

    embedder = CodeRankEmbedClient(settings.embedding_model)
    reranker = CrossEncoderReranker(settings.reranker_model)
    qdrant_client = QdrantClient(url=settings.qdrant_url)
    vector_store = QdrantVectorStore(
        qdrant_client, settings.rag_collection_name, vector_size=embedder.dimension
    )
    return ProjectContextRetriever(embedder, vector_store, reranker=reranker)


def _build_official_docs_workflow(
    settings: Settings,
) -> tuple[TimedOfficialDocsWorkflow, CountingReleaseNotesCache, list[_Closeable]]:
    from app.context.github_release_client import GithubReleaseClient
    from app.context.npm_registry_client import NpmRegistryClient
    from app.context.official_docs_workflow import OfficialDocsWorkflow

    npm_registry_client = NpmRegistryClient()
    github_release_client = GithubReleaseClient(token=settings.github_token)
    # 영속 Redis 없이도 같은 실행 안에서 케이스끼리 캐시를 공유할 수 있도록
    # InMemoryRedis를 쓴다 — 실행 간에는 캐시가 유지되지 않는다(의도된 단순화).
    from app.context.release_notes_cache import RedisReleaseNotesCache

    cache = CountingReleaseNotesCache(RedisReleaseNotesCache(InMemoryRedis()))
    workflow = OfficialDocsWorkflow(npm_registry_client, github_release_client, cache)
    return (
        TimedOfficialDocsWorkflow(workflow),
        cache,
        [npm_registry_client, github_release_client],
    )


_BuildPipelinesResult = tuple[
    dict[Condition, ReviewPipeline], dict[Condition, ConditionInstrumentation], list[_Closeable]
]


def build_pipelines(
    conditions: list[Condition], *, settings: Settings, llm: ReviewLLM
) -> _BuildPipelinesResult:
    """조건별 ReviewPipeline과 계측 훅을 조립한다.

    retriever/official_docs_workflow가 없어도(RAG 미설정 등) 파이프라인 자체는
    정상 동작한다 — 경고 로그만 남기고 해당 조건이 diff-only에 가깝게 실행된다.
    """
    retriever: ContextRetriever | None = None
    if "B" in conditions or "C" in conditions:
        retriever = _build_retriever(settings)

    pipelines: dict[Condition, ReviewPipeline] = {}
    instrumentation: dict[Condition, ConditionInstrumentation] = {}
    closeables: list[_Closeable] = []

    for condition in conditions:
        captured_llm = PromptCapturingLLM(llm)
        inst = ConditionInstrumentation(llm=captured_llm)

        official_docs_workflow = None
        if condition == "C":
            official_docs_workflow, cache, docs_closeables = _build_official_docs_workflow(
                settings
            )
            inst.official_docs = official_docs_workflow
            inst.cache = cache
            closeables.extend(docs_closeables)

        pipeline = ReviewPipeline(
            captured_llm,
            model_version=settings.llm_model,
            prompt_version="v1",
            llm_max_context=settings.llm_max_context,
            max_tokens=settings.llm_max_tokens,
            verify_max_tokens=settings.llm_verify_max_tokens,
            truncation_retry_max_findings=settings.llm_truncation_retry_max_findings,
            retriever=retriever if condition in ("B", "C") else None,
            official_docs_workflow=official_docs_workflow,
        )
        pipelines[condition] = pipeline
        instrumentation[condition] = inst

    return pipelines, instrumentation, closeables


def _parse_conditions(raw: str) -> list[Condition]:
    result: list[Condition] = []
    for token in raw.split(","):
        normalized = token.strip().upper()
        if normalized not in ("A", "B", "C"):
            raise ValueError(f"알 수 없는 조건: {token!r} (A/B/C만 허용)")
        result.append(cast(Condition, normalized))
    return result


async def _run_command(args: argparse.Namespace) -> None:
    settings = get_settings()
    conditions = _parse_conditions(args.conditions)
    dataset_dir: Path = args.dataset
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    cases = load_dataset(dataset_dir, default_repository_id=args.repository_id)

    real_client: OpenAICompatibleLLMClient | None = None
    llm: ReviewLLM
    if args.fake_llm:
        llm = FakeLLM()
    else:
        real_client = OpenAICompatibleLLMClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
        llm = real_client

    pipelines, instrumentation, closeables = build_pipelines(
        conditions, settings=settings, llm=llm
    )

    try:
        results = await run_cases(
            cases, pipelines, instrumentation=instrumentation, conditions=conditions
        )
    finally:
        if real_client is not None:
            await real_client.aclose()
        for closeable in closeables:
            await closeable.aclose()

    save_results(out_dir, results)
    _save_meta(out_dir, settings=settings, conditions=conditions, use_fake_llm=args.fake_llm)
    print(f"{len(results)}건의 실행 결과를 {out_dir}에 저장했습니다.")


def _report_command(args: argparse.Namespace) -> None:
    dataset_dir: Path = args.dataset
    out_dir: Path = args.out

    labels_by_case = load_labels(dataset_dir)
    results = load_results(out_dir, _ALL_CONDITIONS)
    metrics = compute_metrics(results, labels_by_case)

    pending = pending_judgments(results, labels_by_case)
    (out_dir / "pending_judgments.json").write_text(
        json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if args.json:
        payload = {
            condition: condition_metrics.model_dump(by_alias=True)
            for condition, condition_metrics in metrics.items()
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    report_text = render_markdown(metrics)
    (out_dir / "report.md").write_text(report_text, encoding="utf-8")
    print(report_text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="조건 A/B/C 파이프라인 실행")
    run_parser.add_argument("--dataset", type=Path, required=True, help="평가셋 디렉터리")
    run_parser.add_argument("--out", type=Path, required=True, help="실행 결과 저장 디렉터리")
    run_parser.add_argument(
        "--conditions", default="A,B,C", help="콤마 구분 조건 목록 (기본값: A,B,C)"
    )
    run_parser.add_argument(
        "--fake-llm", action="store_true", help="네트워크 불필요한 fake LLM 사용(스모크 테스트)"
    )
    run_parser.add_argument(
        "--repository-id",
        type=int,
        default=None,
        help="이벤트에 repositoryId가 없을 때 쓸 기본 repository_id",
    )

    report_parser = subparsers.add_parser("report", help="저장된 실행 결과로 지표 리포트 생성")
    report_parser.add_argument(
        "--dataset", type=Path, required=True, help="라벨을 로드할 평가셋 디렉터리"
    )
    report_parser.add_argument(
        "--out", type=Path, required=True, help="run 결과가 저장된 디렉터리"
    )
    report_parser.add_argument(
        "--json", action="store_true", help="report.md 대신 JSON을 stdout에 출력"
    )

    args = parser.parse_args()

    if args.command == "run":
        if not args.dataset.exists():
            parser.error(f"평가셋 디렉터리를 찾을 수 없습니다: {args.dataset}")
        asyncio.run(_run_command(args))
    else:
        _report_command(args)


if __name__ == "__main__":
    main()
