import pytest
from qdrant_client import QdrantClient

from app.rag.vector_store import QdrantVectorStore
from app.review.chunking import AstChunk

_REPO = 1


def _store(dimension: int = 3) -> QdrantVectorStore:
    client = QdrantClient(location=":memory:")
    return QdrantVectorStore(client, "chunks", vector_size=dimension)


def _chunk(**overrides: object) -> AstChunk:
    defaults: dict[str, object] = {
        "node_type": "function_definition",
        "name": "foo",
        "start_line": 1,
        "end_line": 3,
        "source": "def foo(): pass",
    }
    defaults.update(overrides)
    return AstChunk(**defaults)  # type: ignore[arg-type]


def test_ensure_collection_is_idempotent() -> None:
    store = _store()
    store.ensure_collection()
    store.ensure_collection()  # 두 번째 호출도 에러 없이 통과해야 한다


def test_upsert_and_search_roundtrip() -> None:
    store = _store()
    store.ensure_collection()
    chunk = _chunk()

    store.upsert_chunks(_REPO, "app/foo.py", [chunk], [[1.0, 0.0, 0.0]])
    results = store.search(_REPO, [1.0, 0.0, 0.0], limit=5)

    assert len(results) == 1
    result = results[0]
    assert result.file_path == "app/foo.py"
    assert result.node_type == "function_definition"
    assert result.name == "foo"
    assert result.start_line == 1
    assert result.end_line == 3
    assert result.source == "def foo(): pass"


def test_reindexing_same_chunk_overwrites_not_duplicates() -> None:
    store = _store()
    store.ensure_collection()
    chunk = _chunk()

    store.upsert_chunks(_REPO, "app/foo.py", [chunk], [[1.0, 0.0, 0.0]])
    # 같은 파일의 같은 위치를 다른 벡터로 재인덱싱 — 새 point가 아니라 덮어써야 한다
    store.upsert_chunks(_REPO, "app/foo.py", [chunk], [[0.0, 1.0, 0.0]])

    results = store.search(_REPO, [0.0, 1.0, 0.0], limit=10)
    assert len(results) == 1


def test_upsert_chunks_raises_on_length_mismatch() -> None:
    store = _store()
    store.ensure_collection()
    with pytest.raises(ValueError):
        store.upsert_chunks(_REPO, "app/foo.py", [_chunk()], [])


def test_upsert_chunks_noop_on_empty_list() -> None:
    store = _store()
    store.ensure_collection()
    store.upsert_chunks(_REPO, "app/foo.py", [], [])  # 에러 없이 통과해야 한다


def test_search_returns_empty_when_collection_empty() -> None:
    store = _store()
    store.ensure_collection()
    assert store.search(_REPO, [1.0, 0.0, 0.0]) == []


def test_delete_by_file_removes_only_that_files_points() -> None:
    store = _store()
    store.ensure_collection()
    store.upsert_chunks(_REPO, "a.py", [_chunk()], [[1.0, 0.0, 0.0]])
    store.upsert_chunks(_REPO, "b.py", [_chunk()], [[0.0, 1.0, 0.0]])

    store.delete_by_file(_REPO, "a.py")

    remaining = store.search(_REPO, [0.0, 1.0, 0.0], limit=10)
    assert [r.file_path for r in remaining] == ["b.py"]


def test_delete_by_file_then_reindex_drops_stale_points_at_shifted_positions() -> None:
    # 함수 위치가 바뀌는(라인 번호가 밀리는) 재인덱싱 상황을 흉내낸다: 이전엔
    # start_line=1이었던 chunk가, 위에 코드가 추가되면서 start_line=5로 밀린 경우.
    # delete_by_file 없이 upsert만 했다면 두 point가 별개 id를 가져 둘 다 남는다.
    store = _store()
    store.ensure_collection()
    old_chunk = _chunk(start_line=1, end_line=3)
    store.upsert_chunks(_REPO, "a.py", [old_chunk], [[1.0, 0.0, 0.0]])

    store.delete_by_file(_REPO, "a.py")
    new_chunk = _chunk(start_line=5, end_line=7)
    store.upsert_chunks(_REPO, "a.py", [new_chunk], [[0.0, 1.0, 0.0]])

    results = store.search(_REPO, [0.0, 1.0, 0.0], limit=10)
    assert len(results) == 1
    assert results[0].start_line == 5


def test_search_does_not_leak_across_repositories() -> None:
    # 이 서버는 여러 레포를 같은 collection에 인덱싱한다 — repository_id로
    # 스코핑하지 않으면 A 레포 PR 리뷰에 B 레포 코드가 섞여 들어간다.
    store = _store()
    store.ensure_collection()
    same_path_chunk = _chunk()

    store.upsert_chunks(1, "app/main.py", [same_path_chunk], [[1.0, 0.0, 0.0]])
    store.upsert_chunks(2, "app/main.py", [same_path_chunk], [[1.0, 0.0, 0.0]])

    repo1_results = store.search(1, [1.0, 0.0, 0.0], limit=10)
    repo2_results = store.search(2, [1.0, 0.0, 0.0], limit=10)

    assert len(repo1_results) == 1
    assert len(repo2_results) == 1


def test_delete_by_file_only_affects_matching_repository() -> None:
    store = _store()
    store.ensure_collection()
    chunk = _chunk()
    store.upsert_chunks(1, "a.py", [chunk], [[1.0, 0.0, 0.0]])
    store.upsert_chunks(2, "a.py", [chunk], [[1.0, 0.0, 0.0]])

    store.delete_by_file(1, "a.py")

    assert store.search(1, [1.0, 0.0, 0.0], limit=10) == []
    assert len(store.search(2, [1.0, 0.0, 0.0], limit=10)) == 1
