from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from app.rag.schema import ChunkSearchResult

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

logger = logging.getLogger(__name__)


class Reranker(Protocol):
    def rerank(
        self, query_text: str, candidates: list[ChunkSearchResult]
    ) -> list[ChunkSearchResult]:
        """candidates를 query_text와의 관련도 기준으로 재정렬한다."""
        ...


class CrossEncoderReranker:
    """cross-encoder 기반 reranker.

    모델 로드가 무겁다. import 시점이나 생성자에서 바로 로드하면 이 모듈을
    가져오기만 해도 느려지므로, 첫 rerank 호출까지 미룬다 (CodeRankEmbedClient와
    동일한 패턴).
    """

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model: CrossEncoder | None = None

    def _get_model(self) -> CrossEncoder:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            logger.info("loading reranker model=%s", self._model_name)
            self._model = CrossEncoder(self._model_name)
        return self._model

    def rerank(
        self, query_text: str, candidates: list[ChunkSearchResult]
    ) -> list[ChunkSearchResult]:
        if not candidates:
            return []

        pairs = [(query_text, candidate.source) for candidate in candidates]
        scores = self._get_model().predict(pairs)
        ranked = sorted(zip(candidates, scores), key=lambda pair: pair[1], reverse=True)
        return [candidate for candidate, _ in ranked]
