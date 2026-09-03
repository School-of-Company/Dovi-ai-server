from qdrant_client import QdrantClient

from app.rag.retriever import ProjectContextRetriever
from app.rag.schema import ChunkSearchResult
from app.rag.vector_store import QdrantVectorStore
from app.review.chunking import AstChunk

_REPO = 1


class FakeEmbedder:
    def __init__(
        self, *, vector: list[float] | None = None, error: Exception | None = None
    ) -> None:
        self._vector = vector or [1.0, 0.0, 0.0]
        self._error = error

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        if self._error is not None:
            raise self._error
        return self._vector

    @property
    def dimension(self) -> int:
        return len(self._vector)


def _store_with_chunk(
    file_path: str, vector: list[float], *, repository_id: int = _REPO
) -> QdrantVectorStore:
    client = QdrantClient(location=":memory:")
    store = QdrantVectorStore(client, "chunks", vector_size=len(vector))
    store.ensure_collection()
    chunk = AstChunk(
        node_type="function_definition",
        name="bar",
        start_line=1,
        end_line=3,
        source="def bar(): pass",
    )
    store.upsert_chunks(repository_id, file_path, [chunk], [vector])
    return store


def test_retrieve_returns_matching_chunk() -> None:
    store = _store_with_chunk("a.py", [1.0, 0.0, 0.0])
    retriever = ProjectContextRetriever(FakeEmbedder(), store)

    results = retriever.retrieve("find bar", _REPO)

    assert len(results) == 1
    assert results[0].file_path == "a.py"


def test_retrieve_scopes_search_to_given_repository_id() -> None:
    store = _store_with_chunk("a.py", [1.0, 0.0, 0.0], repository_id=1)
    retriever = ProjectContextRetriever(FakeEmbedder(), store)

    assert retriever.retrieve("find bar", 2) == []


def test_retrieve_excludes_given_file_path() -> None:
    store = _store_with_chunk("a.py", [1.0, 0.0, 0.0])
    retriever = ProjectContextRetriever(FakeEmbedder(), store)

    results = retriever.retrieve("find bar", _REPO, exclude_file_path="a.py")

    assert results == []


def test_retrieve_returns_empty_for_blank_query() -> None:
    store = _store_with_chunk("a.py", [1.0, 0.0, 0.0])
    retriever = ProjectContextRetriever(FakeEmbedder(), store)

    assert retriever.retrieve("   ", _REPO) == []


def test_retrieve_filters_by_min_score() -> None:
    store = _store_with_chunk("a.py", [1.0, 0.0, 0.0])
    # 쿼리 벡터를 저장된 벡터와 정반대 방향으로 줘서 cosine 유사도가 낮게 나오게 한다.
    retriever = ProjectContextRetriever(
        FakeEmbedder(vector=[-1.0, 0.0, 0.0]), store, min_score=0.9
    )

    assert retriever.retrieve("find bar", _REPO) == []


def test_retrieve_swallows_embedder_errors_and_returns_empty() -> None:
    store = _store_with_chunk("a.py", [1.0, 0.0, 0.0])
    retriever = ProjectContextRetriever(
        FakeEmbedder(error=RuntimeError("model not loaded")), store
    )

    assert retriever.retrieve("find bar", _REPO) == []


def test_retrieve_swallows_vector_store_errors_and_returns_empty() -> None:
    class BoomStore:
        def search(self, *args: object, **kwargs: object) -> list[object]:
            raise RuntimeError("qdrant unreachable")

    retriever = ProjectContextRetriever(FakeEmbedder(), BoomStore())  # type: ignore[arg-type]

    assert retriever.retrieve("find bar", _REPO) == []


def _result(file_path: str, score: float = 0.9) -> ChunkSearchResult:
    return ChunkSearchResult(
        file_path=file_path,
        node_type="function_definition",
        name="bar",
        start_line=1,
        end_line=3,
        source=f"def {file_path}(): pass",
        score=score,
    )


