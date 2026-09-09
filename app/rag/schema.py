from __future__ import annotations

from dataclasses import dataclass

from app.review.schema import CamelModel, FileStatus


@dataclass
class ChunkSearchResult:
    file_path: str
    node_type: str
    name: str | None
    start_line: int
    end_line: int
    source: str
    score: float


class IndexChangedFile(CamelModel):
    file_path: str
    status: FileStatus
    content: str | None = None


class RepoIndexRequestedEvent(CamelModel):
    repository_id: int
    branch: str
    head_sha: str
    changed_files: list[IndexChangedFile] = []
