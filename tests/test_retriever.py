from qdrant_client import QdrantClient

from app.rag.retriever import ProjectContextRetriever
from app.rag.vector_store import QdrantVectorStore
from app.review.chunking import AstChunk


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


def _store_with_chunk(file_path: str, vector: list[float]) -> QdrantVectorStore:
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
    store.upsert_chunks(file_path, [chunk], [vector])
    return store


def test_retrieve_returns_matching_chunk() -> None:
    store = _store_with_chunk("a.py", [1.0, 0.0, 0.0])
    retriever = ProjectContextRetriever(FakeEmbedder(), store)

    results = retriever.retrieve("find bar")

    assert len(results) == 1
    assert results[0].file_path == "a.py"


def test_retrieve_excludes_given_file_path() -> None:
    store = _store_with_chunk("a.py", [1.0, 0.0, 0.0])
    retriever = ProjectContextRetriever(FakeEmbedder(), store)

    results = retriever.retrieve("find bar", exclude_file_path="a.py")

    assert results == []


def test_retrieve_returns_empty_for_blank_query() -> None:
    store = _store_with_chunk("a.py", [1.0, 0.0, 0.0])
    retriever = ProjectContextRetriever(FakeEmbedder(), store)

    assert retriever.retrieve("   ") == []


def test_retrieve_filters_by_min_score() -> None:
    store = _store_with_chunk("a.py", [1.0, 0.0, 0.0])
    # 쿼리 벡터를 저장된 벡터와 정반대 방향으로 줘서 cosine 유사도가 낮게 나오게 한다.
    retriever = ProjectContextRetriever(
        FakeEmbedder(vector=[-1.0, 0.0, 0.0]), store, min_score=0.9
    )

    assert retriever.retrieve("find bar") == []


def test_retrieve_swallows_embedder_errors_and_returns_empty() -> None:
    store = _store_with_chunk("a.py", [1.0, 0.0, 0.0])
    retriever = ProjectContextRetriever(
        FakeEmbedder(error=RuntimeError("model not loaded")), store
    )

    assert retriever.retrieve("find bar") == []


def test_retrieve_swallows_vector_store_errors_and_returns_empty() -> None:
    class BoomStore:
        def search(self, *args: object, **kwargs: object) -> list[object]:
            raise RuntimeError("qdrant unreachable")

    retriever = ProjectContextRetriever(FakeEmbedder(), BoomStore())  # type: ignore[arg-type]

    assert retriever.retrieve("find bar") == []
