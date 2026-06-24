import logging

from pydantic import ValidationError

from app.llm.client import ChatMessage, LLMClient
from app.review.context import build_context
from app.review.diff import analyze
from app.review.result_filter import filter_reviews
from app.review.schema import (
    FailureReason,
    ReviewComment,
    ReviewCompletedEvent,
    ReviewFailedEvent,
    ReviewRequestedEvent,
    ReviewTarget,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = "You are a code review assistant. Review the diff and report real issues."


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
        try:
            output = await self._llm.generate(messages, max_tokens=self._max_tokens)
        except TimeoutError:
            return self._failed(event, "timeout")
        except (ValueError, ValidationError):
            return self._failed(event, "parse_error")
        except Exception:
            logger.exception("unexpected error during LLM generation")
            return self._failed(event, "server_error")

        reviews = filter_reviews(output.reviews)
        return self._completed(event, output.summary, reviews)

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
        diff = "\n\n".join(
            f"# {t.file_path} ({t.status})\n" + "\n".join(t.hunks) for t in targets
        )
        context = build_context(event.context_files)
        user = f"## Project Context\n{context}\n\n## Changes\n{diff}" if context else diff
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
