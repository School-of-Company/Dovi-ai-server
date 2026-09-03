from __future__ import annotations

import logging

from app.rag.embeddings import Embedder
from app.rag.vector_store import ChunkSearchResult, QdrantVectorStore

logger = logging.getLogger(__name__)


class ProjectContextRetriever:
    """diff와 관련된 프로젝트 기존 코드를 Qdrant에서 찾아 리뷰 컨텍스트로 제공한다.

    검색은 리뷰 품질을 보강하는 best-effort 기능이다 — Qdrant 연결 실패나
    컬렉션이 아직 비어있는 레포(2단계 인덱싱을 아직 안 돌린 경우)에서도 리뷰
    자체는 항상 진행돼야 하므로, 여기서 실패하면 예외를 삼키고 빈 결과로
    fallback한다.
    """

    def __init__(
        self,
        embedder: Embedder,
        vector_store: QdrantVectorStore,
        *,
        limit: int = 3,
        min_score: float = 0.5,
    ) -> None:
        self._embedder = embedder
        self._vector_store = vector_store
        self._limit = limit
        self._min_score = min_score

    def retrieve(
        self, query_text: str, exclude_file_path: str | None = None
    ) -> list[ChunkSearchResult]:
        if not query_text.strip():
            return []

        try:
            query_vector = self._embedder.embed_query(query_text)
            # exclude_file_path로 걸러낼 몫까지 감안해 여유 있게 가져온다.
            results = self._vector_store.search(query_vector, limit=self._limit + 1)
        except Exception:
            logger.warning(
                "project context retrieval failed, continuing without it", exc_info=True
            )
            return []

        filtered = [r for r in results if r.score >= self._min_score]
        if exclude_file_path is not None:
            filtered = [r for r in filtered if r.file_path != exclude_file_path]
        return filtered[: self._limit]
