from qdrant_client import QdrantClient

from app.notion.schema import ApiSpecEntry
from app.rag.api_spec_vector_store import ApiSpecVectorStore
from scripts.sync_api_spec import sync_all


class FakeEmbedder:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t)), 0.0, 0.0] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return [float(len(text)), 0.0, 0.0]

    @property
    def dimension(self) -> int:
        return 3


class FakeNotionClient:
    def __init__(self, entries_by_db: dict[str, list[ApiSpecEntry]]) -> None:
        self._entries_by_db = entries_by_db

    async def query_database(self, database_id: str) -> list[ApiSpecEntry]:
        return self._entries_by_db.get(database_id, [])


class FailingNotionClient:
    """특정 database_id 조회 시 예외를 던지는 fake. 격리 테스트 전용."""

    def __init__(
        self, entries_by_db: dict[str, list[ApiSpecEntry]], failing_database_id: str
    ) -> None:
        self._entries_by_db = entries_by_db
        self._failing_database_id = failing_database_id

    async def query_database(self, database_id: str) -> list[ApiSpecEntry]:
        if database_id == self._failing_database_id:
            raise RuntimeError("notion query boom")
        return self._entries_by_db.get(database_id, [])


class FakeLinkStore:
    def __init__(self, links: list[tuple[int, str]]) -> None:
        self._links = links

    async def list_all(self) -> list[tuple[int, str]]:
        return self._links


async def test_sync_all_upserts_entries_for_every_known_repository() -> None:
    entries = [ApiSpecEntry(method="GET", path="/x", summary="s")]
    notion = FakeNotionClient({"db-1": entries})
    link_store = FakeLinkStore([(42, "db-1")])
    vector_store = ApiSpecVectorStore(QdrantClient(location=":memory:"), "api_spec", vector_size=3)

    total = await sync_all(
        link_store=link_store,
        notion_client=notion,
        embedder=FakeEmbedder(),
        vector_store=vector_store,
    )

    assert total == 1
    assert len(vector_store.search(42, [1.0, 0.0, 0.0], limit=10)) == 1


async def test_sync_all_skips_repository_with_no_entries() -> None:
    notion = FakeNotionClient({})
    link_store = FakeLinkStore([(42, "empty-db")])
    vector_store = ApiSpecVectorStore(QdrantClient(location=":memory:"), "api_spec", vector_size=3)

    total = await sync_all(
        link_store=link_store,
        notion_client=notion,
        embedder=FakeEmbedder(),
        vector_store=vector_store,
    )

    assert total == 0


async def test_sync_all_isolates_per_repository_failure() -> None:
    entries_ok = [ApiSpecEntry(method="GET", path="/y", summary="s")]
    notion = FailingNotionClient(
        {"db-1": entries_ok, "db-2": entries_ok}, failing_database_id="db-1"
    )
    link_store = FakeLinkStore([(1, "db-1"), (2, "db-2")])
    vector_store = ApiSpecVectorStore(QdrantClient(location=":memory:"), "api_spec", vector_size=3)

    total = await sync_all(
        link_store=link_store,
        notion_client=notion,
        embedder=FakeEmbedder(),
        vector_store=vector_store,
    )

    assert total == 1
    assert len(vector_store.search(1, [1.0, 0.0, 0.0], limit=10)) == 0
    assert len(vector_store.search(2, [1.0, 0.0, 0.0], limit=10)) == 1
