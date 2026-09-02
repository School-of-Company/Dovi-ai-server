from app.rag.embeddings import CodeRankEmbedClient


def test_lazy_loads_model_only_on_first_use() -> None:
    client = CodeRankEmbedClient("fake-model")
    assert client._model is None


def test_embed_documents_returns_empty_list_without_loading_model() -> None:
    # 빈 입력이면 모델을 아예 로드하지 않아야 한다 — 네트워크/가중치 다운로드가
    # 필요 없는 CI 환경에서도 이 가드가 깨지면 즉시 실패로 드러나야 한다.
    client = CodeRankEmbedClient("fake-model")

    result = client.embed_documents([])

    assert result == []
    assert client._model is None