class SpyVectorStore:
    def __init__(self, results: list[ChunkSearchResult]) -> None:
        self._results = results
        self.received_limit: int | None = None
        self.received_repository_id: int | None = None

    def search(
        self, repository_id: int, query_vector: list[float], *, limit: int = 5
    ) -> list[ChunkSearchResult]:
        self.received_repository_id = repository_id
        self.received_limit = limit
        return self._results


class FakeReranker:
    def __init__(
        self, *, order: list[str] | None = None, error: Exception | None = None
    ) -> None:
        self._order = order
        self._error = error
        self.received: tuple[str, list[ChunkSearchResult]] | None = None

    def rerank(
        self, query_text: str, candidates: list[ChunkSearchResult]
    ) -> list[ChunkSearchResult]:
        self.received = (query_text, candidates)
        if self._error is not None:
            raise self._error
        if self._order is None:
            return candidates
        by_file = {c.file_path: c for c in candidates}
        return [by_file[f] for f in self._order if f in by_file]


def test_retrieve_passes_repository_id_to_vector_store_search() -> None:
    store = SpyVectorStore([_result("a.py")])
    retriever = ProjectContextRetriever(FakeEmbedder(), store)  # type: ignore[arg-type]

    retriever.retrieve("find bar", 42)

    assert store.received_repository_id == 42


def test_retrieve_overfetches_when_reranker_given() -> None:
    store = SpyVectorStore([_result("a.py")])
    retriever = ProjectContextRetriever(
        FakeEmbedder(), store, limit=3, reranker=FakeReranker()  # type: ignore[arg-type]
    )

    retriever.retrieve("find bar", _REPO)

    assert store.received_limit == 12  # limit(3) * overfetch multiplier(4)


def test_retrieve_uses_reranked_order() -> None:
    store = SpyVectorStore([_result("a.py"), _result("b.py")])
    reranker = FakeReranker(order=["b.py", "a.py"])
    retriever = ProjectContextRetriever(
        FakeEmbedder(), store, reranker=reranker  # type: ignore[arg-type]
    )

    results = retriever.retrieve("find bar", _REPO)

    assert [r.file_path for r in results] == ["b.py", "a.py"]
    assert reranker.received is not None
    assert reranker.received[0] == "find bar"


def test_retrieve_falls_back_to_embedding_order_when_reranker_fails() -> None:
    store = SpyVectorStore([_result("a.py"), _result("b.py")])
    reranker = FakeReranker(error=RuntimeError("model not loaded"))
    retriever = ProjectContextRetriever(
        FakeEmbedder(), store, reranker=reranker  # type: ignore[arg-type]
    )

    results = retriever.retrieve("find bar", _REPO)

    assert [r.file_path for r in results] == ["a.py", "b.py"]


def test_retrieve_passes_already_filtered_candidates_to_reranker() -> None:
    # min_score 미달(c.py)과 exclude_file_path(a.py)는 reranker에 아예 전달되면 안 된다 —
    # cross-encoder 추론을 이미 걸러진 후보에만 돌리기 위한 필터 순서 계약을 고정한다.
    store = SpyVectorStore(
        [_result("a.py", score=0.9), _result("b.py", score=0.9), _result("c.py", score=0.1)]
    )
    reranker = FakeReranker()
    retriever = ProjectContextRetriever(
        FakeEmbedder(), store, min_score=0.5, reranker=reranker  # type: ignore[arg-type]
    )

    retriever.retrieve("find bar", _REPO, exclude_file_path="a.py")

    assert reranker.received is not None
    assert [c.file_path for c in reranker.received[1]] == ["b.py"]


def test_retrieve_truncates_reranked_results_to_limit() -> None:
    store = SpyVectorStore([_result("a.py"), _result("b.py"), _result("c.py")])
    reranker = FakeReranker(order=["c.py", "b.py", "a.py"])
    retriever = ProjectContextRetriever(
        FakeEmbedder(), store, limit=1, reranker=reranker  # type: ignore[arg-type]
    )

    results = retriever.retrieve("find bar", _REPO)

    assert [r.file_path for r in results] == ["c.py"]
