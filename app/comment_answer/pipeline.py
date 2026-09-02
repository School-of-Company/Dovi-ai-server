import logging
from typing import Protocol

from app.comment_answer.schema import (
    CommentAnswerCompletedEvent,
    CommentAnswerFailedEvent,
    CommentAnswerRequestedEvent,
)
from app.llm.client import ChatMessage

logger = logging.getLogger(__name__)


class TextGeneratingLLM(Protocol):
    async def generate_text(
        self, messages: list[ChatMessage], *, max_tokens: int = 500
    ) -> str:
        """구조화된 JSON 없이 자유 텍스트 응답을 생성한다.

        Raises:
            TimeoutError: LLM API 호출 타임아웃
            ValueError: 응답 형식이 예상과 다름
        """
        ...

_SYSTEM_PROMPT = (
    "You are answering a follow-up on a code review comment you previously "
    "left on a pull request. You are given the file path, the diff hunk the "
    "comment was about, and the full reply thread in chronological order "
    "(your original finding, then the human's replies).\n\n"
    "Read the human's latest reply and respond directly to it — if they "
    "gave a reason for declining your suggestion, say whether that reason "
    "holds up; if they asked a question, answer it concretely using the "
    "diff hunk as evidence. Do not repeat your original finding verbatim "
    "or restate the obvious.\n\n"
    "Write the answer in Korean, 1-4 concise sentences, as plain text — "
    "no markdown code fences, no headers, no suggestion blocks."
)


class CommentAnswerPipeline:
    def __init__(self, llm: TextGeneratingLLM, *, max_tokens: int = 500) -> None:
        self._llm = llm
        self._max_tokens = max_tokens

    async def run(
        self, event: CommentAnswerRequestedEvent
    ) -> CommentAnswerCompletedEvent | CommentAnswerFailedEvent:
        messages = self._build_messages(event)

        try:
            answer = await self._llm.generate_text(
                messages, max_tokens=self._max_tokens
            )
        except TimeoutError:
            logger.warning(
                "comment answer LLM timeout commentJobId=%s", event.comment_job_id
            )
            return self._failed(event, "timeout")
        except Exception:
            logger.exception(
                "unexpected error during comment answer generation "
                "commentJobId=%s",
                event.comment_job_id,
            )
            return self._failed(event, "server_error")

        answer = answer.strip()
        if not answer:
            logger.warning(
                "comment answer LLM returned empty text commentJobId=%s",
                event.comment_job_id,
            )
            return self._failed(event, "parse_error")

        logger.info("comment answer completed commentJobId=%s", event.comment_job_id)
        return CommentAnswerCompletedEvent(
            comment_job_id=event.comment_job_id, answer=answer
        )

    def _failed(
        self, event: CommentAnswerRequestedEvent, reason: str
    ) -> CommentAnswerFailedEvent:
        return CommentAnswerFailedEvent(
            comment_job_id=event.comment_job_id, reason=reason
        )

    def _build_messages(self, event: CommentAnswerRequestedEvent) -> list[ChatMessage]:
        thread_text = "\n\n".join(
            f"[{c.author}] {c.created_at}\n{c.body}" for c in event.thread
        )
        user = (
            f"## File\n{event.path} (line {event.line})\n\n"
            f"## Diff hunk\n{event.diff_hunk}\n\n"
            f"## Thread\n{thread_text}"
        )
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
