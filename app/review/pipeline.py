import logging

from pydantic import ValidationError

from app.llm.client import ChatMessage, LLMClient
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
    "still match — do not flag it as one.\n\n"
    "Write `title`, `message`, and `suggestedFix` in Korean. `title` must be "
    "short (roughly under 40 characters) and name the exact problem, not a "
    "generic phrase like '개선이 필요합니다'. `message` must be 1-3 concise "
    "sentences stating what breaks and why — not a general description of "
    "what the file contains.\n\n"
    "For every item in `reviews`, `evidence` must contain at least one string "
    "quoting the exact diff line(s) that support the finding, verbatim in "
    "the diff's original language (never translate evidence). Findings with "
    "empty `evidence` are discarded before reaching the user, so never leave "
    "it empty.\n\n"
    "Some files include a '전체 함수/클래스 컨텍스트' section showing the full "
    "source of the function or class a change belongs to, in addition to the "
    "diff hunk. Use it only to understand surrounding code (signatures, "
    "control flow) — `evidence` must still quote from the diff hunk, not "
    "from this extra context."
)


class ReviewPipeline:
    def __init__(
        self,
        llm: LLMClient,
        *,
        model_version: str,
        prompt_version: str,
        max_tokens: int = 1500,
    ) -> None:
        self._llm = llm
        self._model_version = model_version
        self._prompt_version = prompt_version
        self._max_tokens = max_tokens

    async def run(
        self, event: ReviewRequestedEvent
    ) -> ReviewCompletedEvent | ReviewFailedEvent:
        targets = analyze(event)
        if not targets:
            return self._completed(event, "No reviewable changes found.", [])

        messages = self._build_messages(event, targets)

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
        # minor/suggestion은 inline comment로 달지 않는 대신, 요약에 한 줄씩 남긴다
        # (노션 20절 "Minor/Suggestion은 summary로만 제공").
        notes = summarize_minor(reviews)
        if not notes:
            return summary
        bullet_list = "\n".join(f"- {title}" for title in notes)
        return f"{summary}\n\n참고(경미한 항목):\n{bullet_list}"

    def _failed(
        self, event: ReviewRequestedEvent, reason: FailureReason
    ) -> ReviewFailedEvent:
        return ReviewFailedEvent(
            review_job_id=event.review_job_id,
            head_sha=event.head_sha,
            reason=reason,
        )

    def _build_messages(
        self, event: ReviewRequestedEvent, targets: list[ReviewTarget]
    ) -> list[ChatMessage]:
        diff = "\n\n".join(self._render_target(t) for t in targets)
        context = build_context(event.context_files)
        user = f"## Project Context\n{context}\n\n## Changes\n{diff}" if context else diff
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    def _render_target(self, target: ReviewTarget) -> str:
        block = f"# {target.file_path} ({target.status})\n" + "\n".join(target.hunks)
        if target.context_chunks:
            context_section = "\n\n".join(target.context_chunks)
            block += f"\n\n#### 전체 함수/클래스 컨텍스트\n{context_section}"
        return block
