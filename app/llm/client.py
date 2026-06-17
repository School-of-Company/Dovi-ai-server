from typing import Protocol

from app.review.schema import ReviewModelOutput

ChatMessage = dict[str, str]


class LLMClient(Protocol):
    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int = 1500
    ) -> ReviewModelOutput: ...
