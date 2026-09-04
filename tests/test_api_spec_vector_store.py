from qdrant_client import QdrantClient

from app.notion.schema import ApiSpecEntry
from app.rag.api_spec_vector_store import ApiSpecVectorStore


def _store(dimension: int = 3) -> ApiSpecVectorStore:
    client = QdrantClient(location=":memory:")
    return ApiSpecVectorStore(client, "api_spec", vector_size=dimension)


def _entry(path: str = "/api/users") -> ApiSpecEntry:
    return ApiSpecEntry(method="GET", path=path, summary="유저 조회")


def test_upsert_and_search_roundtrip() -> None:
    store = _store()
    store.ensure_collection()

    store.upsert_entries(1, [_entry()], [[1.0, 0.0, 0.0]])
    results = store.search(1, [1.0, 0.0, 0.0], limit=5)

    assert len(results) == 1
    assert results[0].path == "/api/users"
    assert results[0].method == "GET"


def test_search_does_not_leak_across_repositories() -> None:
    store = _store()
    store.ensure_collection()
    store.upsert_entries(1, [_entry()], [[1.0, 0.0, 0.0]])
    store.upsert_entries(2, [_entry()], [[1.0, 0.0, 0.0]])

    assert len(store.search(1, [1.0, 0.0, 0.0], limit=10)) == 1
    assert len(store.search(2, [1.0, 0.0, 0.0], limit=10)) == 1


def test_delete_by_repository_removes_only_that_repository() -> None:
    store = _store()
    store.ensure_collection()
    store.upsert_entries(1, [_entry()], [[1.0, 0.0, 0.0]])
    store.upsert_entries(2, [_entry()], [[1.0, 0.0, 0.0]])

    store.delete_by_repository(1)

    assert store.search(1, [1.0, 0.0, 0.0], limit=10) == []
    assert len(store.search(2, [1.0, 0.0, 0.0], limit=10)) == 1
