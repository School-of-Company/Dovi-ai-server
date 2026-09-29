import logging
import math
from typing import Protocol

from app.comment_answer.schema import (
    CommentAnswerCompletedEvent,
    CommentAnswerFailedEvent,
    CommentAnswerRequestedEvent,
    ThreadComment,
)
from app.llm.client import ChatMessage
from app.llm.tokens import estimate_tokens

logger = logging.getLogger(__name__)

# 리뷰 파이프라인(app/review/pipeline.py)과 같은 값·같은 이유 — chat template의
# role 마커 등 content 문자열만 세서는 안 잡히는 토큰을 위한 여유분이다.
# 아키텍처 규칙상 사용처가 3곳 이상일 때만 app/common/으로 뺀다(지금은 2곳).
_SAFETY_MARGIN_TOKENS = 200

# diff_hunk를 이 토큰 수 밑으로는 줄이지 않는다. 리뷰 파이프라인의
# _MIN_DIFF_TOKENS(500)보다 작게 잡는다 — diff_hunk는 PR 전체 diff가 아니라
# 코멘트 하나가 달린 단일 hunk라 원래 훨씬 작다.
_MIN_DIFF_HUNK_TOKENS = 200


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

    async def count_tokens(self, text: str) -> int:
        """text의 실제 토큰 수를 센다(이슈 #107 — review pipeline과 같은 llm_client를
        쓰므로 계약도 동일하다).

        실패하면 예외를 그대로 던진다 — 폴백(보수적 추정)은 호출자 책임이다.
        """
        ...

    async def get_context_window(self) -> int | None:
        """서버가 실제로 쓸 수 있는 컨텍스트 크기(usable n_ctx)를 반환한다.

        조회할 수 없으면 None을 반환한다(예외를 던지지 않는다).
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


def _cut_at_line_boundary(text: str, content_limit: int) -> int:
    # app/review/pipeline.py의 동명 헬퍼와 로직이 같은 순수 문자열 유틸리티다 —
    # 코드 한 줄이 반토막 나면 LLM이 없는 문법 오류로 착각할 수 있어 가능하면
    # 줄 경계에서 자른다. 사용처가 3곳 이상이 되기 전까지는 각자 복제해 둔다.
    cut = text.rfind("\n", 0, content_limit)
    if cut == -1 or cut < content_limit // 2:
        cut = content_limit
    return cut


def _shrink_to_char_limit(text: str, target_chars: int) -> str:
    if target_chars <= 0:
        return ""
    if len(text) <= target_chars:
        return text
    trunc_msg = "\n...(truncated)"
    if target_chars < len(trunc_msg):
        return ""
    content_limit = target_chars - len(trunc_msg)
    cut = _cut_at_line_boundary(text, content_limit)
    return text[:cut] + trunc_msg


class CommentAnswerPipeline:
    def __init__(
        self,
        llm: TextGeneratingLLM,
        *,
        max_tokens: int = 500,
        llm_max_context: int = 8192,
    ) -> None:
        self._llm = llm
        self._max_tokens = max_tokens
        self._llm_max_context = llm_max_context
        # /props로 확인한 실제 usable 컨텍스트 — review pipeline과 동일한 캐싱
        # 패턴(성공한 값만 캐시, 실패는 다음 호출에서 재시도).
        self._effective_max_context: int | None = None

    async def run(
        self, event: CommentAnswerRequestedEvent
    ) -> CommentAnswerCompletedEvent | CommentAnswerFailedEvent:
        messages = await self._assemble_within_budget(event)
        if messages is None:
            logger.warning(
                "comment answer prompt exceeds context budget even after "
                "reducing commentJobId=%s",
                event.comment_job_id,
            )
            return self._failed(event, "context_overflow")

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

    async def _resolve_max_context(self) -> int:
        """review pipeline의 동명 메서드와 동일한 이유·동일한 계약 — 같은
        llm_client(/props)를 쓰지만 캐시는 파이프라인 인스턴스별로 따로 갖는다."""
        if self._effective_max_context is not None:
            return self._effective_max_context
        try:
            actual = await self._llm.get_context_window()
        except Exception:
            actual = None
        if actual is not None:
            self._effective_max_context = min(self._llm_max_context, actual)
            return self._effective_max_context
        return self._llm_max_context

    async def _count_tokens(self, text: str) -> int:
        """실제 토큰 수를 재고, 실패하면 보수적 추정으로 폴백한다(review
        pipeline과 동일한 app.llm.tokens.estimate_tokens() 재사용 — 이 모듈은
        도메인 로직이 아니라 llm 계층의 공용 유틸리티라 review/comment_answer
        양쪽에서 직접 가져다 써도 계층 규칙에 어긋나지 않는다)."""
        try:
            return await self._llm.count_tokens(text)
        except Exception:
            logger.warning(
                "token count via /tokenize failed, using conservative estimate",
                exc_info=True,
            )
            return estimate_tokens(text)

    async def _diff_hunk_floor_chars(self, diff_hunk: str) -> int:
        """diff_hunk를 이 문자 수 밑으로는 줄이지 않는다.

        원래 diff_hunk가 이미 _MIN_DIFF_HUNK_TOKENS보다 작으면 그 크기 자체가
        바닥이라 — 그런 작은 hunk가 축소 대상이 되거나 실패 조건이 되면
        안 된다(review pipeline의 _diff_floor_chars와 동일한 이유).
        """
        original_tokens = await self._count_tokens(diff_hunk)
        if original_tokens <= _MIN_DIFF_HUNK_TOKENS:
            return len(diff_hunk)
        return max(
            0, math.floor(len(diff_hunk) * _MIN_DIFF_HUNK_TOKENS / original_tokens)
        )

    async def _reduced_char_limit(self, current_text: str, overshoot_tokens: int) -> int:
        """current_text에서 overshoot_tokens만큼 토큰을 줄이기 위한 목표 문자 수를
        비례 계산한다(review pipeline의 동명 메서드와 동일한 알고리즘·동일한
        이유 — overshoot_tokens와 반드시 같은 측정 기준으로 현재 크기를 재야
        한다)."""
        current_tokens = await self._count_tokens(current_text)
        if current_tokens <= 0:
            return 0
        target_tokens = max(0, current_tokens - overshoot_tokens)
        return max(0, math.floor(len(current_text) * target_tokens / current_tokens))

    async def _assemble_within_budget(
        self, event: CommentAnswerRequestedEvent
    ) -> list[ChatMessage] | None:
        """오늘의 _build_messages() 결과를 실측 토큰 수로 검증하고, 예산을 넘으면
        오래된 스레드 코멘트부터 하나씩 버리고(event.thread는 chronological —
        스키마 계약상 thread[0]이 가장 오래됨), 그래도 넘치면 diff_hunk를
        _MIN_DIFF_HUNK_TOKENS까지 줄인다(이슈 #107).

        1차 조립(오버라이드 없음)은 기존 로직 그대로라, 예산 안에 드는 평범한
        스레드는 축소 단계가 아예 작동하지 않고 프롬프트가 오늘과 100% 동일하게
        나온다 — 이게 회귀 테스트가 성립하는 근거다(#98과 동일한 원칙).

        전부 최소로 줄여도 넘치면 None을 반환해 호출자가 context_overflow로
        실패 처리하게 한다(조용히 잘린 프롬프트를 보내지 않는다).
        """
        effective_max_context = await self._resolve_max_context()
        budget = effective_max_context - self._max_tokens - _SAFETY_MARGIN_TOKENS
        diff_hunk_floor = await self._diff_hunk_floor_chars(event.diff_hunk)

        thread = list(event.thread)
        diff_hunk_max_chars: int | None = None

        for _attempt in range(1 + len(event.thread) + 3):
            messages = self._build_messages(
                event, thread=thread, diff_hunk_max_chars=diff_hunk_max_chars
            )
            text = messages[0]["content"] + messages[1]["content"]
            actual = await self._count_tokens(text)
            if actual <= budget:
                return messages

            if thread:
                # 가장 오래된 코멘트부터 버린다 — 최신 코멘트(사람의 최근
                # 반응)일수록 답변에 더 중요하므로 남긴다.
                thread = thread[1:]
                continue

            overshoot = actual - budget
            current = (
                event.diff_hunk
                if diff_hunk_max_chars is None
                else _shrink_to_char_limit(event.diff_hunk, diff_hunk_max_chars)
            )
            new_limit = max(
                diff_hunk_floor, await self._reduced_char_limit(current, overshoot)
            )
            if new_limit >= len(current):
                return None  # 더 줄일 수 없다 — 명시적 실패
            diff_hunk_max_chars = new_limit

        return None

    def _build_messages(
        self,
        event: CommentAnswerRequestedEvent,
        *,
        thread: list[ThreadComment] | None = None,
        diff_hunk_max_chars: int | None = None,
    ) -> list[ChatMessage]:
        """override 인자를 전부 안 주면(=None) 오늘의 로직과 100% 동일하게
        동작한다 — _assemble_within_budget()이 예산 초과가 실측으로 확인됐을
        때만 override를 채워 재조립한다(이슈 #107)."""
        thread_to_use = event.thread if thread is None else thread
        diff_hunk = event.diff_hunk
        if diff_hunk_max_chars is not None:
            diff_hunk = _shrink_to_char_limit(diff_hunk, diff_hunk_max_chars)

        thread_text = "\n\n".join(
            f"[{c.author}] {c.created_at}\n{c.body}" for c in thread_to_use
        )
        user = (
            f"## File\n{event.path} (line {event.line})\n\n"
            f"## Diff hunk\n{diff_hunk}\n\n"
            f"## Thread\n{thread_text}"
        )
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
