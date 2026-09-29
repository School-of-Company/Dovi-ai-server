from collections.abc import Callable

from app.comment_answer.pipeline import (
    _SAFETY_MARGIN_TOKENS,
    _SYSTEM_PROMPT,
    CommentAnswerPipeline,
)
from app.comment_answer.schema import (
    CommentAnswerCompletedEvent,
    CommentAnswerFailedEvent,
    CommentAnswerRequestedEvent,
    ThreadComment,
)
from app.llm.client import ChatMessage


def _realistic_token_counter(text: str) -> int:
    """대략 4자/토큰 — review pipeline 테스트의 동명 헬퍼와 같은 이유
    (1자=1토큰으로 세면 시스템 프롬프트만으로도 테스트 예산을 넘겨버린다)."""
    return len(text) // 4 + 1


def _llm_max_context_with_slack(*, max_tokens: int, slack_tokens: int) -> int:
    """시스템 프롬프트 실제 크기를 반영해 계산한다 — review pipeline 테스트의
    동명 헬퍼와 같은 이유(하드코딩된 매직 넘버 대신 실측 기반)."""
    system_tokens = _realistic_token_counter(_SYSTEM_PROMPT)
    return system_tokens + max_tokens + _SAFETY_MARGIN_TOKENS + slack_tokens


