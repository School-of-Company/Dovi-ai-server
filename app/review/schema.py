from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

Severity = Literal["critical", "major", "minor", "suggestion"]
FileStatus = Literal["added", "modified", "removed", "renamed"]
FailureReason = Literal["parse_error", "timeout", "server_error"]


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ContextFile(CamelModel):
    path: str
    content: str
    source: str = "github"


class ChangedFile(CamelModel):
    file_path: str
    status: FileStatus
    patch: str = ""
    content: str | None = None
    previous_content: str | None = None


class ReviewRequestedEvent(CamelModel):
    review_job_id: str
    repository_id: int
    pr_number: int
    head_sha: str
    base_sha: str
    context_files: list[ContextFile] = []
    changed_files: list[ChangedFile] = []


class ReviewComment(CamelModel):
    severity: Severity
    confidence: float
    file_path: str
    line: int
    title: str
    message: str
    evidence: list[str] = []
    suggested_fix: str | None = None


class ReviewModelOutput(CamelModel):
    summary: str
    reviews: list[ReviewComment] = []


class ReviewCompletedEvent(CamelModel):
    review_job_id: str
    repository_id: int
    pr_number: int
    head_sha: str
    summary: str
    reviews: list[ReviewComment] = []
    model_version: str
    prompt_version: str


class ReviewFailedEvent(CamelModel):
    review_job_id: str
    head_sha: str
    reason: FailureReason


def make_review_job_id(repository_id: int, pr_number: int, head_sha: str) -> str:
    return f"{repository_id}:{pr_number}:{head_sha}"
