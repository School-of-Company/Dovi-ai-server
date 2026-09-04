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

from app.notion.schema import ApiSpecEntry
from app.rag.api_spec_schema import ApiSpecSearchResult

logger = logging.getLogger(__name__)

_POINT_ID_NAMESPACE = uuid.UUID("9f1c1a3e-2b7d-4e6a-8c3f-1a2b3c4d5e6f")


def _point_id(repository_id: int, entry: ApiSpecEntry) -> str:
    key = f"{repository_id}:{entry.method}:{entry.path}"
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, key))


class ApiSpecVectorStore:
    def __init__(self, client: QdrantClient, collection_name: str, vector_size: int) -> None:
        self._client = client
        self._collection_name = collection_name
        self._vector_size = vector_size

    def ensure_collection(self) -> None:
        if self._client.collection_exists(self._collection_name):
            return
        self._client.create_collection(
            collection_name=self._collection_name,
            vectors_config=VectorParams(size=self._vector_size, distance=Distance.COSINE),
        )
        self._client.create_payload_index(
            collection_name=self._collection_name,
            field_name="repositoryId",
            field_schema=PayloadSchemaType.INTEGER,
        )

    def upsert_entries(
        self, repository_id: int, entries: list[ApiSpecEntry], vectors: list[list[float]]
    ) -> None:
        if len(entries) != len(vectors):
            raise ValueError("entries와 vectors의 개수가 일치해야 한다")
        if not entries:
            return
        points = [
            PointStruct(
                id=_point_id(repository_id, entry),
                vector=vector,
                payload={
                    "repositoryId": repository_id,
                    "method": entry.method,
                    "path": entry.path,
                    "summary": entry.summary,
                    "requestSchema": entry.request_schema,
                    "responseSchema": entry.response_schema,
                    "auth": entry.auth,
                },
            )
            for entry, vector in zip(entries, vectors)
        ]
        self._client.upsert(collection_name=self._collection_name, points=points)

    def delete_by_repository(self, repository_id: int) -> None:
        self._client.delete(
            collection_name=self._collection_name,
            points_selector=Filter(
                must=[FieldCondition(key="repositoryId", match=MatchValue(value=repository_id))]
            ),
        )

    def search(
        self, repository_id: int, query_vector: list[float], *, limit: int = 5
    ) -> list[ApiSpecSearchResult]:
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
                ApiSpecSearchResult(
                    method=payload["method"],
                    path=payload["path"],
                    summary=payload["summary"],
                    request_schema=payload["requestSchema"],
                    response_schema=payload["responseSchema"],
                    auth=payload["auth"],
                    score=point.score,
                )
            )
        return results
