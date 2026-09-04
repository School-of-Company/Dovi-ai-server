from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ApiSpecSearchResult:
    method: str
    path: str
    summary: str
    request_schema: str
    response_schema: str
    auth: str
    score: float
