from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChunkSearchResult:
    file_path: str
    node_type: str
    name: str | None
    start_line: int
    end_line: int
    source: str
    score: float
