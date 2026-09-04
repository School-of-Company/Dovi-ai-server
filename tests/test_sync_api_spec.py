from qdrant_client import QdrantClient

from app.notion.schema import ApiSpecEntry
from app.rag.api_spec_vector_store import ApiSpecVectorStore
from scripts.sync_api_spec import _extract_database_id, sync_all


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


_DB_1 = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c301"
_DB_2 = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c302"
_EMPTY_DB = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c303"


async def test_sync_all_upserts_entries_for_every_known_repository() -> None:
    entries = [ApiSpecEntry(method="GET", path="/x", summary="s")]
    notion = FakeNotionClient({_DB_1: entries})
    link_store = FakeLinkStore([(42, _DB_1)])
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
    link_store = FakeLinkStore([(42, _EMPTY_DB)])
    vector_store = ApiSpecVectorStore(QdrantClient(location=":memory:"), "api_spec", vector_size=3)

    total = await sync_all(
        link_store=link_store,
        notion_client=notion,
        embedder=FakeEmbedder(),
        vector_store=vector_store,
    )

    assert total == 0


async def test_sync_all_preserves_existing_data_when_notion_returns_empty() -> None:
    """Notion 일시 장애(500/timeout/rate limit 등)로 query_database가 빈 리스트를
    반환해도, 그것이 delete_by_repository를 트리거해서는 안 된다 — 기존 데이터가
    그대로 남아있어야 한다."""
    entries = [ApiSpecEntry(method="GET", path="/x", summary="s")]
    notion = FakeNotionClient({_DB_1: entries})
    vector_store = ApiSpecVectorStore(QdrantClient(location=":memory:"), "api_spec", vector_size=3)

    # 1차 sync: 정상적으로 entries가 upsert된다.
    first_total = await sync_all(
        link_store=FakeLinkStore([(42, _DB_1)]),
        notion_client=notion,
        embedder=FakeEmbedder(),
        vector_store=vector_store,
    )
    assert first_total == 1
    assert len(vector_store.search(42, [1.0, 0.0, 0.0], limit=10)) == 1

    # 2차 sync: Notion이 일시 장애로 빈 리스트를 반환한다 (FakeNotionClient에서
    # db-1을 찾지 못하는 상황으로 시뮬레이션).
    failing_notion = FakeNotionClient({})
    second_total = await sync_all(
        link_store=FakeLinkStore([(42, _DB_1)]),
        notion_client=failing_notion,
        embedder=FakeEmbedder(),
        vector_store=vector_store,
    )

    assert second_total == 0
    # 기존에 upsert된 데이터가 삭제되지 않고 남아있어야 한다.
    assert len(vector_store.search(42, [1.0, 0.0, 0.0], limit=10)) == 1


async def test_sync_all_isolates_per_repository_failure() -> None:
    entries_ok = [ApiSpecEntry(method="GET", path="/y", summary="s")]
    notion = FailingNotionClient(
        {_DB_1: entries_ok, _DB_2: entries_ok}, failing_database_id=_DB_1
    )
    link_store = FakeLinkStore([(1, _DB_1), (2, _DB_2)])
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


def test_extract_database_id_handles_realistic_notion_copy_link_url() -> None:
    url = "https://www.notion.so/myworkspace/API-Spec-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4?v=1234567890abcdef1234567890abcdef"

    database_id = _extract_database_id(url)

    assert database_id == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"


def test_extract_database_id_handles_dashed_uuid_format() -> None:
    url = "https://www.notion.so/myworkspace/API-Spec-a1b2c3d4-e5f6-a1b2-c3d4-e5f6a1b2c3d4?v=xyz"

    database_id = _extract_database_id(url)

    assert database_id == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"


def test_extract_database_id_returns_none_for_url_without_id() -> None:
    assert _extract_database_id("https://www.notion.so/myworkspace/not-a-valid-page") is None


async def test_sync_all_skips_repository_with_unparseable_url_without_raising() -> None:
    entries_ok = [ApiSpecEntry(method="GET", path="/y", summary="s")]
    notion = FakeNotionClient({_DB_2: entries_ok})
    link_store = FakeLinkStore(
        [(1, "https://www.notion.so/myworkspace/not-a-valid-page"), (2, _DB_2)]
    )
    vector_store = ApiSpecVectorStore(QdrantClient(location=":memory:"), "api_spec", vector_size=3)

    total = await sync_all(
        link_store=link_store,
        notion_client=notion,
        embedder=FakeEmbedder(),
        vector_store=vector_store,
    )

    assert total == 1
    assert len(vector_store.search(2, [1.0, 0.0, 0.0], limit=10)) == 1
