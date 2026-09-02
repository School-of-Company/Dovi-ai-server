from app.comment_answer.pipeline import CommentAnswerPipeline
from app.comment_answer.schema import (
    CommentAnswerCompletedEvent,
    CommentAnswerFailedEvent,
    CommentAnswerRequestedEvent,
    ThreadComment,
)
from app.llm.client import ChatMessage


class FakeTextLLM:
    def __init__(
        self, text: str | None = None, error: Exception | None = None
    ) -> None:
        self._text = text
        self._error = error
        self.received: list[ChatMessage] | None = None

    async def generate_text(
        self, messages: list[ChatMessage], *, max_tokens: int = 500
    ) -> str:
        self.received = messages
        if self._error is not None:
            raise self._error
        assert self._text is not None
        return self._text


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
