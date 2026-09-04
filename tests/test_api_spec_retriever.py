from app.context.api_spec_retriever import ApiSpecRetriever
from app.rag.api_spec_vector_store import ApiSpecSearchResult


class FakeEmbedder:
    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


class SpyStore:
    def __init__(self, results: list[ApiSpecSearchResult]) -> None:
        self._results = results
        self.received_repository_id: int | None = None

    def search(
        self, repository_id: int, query_vector: list[float], *, limit: int = 5
    ) -> list[ApiSpecSearchResult]:
        self.received_repository_id = repository_id
        return self._results


def _result(path: str = "/api/x") -> ApiSpecSearchResult:
    return ApiSpecSearchResult(
        method="GET",
        path=path,
        summary="s",
        request_schema="",
        response_schema="",
        auth="",
        score=0.9,
    )


def test_retrieve_returns_matching_entries() -> None:
    store = SpyStore([_result()])
    retriever = ApiSpecRetriever(FakeEmbedder(), store)  # type: ignore[arg-type]

    results = retriever.retrieve("query", 42)

    assert store.received_repository_id == 42
    assert len(results) == 1


def test_retrieve_swallows_errors_and_returns_empty() -> None:
    class BoomStore:
        def search(self, *args: object, **kwargs: object) -> list[object]:
            raise RuntimeError("qdrant down")

    retriever = ApiSpecRetriever(FakeEmbedder(), BoomStore())  # type: ignore[arg-type]

    assert retriever.retrieve("query", 42) == []
