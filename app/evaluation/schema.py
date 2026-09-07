from app.review.schema import CamelModel


class ReviewFeedbackEvent(CamelModel):
    review_job_id: str
    finding_index: int
    reflected: bool
    reason: str | None = None
