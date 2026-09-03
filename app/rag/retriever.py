from __future__ import annotations

import logging

from app.rag.embeddings import Embedder
from app.rag.reranker import Reranker
from app.rag.schema import ChunkSearchResult
from app.rag.vector_store import QdrantVectorStore

logger = logging.getLogger(__name__)

# reranker가 있을 때 벡터 검색에서 얼마나 넉넉히 후보를 가져올지 배수.
# reranker 없이 limit+1만 가져오면 재정렬해봐야 고를 수 있는 후보 폭이 없다.
_RERANK_OVERFETCH_MULTIPLIER = 4


class ProjectContextRetriever:
    """diff와 관련된 프로젝트 기존 코드를 Qdrant에서 찾아 리뷰 컨텍스트로 제공한다.

    검색은 리뷰 품질을 보강하는 best-effort 기능이다 — Qdrant 연결 실패나
    컬렉션이 아직 비어있는 레포(2단계 인덱싱을 아직 안 돌린 경우)에서도 리뷰
    자체는 항상 진행돼야 하므로, 여기서 실패하면 예외를 삼키고 빈 결과로
    fallback한다. reranker 단계도 마찬가지로 실패하면 임베딩 정렬 결과로
    fallback한다.
    """

    def __init__(
        self,
        embedder: Embedder,
        vector_store: QdrantVectorStore,
        *,
        limit: int = 3,
        min_score: float = 0.5,
        reranker: Reranker | None = None,
    ) -> None:
        self._embedder = embedder
        self._vector_store = vector_store
        self._limit = limit
        self._min_score = min_score
        self._reranker = reranker

    def retrieve(
        self,
        query_text: str,
        repository_id: int,
        exclude_file_path: str | None = None,
    ) -> list[ChunkSearchResult]:
        if not query_text.strip():
            return []

        search_limit = self._limit + 1
        if self._reranker is not None:
            search_limit = self._limit * _RERANK_OVERFETCH_MULTIPLIER

        try:
            query_vector = self._embedder.embed_query(query_text)
            results = self._vector_store.search(repository_id, query_vector, limit=search_limit)
        except Exception:
            logger.warning(
                "project context retrieval failed, continuing without it", exc_info=True
            )
            return []

        filtered = [r for r in results if r.score >= self._min_score]
        if exclude_file_path is not None:
            filtered = [r for r in filtered if r.file_path != exclude_file_path]

        if self._reranker is not None:
            try:
                filtered = self._reranker.rerank(query_text, filtered)
            except Exception:
                logger.warning("reranking failed, using embedding order", exc_info=True)

        return filtered[: self._limit]