class FakeTextLLM:
    def __init__(
        self,
        text: str | None = None,
        error: Exception | None = None,
        token_counter: Callable[[str], int] | None = None,
        context_window: int | None = None,
    ) -> None:
        self._text = text
        self._error = error
        # 기본은 항상 "예산 안"(작은 고정값)으로 잡아, 이번 토큰 예산 기능이
        # 없던 기존 테스트들이 축소 없이 그대로 통과하게 한다(회귀 없음 보장).
        self._token_counter = token_counter
        self._context_window = context_window
        self.received: list[ChatMessage] | None = None

    async def generate_text(
        self, messages: list[ChatMessage], *, max_tokens: int = 500
    ) -> str:
        self.received = messages
        if self._error is not None:
            raise self._error
        assert self._text is not None
        return self._text

    async def count_tokens(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter(text)
        return 1

    async def get_context_window(self) -> int | None:
        return self._context_window


def _event() -> CommentAnswerRequestedEvent:
    return CommentAnswerRequestedEvent(
        comment_job_id="qa:1:2:100",
        repository_id=1,
        pr_number=2,
        path="src/foo.ts",
        line=12,
        diff_hunk="@@ -1 +1 @@\n-old\n+new",
        thread=[
            ThreadComment(
                comment_id=99,
                author="dovi-code-assist[bot]",
                body="원본 지적",
                created_at="2026-09-01T00:00:00Z",
            ),
            ThreadComment(
                comment_id=100,
                author="cfcromn",
                body="@dovi-code-assist 이거 반박합니다",
                created_at="2026-09-01T00:05:00Z",
            ),
        ],
    )


async def test_run_returns_completed_on_success() -> None:
    llm = FakeTextLLM(text="반박이 타당합니다.")
    result = await CommentAnswerPipeline(llm).run(_event())

    assert isinstance(result, CommentAnswerCompletedEvent)
    assert result.comment_job_id == "qa:1:2:100"
    assert result.answer == "반박이 타당합니다."
    assert llm.received is not None


async def test_run_includes_thread_and_diff_hunk_in_prompt() -> None:
    llm = FakeTextLLM(text="ok")
    await CommentAnswerPipeline(llm).run(_event())

    assert llm.received is not None
    user_message = llm.received[1]["content"]
    assert "@@ -1 +1 @@" in user_message
    assert "원본 지적" in user_message
    assert "이거 반박합니다" in user_message


async def test_run_returns_failed_on_timeout() -> None:
    llm = FakeTextLLM(error=TimeoutError())
    result = await CommentAnswerPipeline(llm).run(_event())

    assert isinstance(result, CommentAnswerFailedEvent)
    assert result.reason == "timeout"


async def test_run_returns_failed_on_server_error() -> None:
    llm = FakeTextLLM(error=RuntimeError("connection refused"))
    result = await CommentAnswerPipeline(llm).run(_event())

    assert isinstance(result, CommentAnswerFailedEvent)
    assert result.reason == "server_error"


async def test_run_returns_failed_on_empty_answer() -> None:
    llm = FakeTextLLM(text="   ")
    result = await CommentAnswerPipeline(llm).run(_event())

    assert isinstance(result, CommentAnswerFailedEvent)
    assert result.reason == "parse_error"


# --- 토큰 기준 프롬프트 예산 (이슈 #107) ---


async def test_run_drops_oldest_thread_comments_first_when_budget_exceeded() -> None:
    event = CommentAnswerRequestedEvent(
        comment_job_id="qa:1:2:100",
        repository_id=1,
        pr_number=2,
        path="src/foo.ts",
        line=12,
        diff_hunk="@@ -1 +1 @@\n-old\n+new",
        thread=[
            ThreadComment(
                comment_id=1,
                author="dovi-code-assist[bot]",
                body="오래된 코멘트 " * 100,
                created_at="2026-09-01T00:00:00Z",
            ),
            ThreadComment(
                comment_id=2,
                author="human",
                body="최신 코멘트",
                created_at="2026-09-01T00:05:00Z",
            ),
        ],
    )
    max_tokens = 50
    llm_max_context = _llm_max_context_with_slack(max_tokens=max_tokens, slack_tokens=30)
    llm = FakeTextLLM(text="ok", token_counter=_realistic_token_counter)
    pipeline = CommentAnswerPipeline(
        llm, max_tokens=max_tokens, llm_max_context=llm_max_context
    )

    result = await pipeline.run(event)

    assert isinstance(result, CommentAnswerCompletedEvent)
    assert llm.received is not None
    user_message = llm.received[1]["content"]
    assert "오래된 코멘트" not in user_message  # 가장 오래된 코멘트부터 버려짐
    assert "최신 코멘트" in user_message  # 최신 코멘트는 남음


async def test_run_shrinks_diff_hunk_after_dropping_all_thread_comments() -> None:
    huge_diff_hunk = "@@ -1,500 +1,500 @@\n" + "\n".join(
        f"-line {i}\n+line {i}" for i in range(500)
    )
    event = CommentAnswerRequestedEvent(
        comment_job_id="qa:1:2:100",
        repository_id=1,
        pr_number=2,
        path="src/foo.ts",
        line=12,
        diff_hunk=huge_diff_hunk,
        thread=[
            ThreadComment(
                comment_id=1, author="human", body="질문", created_at="2026-09-01T00:00:00Z"
            )
        ],
    )
    max_tokens = 50
    llm_max_context = _llm_max_context_with_slack(max_tokens=max_tokens, slack_tokens=300)
    llm = FakeTextLLM(text="ok", token_counter=_realistic_token_counter)
    pipeline = CommentAnswerPipeline(
        llm, max_tokens=max_tokens, llm_max_context=llm_max_context
    )

    result = await pipeline.run(event)

    assert isinstance(result, CommentAnswerCompletedEvent)
    assert llm.received is not None
    user_message = llm.received[1]["content"]
    assert "질문" not in user_message  # 스레드는 다 버려짐
    assert "...(truncated)" in user_message  # 그래도 넘쳐 diff_hunk까지 줄어듦
    assert "line 0" in user_message  # 앞부분은 남아있음(줄 경계에서 자름)


async def test_run_never_shrinks_diff_hunk_below_floor_for_naturally_small_hunk() -> None:
    """diff_hunk가 원래 _MIN_DIFF_HUNK_TOKENS보다 작으면, 스레드가 아무리 커도
    diff_hunk 자체가 축소 대상이나 실패 원인이 되면 안 된다(diff_hunk_floor
    보정 — review pipeline #98 v4에서 잡은 것과 같은 함정)."""
    event = CommentAnswerRequestedEvent(
        comment_job_id="qa:1:2:100",
        repository_id=1,
        pr_number=2,
        path="src/foo.ts",
        line=12,
        diff_hunk="@@ -1 +1 @@\n-old\n+new",
        thread=[
            ThreadComment(
                comment_id=1,
                author="human",
                body="아주 긴 코멘트 " * 200,
                created_at="2026-09-01T00:00:00Z",
            )
        ],
    )
    max_tokens = 50
    # 슬랙은 빈 스레드 + 자연 크기 diff_hunk + 고정 보일러플레이트("## File\n" 등)가
    # 들어갈 정도는 넉넉해야 한다 — 그래도 200토큰짜리 스레드 코멘트보다는
    # 훨씬 작아 드롭이 여전히 필요하다.
    llm_max_context = _llm_max_context_with_slack(max_tokens=max_tokens, slack_tokens=50)
    llm = FakeTextLLM(text="ok", token_counter=_realistic_token_counter)
    pipeline = CommentAnswerPipeline(
        llm, max_tokens=max_tokens, llm_max_context=llm_max_context
    )

    result = await pipeline.run(event)

    assert isinstance(result, CommentAnswerCompletedEvent)  # 실패하면 안 된다
    assert llm.received is not None
    user_message = llm.received[1]["content"]
    assert "@@ -1 +1 @@" in user_message
    assert "-old" in user_message
    assert "+new" in user_message


async def test_run_fails_with_context_overflow_when_diff_hunk_cannot_fit_even_at_floor() -> (
    None
):
    huge_diff_hunk = "@@ -1,3000 +1,3000 @@\n" + "\n".join(
        f"+line {i}" for i in range(3000)
    )
    event = CommentAnswerRequestedEvent(
        comment_job_id="qa:1:2:100",
        repository_id=1,
        pr_number=2,
        path="src/foo.ts",
        line=12,
        diff_hunk=huge_diff_hunk,
        thread=[],
    )
    llm = FakeTextLLM(text="ok", token_counter=len)
    pipeline = CommentAnswerPipeline(llm, llm_max_context=50, max_tokens=10)

    result = await pipeline.run(event)

    assert isinstance(result, CommentAnswerFailedEvent)
    assert result.reason == "context_overflow"
    assert llm.received is None  # LLM 생성 호출 자체가 없어야 한다


async def test_resolve_max_context_only_caches_successful_result() -> None:
    class FlakyContextLLM(FakeTextLLM):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self.context_window_calls = 0

        async def get_context_window(self) -> int | None:
            self.context_window_calls += 1
            if self.context_window_calls == 1:
                return None  # 첫 호출은 실패(서버가 아직 기동 중인 경우 등)
            return 4096  # 재시도부터는 성공

    llm = FlakyContextLLM(text="ok")
    pipeline = CommentAnswerPipeline(llm, llm_max_context=8192)

    first = await pipeline._resolve_max_context()
    second = await pipeline._resolve_max_context()
    third = await pipeline._resolve_max_context()

    assert first == 8192  # 실패 → 설정값 폴백(캐시 안 됨)
    assert second == 4096  # 재시도 성공 → /props 값 사용
    assert third == 4096  # 성공한 값은 캐시되어 재호출 안 됨
    assert llm.context_window_calls == 2
