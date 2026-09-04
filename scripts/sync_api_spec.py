"""DOVI.md에서 발견된 Notion API 명세 DB를 주기적으로 Qdrant에 동기화하는 CLI.

review pipeline이 Redis(RedisNotionLinkStore)에 기록해둔 (repository_id, notion
database url) 목록을 읽어 Notion을 조회하고, 결과를 임베딩해 Qdrant에 저장한다.
PR 리뷰 요청 처리 경로에서는 이 스크립트를 호출하지 않는다 — 호스트 cron으로
주기 실행한다 (예: 매일 새벽 1회).

사용법:
    uv run python scripts/sync_api_spec.py
"""

import asyncio
import logging
from typing import Protocol

from qdrant_client import QdrantClient

from app.context.api_spec_link_store import RedisNotionLinkStore
from app.core.config import get_settings
from app.notion.client import NotionClient
from app.notion.schema import ApiSpecEntry
from app.rag.api_spec_vector_store import ApiSpecVectorStore
from app.rag.embeddings import CodeRankEmbedClient, Embedder
from app.review.dedup import create_redis_client  # 기존 redis client factory 재사용

logger = logging.getLogger(__name__)


class NotionQueryClient(Protocol):
    """sync_all이 필요로 하는 최소 인터페이스. 테스트에서 fake로 대체하기 위함."""

    async def query_database(self, database_id: str) -> list[ApiSpecEntry]: ...


class LinkSource(Protocol):
    """sync_all이 필요로 하는 최소 인터페이스. 테스트에서 fake로 대체하기 위함."""

    async def list_all(self) -> list[tuple[int, str]]: ...


async def sync_all(
    *,
    link_store: LinkSource,
    notion_client: NotionQueryClient,
    embedder: Embedder,
    vector_store: ApiSpecVectorStore,
) -> int:
    vector_store.ensure_collection()
    total = 0
    for repository_id, database_url in await link_store.list_all():
        database_id = database_url.rstrip("/").split("/")[-1]
        entries = await notion_client.query_database(database_id)
        vector_store.delete_by_repository(repository_id)
        if not entries:
            logger.info("no api spec entries repository_id=%s", repository_id)
            continue
        vectors = embedder.embed_documents([entry.to_text() for entry in entries])
        vector_store.upsert_entries(repository_id, entries, vectors)
        total += len(entries)
        logger.info("synced repository_id=%s entries=%d", repository_id, len(entries))
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()

    redis = create_redis_client(settings)
    # redis.asyncio.Redis의 실제 타입 스텁이 RedisLike보다 훨씬 넓어 구조적으로
    # 완전히 일치하지 않지만, set/get/keys를 문자열 인자로만 호출하므로 런타임에는 호환된다.
    link_store = RedisNotionLinkStore(redis)  # type: ignore[arg-type]
    notion_client = NotionClient(token=settings.notion_api_token)
    embedder = CodeRankEmbedClient(settings.embedding_model)
    qdrant_client = QdrantClient(url=settings.qdrant_url)
    vector_store = ApiSpecVectorStore(
        qdrant_client, settings.api_spec_collection_name, vector_size=embedder.dimension
    )

    total = asyncio.run(
        sync_all(
            link_store=link_store,
            notion_client=notion_client,
            embedder=embedder,
            vector_store=vector_store,
        )
    )
    print(f"동기화 완료: {total}개 endpoint")


if __name__ == "__main__":
    main()
