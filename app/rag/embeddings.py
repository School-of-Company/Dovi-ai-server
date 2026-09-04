from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# CodeRankEmbed는 query/document를 비대칭으로 인코딩하는 모델이라, 검색 질의에는
# 이 접두사를 붙여야 문서 임베딩과 같은 공간에서 의미 있게 비교된다 (모델 카드 지침).
_QUERY_PREFIX = "Represent this query for searching relevant code: "


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """색인 대상 코드 chunk들을 임베딩한다."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """검색 질의를 임베딩한다. 문서 임베딩과 인코딩 방식이 다를 수 있다."""
        ...

    @property
    def dimension(self) -> int:
        """임베딩 벡터 차원. Qdrant collection 생성 시 필요하다."""
        ...


class CodeRankEmbedClient:
    """nomic-ai/CodeRankEmbed 기반 임베딩 클라이언트.

    모델 로드가 무겁다(가중치 다운로드 + GPU/CPU 적재). import 시점이나 생성자에서
    바로 로드하면 이 모듈을 가져오기만 해도 느려지므로, 첫 embed 호출까지 미룬다.
    """

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model: SentenceTransformer | None = None

    def _get_model(self) -> SentenceTransformer:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("loading embedding model=%s", self._model_name)
            # CodeRankEmbed는 커스텀 모델 코드를 함께 배포해서 trust_remote_code
            # 없이는 로드가 안 된다 — HuggingFace 레포에서 받은 코드를 그대로
            # 실행한다는 뜻이므로, EMBEDDING_MODEL 값을 바꿀 땐 출처를 신뢰할 수
            # 있는 레포인지 먼저 확인해야 한다.
            self._model = SentenceTransformer(self._model_name, trust_remote_code=True)
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._get_model().encode(texts, convert_to_numpy=True)
        return [vector.tolist() for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        vectors = self._get_model().encode([_QUERY_PREFIX + text], convert_to_numpy=True)
        return vectors[0].tolist()  # type: ignore[no-any-return]

    @property
    def dimension(self) -> int:
        dim = self._get_model().get_embedding_dimension()
        if dim is None:
            raise RuntimeError(f"model {self._model_name} did not report an embedding dimension")
        return dim
