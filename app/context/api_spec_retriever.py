from __future__ import annotations

import logging

from app.rag.api_spec_schema import ApiSpecSearchResult
from app.rag.api_spec_vector_store import ApiSpecVectorStore
from app.rag.embeddings import Embedder

logger = logging.getLogger(__name__)


class ApiSpecRetriever:
    """Notion에서 동기화된 API 명세를 검색한다.

    ProjectContextRetriever와 동일한 best-effort 원칙: 실패하면 예외를 삼키고
    빈 결과로 fallback한다 (API 명세 검색 실패가 리뷰 자체를 막으면 안 된다).
    """

    def __init__(
        self,
        embedder: Embedder,
        vector_store: ApiSpecVectorStore,
        *,
        limit: int = 3,
        min_score: float = 0.5,
    ) -> None:
        self._embedder = embedder
        self._vector_store = vector_store
        self._limit = limit
        self._min_score = min_score

    def retrieve(self, query_text: str, repository_id: int) -> list[ApiSpecSearchResult]:
        if not query_text.strip():
            return []
        try:
            query_vector = self._embedder.embed_query(query_text)
            results = self._vector_store.search(repository_id, query_vector, limit=self._limit)
        except Exception:
            logger.warning("api spec retrieval failed, continuing without it", exc_info=True)
            return []
        return [r for r in results if r.score >= self._min_score][: self._limit]
