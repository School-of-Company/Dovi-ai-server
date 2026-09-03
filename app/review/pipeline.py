import asyncio
import functools
import logging
from typing import Protocol

from pydantic import ValidationError

from app.llm.client import ChatMessage, LLMClient
from app.rag.schema import ChunkSearchResult
from app.review.context import build_context
from app.review.diff import analyze
from app.review.result_filter import filter_reviews, summarize_minor
from app.review.schema import (
    FailureReason,
    ReviewComment,
    ReviewCompletedEvent,
    ReviewFailedEvent,
    ReviewRequestedEvent,
    ReviewTarget,
    VerificationResult,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a code review assistant. Review the diff and report only real, "
    "concrete issues — runtime errors, security/auth problems, API contract "
    "breaks, data-consistency bugs, async/concurrency issues, dependency "
    "compatibility. Do not nitpick prose wording, phrasing, or documentation "
    "style in non-code files (e.g. markdown logs, changelogs) — those are "
    "not code review findings. In languages with structural/duck typing "
    "(e.g. Python's `Protocol`, TypeScript structural interfaces), a type "
    "swap is NOT a compatibility break as long as the method signatures "
    "still match — do not flag it as one. Do not flag whether a package/"
    "dependency is installed, an import resolves, or the code compiles/"
    "type-checks — CI's build and type-check steps already verify this "
    "mechanically on every push; a diff-only review cannot check it "
    "reliably and guessing about it only adds noise. Before reporting a "
    "finding about a removed ('-') line, check whether the same hunk's "
    "added ('+') lines already fix or address it — if they do, the finding "
    "is stale and must not be reported.\n\n"
    "Write `summary`, `title`, `message`, and `suggestedFix` in Korean. "
    "`summary` is posted as the PR's main review comment, so it must be 1-3 "
    "concrete sentences describing what the diff actually does and your "
    "overall assessment — never a bare label like '코드 리뷰 결과' or "
    "'리뷰 완료' with no content. If `reviews` is empty, `summary` must say "
    "so explicitly (e.g. '특이사항이 발견되지 않았습니다'), not just restate "
    "the diff's file names. `title` must be short (roughly under 40 "
    "characters) and name the exact problem, not a generic phrase like "
    "'개선이 필요합니다'. `message` must be 1-3 concise sentences stating "
    "what breaks and why — not a general description of what the file "
    "contains. `suggestedFix` must be plain prose describing the fix — "
    "never wrap it in a ```suggestion or any other markdown code fence; "
    "that syntax is for a literal drop-in code replacement, not an "
    "explanation.\n\n"
    "For every item in `reviews`, `evidence` must contain at least one string "
    "quoting the exact diff line(s) that support the finding, verbatim in "
    "the diff's original language (never translate evidence). Findings with "
    "empty `evidence` are discarded before reaching the user, so never leave "
    "it empty.\n\n"
    "Some files include a '전체 함수/클래스 컨텍스트' section showing the full "
    "source of the function or class a change belongs to, and/or a "
    "'관련 프로젝트 코드' section showing similar or related code found "
    "elsewhere in the project via search, in addition to the diff hunk. Use "
    "both only to understand surrounding code and existing conventions "
    "(signatures, control flow, naming) — `evidence` must still quote from "
    "the diff hunk, not from either of these extra sections."
)

_VERIFY_SYSTEM_PROMPT = (
    "You previously reviewed a PR diff and produced the numbered code review "
    "findings below. Verify each one skeptically against the same diff — do "
    "not just restate a finding as true.\n\n"
    "Be especially skeptical of: claims that a structural/duck-typed type "
    "swap (e.g. Python's `Protocol`, TypeScript structural interfaces) "
    "breaks compatibility when method signatures still match; algorithmic-"
    "complexity claims where the suggested alternative has the same "
    "complexity as the original; treating an intentionally broad exception "
    "handler (used for a documented fallback) as a bug; flagging code that "
    "already has an equivalent safety check nearby; a finding on a removed "
    "('-') line whose suggested fix is already present in the same hunk's "
    "added ('+') lines; and suggestions that would themselves violate this "
    "project's conventions (e.g. logging full request payloads).\n\n"
    "For every numbered finding, set `confirmed` to true only if the "
    "described problem is real and `evidence` actually supports it. Give a "
    "one-sentence `reason` either way, and set `index` to the finding's "
    "number."
)


