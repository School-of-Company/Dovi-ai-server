import asyncio
import logging

from pydantic import ValidationError

from app.kafka.consumer import MessageSource
from app.rag.embeddings import Embedder
from app.rag.schema import IndexChangedFile, RepoIndexRequestedEvent
from app.rag.vector_store import QdrantVectorStore
from app.review.chunking import extract_all_chunks, merge_small_chunks

logger = logging.getLogger(__name__)


class RepoIndexConsumer:
    """repo.index.requested 이벤트를 소비해 push된 변경 파일만 증분 재인덱싱한다.

    scripts/index_repo.py의 부트스트랩 전체 인덱싱과 동일한 delete-then-upsert
    패턴을 파일 단위로 적용한다 — upsert만으로는 chunk 위치가 바뀌거나 chunk 수가
    줄어들 때 예전 point가 안 지워지기 때문이다.

    renamed 파일은 github-app 페이로드에 이전 경로 정보가 없어(IndexChangedFile은
    새 경로만 가짐), 이전 경로의 예전 point는 정리하지 못한다 — 다음 전체
    재인덱싱(scripts/index_repo.py) 때까지 orphan으로 남는 알려진 한계다.

    best-effort: 파일 하나의 청킹/임베딩이 실패해도 나머지 파일은 계속 처리한다
    (RAG 인덱스 최신화 실패가 리뷰 자체를 막으면 안 된다).
    """

    def __init__(
        self,
        source: MessageSource,
        embedder: Embedder,
        vector_store: QdrantVectorStore,
    ) -> None:
        self._source = source
        self._embedder = embedder
        self._vector_store = vector_store

    async def run(self, shutdown: asyncio.Event | None = None) -> None:
        async for message in self._source:
            await self.handle(message.value)
            await self._source.commit()
            if shutdown is not None and shutdown.is_set():
                return

    async def handle(self, raw: bytes) -> None:
        try:
            event = RepoIndexRequestedEvent.model_validate_json(raw)
        except ValidationError:
            logger.exception("invalid RepoIndexRequestedEvent payload, skipping")
            return

        for changed_file in event.changed_files:
            try:
                await self._reindex_file(event.repository_id, changed_file)
            except Exception:
                logger.warning(
                    "failed to reindex file, skipping repositoryId=%s filePath=%s",
                    event.repository_id,
                    changed_file.file_path,
                    exc_info=True,
                )

    async def _reindex_file(self, repository_id: int, changed_file: IndexChangedFile) -> None:
        # 재인덱싱 시 이 파일의 예전 point를 항상 먼저 비운다 — removed 파일은
        # 이걸로 끝이고, modified/added/renamed 파일은 재삽입 전에 stale point를
        # 지워야 chunk 위치가 바뀌거나 chunk 수가 줄었을 때도 안전하다.
        self._vector_store.delete_by_file(repository_id, changed_file.file_path)

        if changed_file.status == "removed" or changed_file.content is None:
            return

        chunks = extract_all_chunks(changed_file.file_path, changed_file.content)
        if chunks is None:
            return
        chunks = merge_small_chunks(chunks)

        vectors = self._embedder.embed_documents([chunk.source for chunk in chunks])
        self._vector_store.upsert_chunks(
            repository_id, changed_file.file_path, chunks, vectors
        )
        logger.info(
            "reindexed file repositoryId=%s filePath=%s chunks=%d",
            repository_id,
            changed_file.file_path,
            len(chunks),
        )
