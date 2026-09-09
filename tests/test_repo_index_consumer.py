from collections.abc import AsyncIterator

import pytest
from qdrant_client import QdrantClient

from app.rag.index_consumer import RepoIndexConsumer
from app.rag.schema import IndexChangedFile, RepoIndexRequestedEvent
from app.rag.vector_store import QdrantVectorStore


class FakeMessage:
    def __init__(self, value: bytes) -> None:
        self.value = value


class FakeSource:
    def __init__(self, messages: list[FakeMessage]) -> None:
        self._messages = messages
        self.commit_count = 0

    async def __aiter__(self) -> AsyncIterator[FakeMessage]:
        for message in self._messages:
            yield message

    async def commit(self) -> None:
        self.commit_count += 1


class FakeEmbedder:
    def __init__(self, dimension: int = 4) -> None:
        self._dimension = dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t))] * self._dimension for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return [float(len(text))] * self._dimension

    @property
    def dimension(self) -> int:
        return self._dimension


class FailingEmbedder(FakeEmbedder):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("boom")


def _vector_store(dimension: int = 4) -> QdrantVectorStore:
    # 컬렉션은 scripts/index_repo.py의 부트스트랩 인덱싱이 이미 만들어둔 상태를
    # 전제한다(RAG_ENABLED는 부트스트랩 이후에만 켜짐) — 여기서 미리 만들어 그
    # 전제조건을 재현한다.
    client = QdrantClient(location=":memory:")
    store = QdrantVectorStore(client, "test_collection", vector_size=dimension)
    store.ensure_collection()
    return store


def _event_bytes(event: RepoIndexRequestedEvent) -> bytes:
    return event.model_dump_json(by_alias=True).encode("utf-8")


@pytest.mark.asyncio
async def test_handle_added_file_indexes_chunks_into_vector_store() -> None:
    embedder = FakeEmbedder()
    store = _vector_store()
    consumer = RepoIndexConsumer(FakeSource([]), embedder, store)

    event = RepoIndexRequestedEvent(
        repository_id=1,
        branch="main",
        head_sha="sha1",
        changed_files=[
            IndexChangedFile(
                file_path="foo.py",
                status="added",
                content="def bar():\n    return 1\n",
            )
        ],
    )

    await consumer.handle(_event_bytes(event))

    results = store.search(1, embedder.embed_query("bar"), limit=5)
    assert len(results) == 1
    assert results[0].file_path == "foo.py"
    assert results[0].name == "bar"


@pytest.mark.asyncio
async def test_handle_removed_file_deletes_existing_points() -> None:
    embedder = FakeEmbedder()
    store = _vector_store()
    consumer = RepoIndexConsumer(FakeSource([]), embedder, store)

    add_event = RepoIndexRequestedEvent(
        repository_id=1,
        branch="main",
        head_sha="sha1",
        changed_files=[
            IndexChangedFile(
                file_path="foo.py", status="added", content="def bar():\n    return 1\n"
            )
        ],
    )
    await consumer.handle(_event_bytes(add_event))
    assert len(store.search(1, embedder.embed_query("bar"), limit=5)) == 1

    remove_event = RepoIndexRequestedEvent(
        repository_id=1,
        branch="main",
        head_sha="sha2",
        changed_files=[IndexChangedFile(file_path="foo.py", status="removed")],
    )
    await consumer.handle(_event_bytes(remove_event))

    assert store.search(1, embedder.embed_query("bar"), limit=5) == []


@pytest.mark.asyncio
async def test_handle_modified_file_drops_stale_chunks_at_shifted_positions() -> None:
    embedder = FakeEmbedder()
    store = _vector_store()
    consumer = RepoIndexConsumer(FakeSource([]), embedder, store)

    original = RepoIndexRequestedEvent(
        repository_id=1,
        branch="main",
        head_sha="sha1",
        changed_files=[
            IndexChangedFile(
                file_path="foo.py",
                status="added",
                content="def bar():\n    return 1\n",
            )
        ],
    )
    await consumer.handle(_event_bytes(original))

    # 위에 함수를 하나 더 추가해서 bar()의 라인 위치가 밀리게 만든다 — upsert만으로는
    # 예전 위치의 point가 안 지워진다는 걸 검증하려는 것.
    modified = RepoIndexRequestedEvent(
        repository_id=1,
        branch="main",
        head_sha="sha2",
        changed_files=[
            IndexChangedFile(
                file_path="foo.py",
                status="modified",
                content="def new_first():\n    return 0\n\n\ndef bar():\n    return 1\n",
            )
        ],
    )
    await consumer.handle(_event_bytes(modified))

    # merge_small_chunks가 작은 두 함수를 한 chunk로 합치므로, 여기서 검증하려는
    # 것은 이름 분리가 아니라 예전 위치의 point가 새 point로 완전히 교체됐는지다
    # (delete_by_file 없이 upsert만 했다면 point가 2개 남는다).
    results = store.search(1, embedder.embed_query("bar"), limit=10)
    assert len(results) == 1
    assert "new_first" in results[0].source
    assert "bar" in results[0].source


@pytest.mark.asyncio
async def test_handle_file_with_no_extractable_chunks_only_deletes() -> None:
    embedder = FakeEmbedder()
    store = _vector_store()
    consumer = RepoIndexConsumer(FakeSource([]), embedder, store)

    event = RepoIndexRequestedEvent(
        repository_id=1,
        branch="main",
        head_sha="sha1",
        changed_files=[
            IndexChangedFile(file_path="README.md", status="added", content="# hi\n")
        ],
    )

    await consumer.handle(_event_bytes(event))

    assert store.search(1, embedder.embed_query("hi"), limit=5) == []


@pytest.mark.asyncio
async def test_handle_invalid_payload_does_not_raise() -> None:
    consumer = RepoIndexConsumer(FakeSource([]), FakeEmbedder(), _vector_store())

    await consumer.handle(b"not json")


@pytest.mark.asyncio
async def test_handle_continues_after_one_file_fails() -> None:
    store = _vector_store()
    consumer = RepoIndexConsumer(FakeSource([]), FailingEmbedder(), store)

    event = RepoIndexRequestedEvent(
        repository_id=1,
        branch="main",
        head_sha="sha1",
        changed_files=[
            IndexChangedFile(
                file_path="a.py", status="added", content="def a():\n    return 1\n"
            ),
            IndexChangedFile(
                file_path="b.py", status="added", content="def b():\n    return 2\n"
            ),
        ],
    )

    # 두 파일 모두 embed_documents가 예외를 던지지만, handle() 자체는 예외를
    # 전파하지 않고 두 파일 다 시도해야 한다(best-effort).
    await consumer.handle(_event_bytes(event))


@pytest.mark.asyncio
async def test_run_commits_after_each_message() -> None:
    embedder = FakeEmbedder()
    store = _vector_store()
    event = RepoIndexRequestedEvent(
        repository_id=1,
        branch="main",
        head_sha="sha1",
        changed_files=[
            IndexChangedFile(
                file_path="foo.py", status="added", content="def bar():\n    return 1\n"
            )
        ],
    )
    source = FakeSource([FakeMessage(_event_bytes(event)), FakeMessage(_event_bytes(event))])
    consumer = RepoIndexConsumer(source, embedder, store)

    await consumer.run()

    assert source.commit_count == 2
