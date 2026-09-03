from app.rag.reranker import CrossEncoderReranker
from app.rag.schema import ChunkSearchResult


def test_lazy_loads_model_only_on_first_use() -> None:
    reranker = CrossEncoderReranker("fake-model")
    assert reranker._model is None


def test_rerank_returns_empty_list_without_loading_model_for_no_candidates() -> None:
    reranker = CrossEncoderReranker("fake-model")

    result = reranker.rerank("query", [])

    assert result == []
    assert reranker._model is None


class _FakeCrossEncoder:
    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        assert len(pairs) == len(self._scores)
        return self._scores


def _candidate(file_path: str) -> ChunkSearchResult:
    return ChunkSearchResult(
        file_path=file_path,
        node_type="function_definition",
        name="bar",
        start_line=1,
        end_line=3,
        source=f"def {file_path}(): pass",
        score=0.9,
    )


def test_rerank_orders_candidates_by_descending_predicted_score() -> None:
    reranker = CrossEncoderReranker("fake-model")
    # _model을 직접 채워 _get_model()의 지연 로딩 분기를 건드리지 않고 predict() 결과만 검증한다.
    reranker._model = _FakeCrossEncoder([0.1, 0.9, 0.5])  # type: ignore[assignment]
    candidates = [_candidate("a.py"), _candidate("b.py"), _candidate("c.py")]

    result = reranker.rerank("query", candidates)

    assert [c.file_path for c in result] == ["b.py", "c.py", "a.py"]
