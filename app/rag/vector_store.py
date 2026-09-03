from __future__ import annotations

import logging
import uuid

from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.rag.schema import ChunkSearchResult
from app.review.chunking import AstChunk

logger = logging.getLogger(__name__)

# chunk의 point id를 결정론적으로 만들기 위한 고정 네임스페이스. 같은 파일의 같은
# 위치를 다시 인덱싱하면 같은 id가 나와 upsert가 새 point를 추가하지 않고 덮어쓴다.
_POINT_ID_NAMESPACE = uuid.UUID("6f8f7f2e-2b3a-4b8a-9b0e-7a1f6c9d2e3a")


def _point_id(repository_id: int, file_path: str, chunk: AstChunk) -> str:
    # repository_id가 빠지면 서로 다른 레포의 같은 상대경로 파일(예: app/main.py)이
    # 같은 point id로 충돌해 서로 덮어쓴다 — 이 서버 하나가 여러 레포를 동시에
    # 인덱싱/서빙하므로 반드시 키에 포함되어야 한다.
    key = (
        f"{repository_id}:{file_path}:{chunk.start_line}:{chunk.end_line}:"
        f"{chunk.node_type}:{chunk.name}"
    )
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, key))


class QdrantVectorStore:
    def __init__(self, client: QdrantClient, collection_name: str, vector_size: int) -> None:
        self._client = client
        self._collection_name = collection_name
        self._vector_size = vector_size

    def ensure_collection(self) -> None:
        if self._client.collection_exists(self._collection_name):
            return
        logger.info("creating qdrant collection=%s", self._collection_name)
        self._client.create_collection(
            collection_name=self._collection_name,
            vectors_config=VectorParams(size=self._vector_size, distance=Distance.COSINE),
        )
        # repositoryId에 payload index가 없으면 실제 서버에서 필터가 걸린 검색이
        # HNSW를 못 타고 전수 스캔으로 폴백된다 — 여러 레포가 쌓일수록 느려진다.
        self._client.create_payload_index(
            collection_name=self._collection_name,
            field_name="repositoryId",
            field_schema=PayloadSchemaType.INTEGER,
        )

    def upsert_chunks(
        self,
        repository_id: int,
        file_path: str,
        chunks: list[AstChunk],
        vectors: list[list[float]],
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks와 vectors의 개수가 일치해야 한다")
        if not chunks:
            return

        points = [
            PointStruct(
                id=_point_id(repository_id, file_path, chunk),
                vector=vector,
                payload={
                    "repositoryId": repository_id,
                    "filePath": file_path,
                    "nodeType": chunk.node_type,
                    "name": chunk.name,
                    "startLine": chunk.start_line,
                    "endLine": chunk.end_line,
                    "source": chunk.source,
                },
            )
            for chunk, vector in zip(chunks, vectors)
        ]
        self._client.upsert(collection_name=self._collection_name, points=points)

    def delete_by_file(self, repository_id: int, file_path: str) -> None:
        """해당 레포의 해당 파일에 속한 기존 point를 전부 지운다.

        point id는 (레포, 파일, 시작/끝 라인, ...)로 결정되므로, 파일을 재인덱싱할 때
        함수가 삭제되거나 위/아래 코드 변경으로 라인 번호가 밀리면 새 chunk는
        새 id를 받아 upsert만으로는 예전 point가 지워지지 않는다. 그래서 재인덱싱
        직전에 항상 이 메서드로 해당 파일의 point를 먼저 비워야 stale point가
        검색 결과에 계속 섞여 나오는 걸 막을 수 있다.
        """
        self._client.delete(
            collection_name=self._collection_name,
            points_selector=Filter(
                must=[
                    FieldCondition(key="repositoryId", match=MatchValue(value=repository_id)),
                    FieldCondition(key="filePath", match=MatchValue(value=file_path)),
                ]
            ),
        )

    def search(
        self, repository_id: int, query_vector: list[float], *, limit: int = 5
    ) -> list[ChunkSearchResult]:
        response = self._client.query_points(
            collection_name=self._collection_name,
            query=query_vector,
            limit=limit,
            query_filter=Filter(
                must=[FieldCondition(key="repositoryId", match=MatchValue(value=repository_id))]
            ),
        )
        results = []
        for point in response.points:
            payload = point.payload
            if payload is None:
                continue
            results.append(
                ChunkSearchResult(
                    file_path=payload["filePath"],
                    node_type=payload["nodeType"],
                    name=payload.get("name"),
                    start_line=payload["startLine"],
                    end_line=payload["endLine"],
                    source=payload["source"],
                    score=point.score,
                )
            )
        return results
