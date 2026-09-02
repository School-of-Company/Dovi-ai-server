from app.review.schema import CamelModel


class ThreadComment(CamelModel):
    comment_id: int
    author: str
    body: str
    created_at: str


class CommentAnswerRequestedEvent(CamelModel):
    comment_job_id: str
    repository_id: int
    pr_number: int
    path: str
    line: int | None = None
    diff_hunk: str
    thread: list[ThreadComment] = []


class CommentAnswerCompletedEvent(CamelModel):
    comment_job_id: str
    answer: str


class CommentAnswerFailedEvent(CamelModel):
    comment_job_id: str
    reason: str