class VerifyingLLM(Protocol):
    async def verify_findings(
        self, messages: list[ChatMessage], *, max_tokens: int = 800
    ) -> VerificationResult:
        """이전에 생성한 finding들을 diff/컨텍스트에 비추어 다시 검증한다.

        Raises:
            TimeoutError: LLM API 호출 타임아웃
            ValueError: 응답 파싱 또는 검증 실패
        """
        ...


class ReviewLLM(LLMClient, VerifyingLLM, Protocol):
    """ReviewPipeline이 필요로 하는 전체 인터페이스 (생성 + 자체 검증)."""


class ContextRetriever(Protocol):
    def retrieve(
        self,
        query_text: str,
        repository_id: int,
        exclude_file_path: str | None = None,
    ) -> list[ChunkSearchResult]:
        """query_text와 관련된 프로젝트 기존 코드를 repository_id 범위 안에서 찾는다.

        실패 시 빈 리스트를 반환한다.
        """
        ...


class ReviewPipeline:
    def __init__(
        self,
        llm: ReviewLLM,
        *,
        model_version: str,
        prompt_version: str,
        max_tokens: int = 1500,
        verify_max_tokens: int = 800,
        retriever: ContextRetriever | None = None,
    ) -> None:
        self._llm = llm
        self._model_version = model_version
        self._prompt_version = prompt_version
        self._max_tokens = max_tokens
        self._verify_max_tokens = verify_max_tokens
        self._retriever = retriever

    async def run(
        self, event: ReviewRequestedEvent
    ) -> ReviewCompletedEvent | ReviewFailedEvent:
        targets = analyze(event)
        if not targets:
            return self._completed(event, "No reviewable changes found.", [])

        related_context = await self._retrieve_related_context(event.repository_id, targets)
        messages = self._build_messages(event, targets, related_context)

        # parse_error/server_error는 1회 재시도 후 실패 처리. timeout은 즉시 실패
        # (재시도가 SLA를 더 악화시키므로 재시도하지 않는다).
        last_reason: FailureReason = "server_error"
        for _ in range(2):
            try:
                output = await self._llm.generate(messages, max_tokens=self._max_tokens)
            except TimeoutError:
                logger.warning(
                    "LLM timeout reviewJobId=%s", event.review_job_id
                )
                return self._failed(event, "timeout")
            except (ValueError, ValidationError):
                logger.warning(
                    "LLM output parse_error reviewJobId=%s", event.review_job_id
                )
                last_reason = "parse_error"
                continue
            except Exception:
                logger.exception(
                    "unexpected error during LLM generation reviewJobId=%s",
                    event.review_job_id,
                )
                last_reason = "server_error"
                continue

            reviews = filter_reviews(output.reviews)
            if reviews:
                reviews = await self._verify(event, messages, reviews)
            summary = self._build_summary(output.summary, output.reviews)
            logger.info(
                "review completed reviewJobId=%s reviewCount=%d",
                event.review_job_id,
                len(reviews),
            )
            return self._completed(event, summary, reviews)

        logger.warning(
            "review failed reviewJobId=%s reason=%s", event.review_job_id, last_reason
        )
        return self._failed(event, last_reason)

    def _completed(
        self,
        event: ReviewRequestedEvent,
        summary: str,
        reviews: list[ReviewComment],
    ) -> ReviewCompletedEvent:
        return ReviewCompletedEvent(
            review_job_id=event.review_job_id,
            repository_id=event.repository_id,
            pr_number=event.pr_number,
            head_sha=event.head_sha,
            summary=summary,
            reviews=reviews,
            model_version=self._model_version,
            prompt_version=self._prompt_version,
        )

    def _build_summary(self, summary: str, reviews: list[ReviewComment]) -> str:
        # 프롬프트에 summary 작성 지침을 넣어도 모델이 빈 문자열이나 공백만
        # 반환하는 경우가 있다 — PR의 메인 코멘트가 사실상 텅 비어 보이는
        # 상황을 막기 위해 코드 레벨로도 한 번 더 방어한다.
        if not summary.strip():
            summary = "요약 생성에 실패했습니다 (모델 출력이 비어 있음)."

        # minor/suggestion은 inline comment로 달지 않는 대신, 요약에 한 줄씩 남긴다
        # (노션 20절 "Minor/Suggestion은 summary로만 제공").
        notes = summarize_minor(reviews)
        if not notes:
            return summary
        bullet_list = "\n".join(f"- {title}" for title in notes)
        return f"{summary}\n\n참고(경미한 항목):\n{bullet_list}"

    async def _verify(
        self,
        event: ReviewRequestedEvent,
        messages: list[ChatMessage],
        reviews: list[ReviewComment],
    ) -> list[ReviewComment]:
        """critical/major finding들을 diff에 비추어 다시 검증해, 확인된 것만 남긴다.

        검증 호출 자체가 실패하면 원본을 그대로 노출하는 대신 보수적으로 이번
        배치를 전부 폐기한다 (노션 "리뷰 결과 자체 검증" 문서 참고).
        """
        verify_messages = self._build_verify_messages(messages, reviews)
        try:
            result = await self._llm.verify_findings(
                verify_messages, max_tokens=self._verify_max_tokens
            )
        except Exception:
            logger.exception(
                "verification LLM call failed reviewJobId=%s, discarding "
                "findings defensively",
                event.review_job_id,
            )
            return []

        verdict_by_index = {v.index: v for v in result.verdicts}
        confirmed: list[ReviewComment] = []
        disputed = 0
        for i, review in enumerate(reviews):
            verdict = verdict_by_index.get(i)
            if verdict is not None and verdict.confirmed:
                confirmed.append(review)
                continue
            disputed += 1
            logger.info(
                "finding disputed reviewJobId=%s file=%s title=%s reason=%s",
                event.review_job_id,
                review.file_path,
                review.title,
                verdict.reason if verdict is not None else "no verdict returned",
            )

        if disputed:
            logger.info(
                "verification reviewJobId=%s confirmed=%d disputed=%d",
                event.review_job_id,
                len(confirmed),
                disputed,
            )
        return confirmed

    def _build_verify_messages(
        self, original_messages: list[ChatMessage], reviews: list[ReviewComment]
    ) -> list[ChatMessage]:
        diff_and_context = original_messages[1]["content"]
        findings = "\n\n".join(
            self._render_finding(i, review) for i, review in enumerate(reviews)
        )
        user = f"{diff_and_context}\n\n## Findings to verify\n{findings}"
        return [
            {"role": "system", "content": _VERIFY_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    def _render_finding(self, index: int, review: ReviewComment) -> str:
        evidence = "; ".join(review.evidence)
        block = (
            f"{index}. [{review.severity}] {review.title}\n"
            f"{review.message}\n"
            f"evidence: {evidence}"
        )
        if review.suggested_fix:
            block += f"\nsuggestedFix: {review.suggested_fix}"
        return block

    def _failed(
        self, event: ReviewRequestedEvent, reason: FailureReason
    ) -> ReviewFailedEvent:
        return ReviewFailedEvent(
            review_job_id=event.review_job_id,
            head_sha=event.head_sha,
            reason=reason,
        )

    async def _retrieve_related_context(
        self, repository_id: int, targets: list[ReviewTarget]
    ) -> dict[str, list[ChunkSearchResult]]:
        """target별로 관련 프로젝트 코드를 repository_id 범위 안에서 검색한다.

        retriever가 없으면(RAG 미활성화) 즉시 빈 dict를 반환한다. 임베딩/검색은
        CPU 바운드 작업이라 이벤트 루프를 막지 않도록 executor에서 돌린다.
        """
        if self._retriever is None:
            return {}

        loop = asyncio.get_running_loop()
        related: dict[str, list[ChunkSearchResult]] = {}
        for target in targets:
            query = "\n".join(target.hunks)
            call = functools.partial(
                self._retriever.retrieve,
                query,
                repository_id,
                exclude_file_path=target.file_path,
            )
            related[target.file_path] = await loop.run_in_executor(None, call)
        return related

    def _build_messages(
        self,
        event: ReviewRequestedEvent,
        targets: list[ReviewTarget],
        related_context: dict[str, list[ChunkSearchResult]],
    ) -> list[ChatMessage]:
        diff = "\n\n".join(
            self._render_target(t, related_context.get(t.file_path, [])) for t in targets
        )
        context = build_context(event.context_files)
        user = f"## Project Context\n{context}\n\n## Changes\n{diff}" if context else diff
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    def _render_target(
        self, target: ReviewTarget, related: list[ChunkSearchResult]
    ) -> str:
        block = f"# {target.file_path} ({target.status})\n" + "\n".join(target.hunks)
        if target.context_chunks:
            context_section = "\n\n".join(target.context_chunks)
            block += f"\n\n#### 전체 함수/클래스 컨텍스트\n{context_section}"
        if related:
            related_section = "\n\n".join(
                f"# {r.file_path} :: {r.name or r.node_type}\n{r.source}" for r in related
            )
            block += f"\n\n#### 관련 프로젝트 코드\n{related_section}"
        return block
