class LLMOutputTruncatedError(ValueError):
    """출력이 max_tokens에 걸려 잘려 파싱에 실패했다(finish_reason == "length").

    ValueError를 상속해, 이 예외를 따로 분기하지 않는 기존 호출자(예:
    comment_answer 파이프라인)는 오늘과 동일하게 `except ValueError`로 잡는다.

    raw_content에 잘린 원문을 담아, 호출자(ReviewPipeline)가 부분 복구를
    시도할 수 있게 한다(이슈 #99).
    """

    def __init__(self, message: str, *, raw_content: str) -> None:
        super().__init__(message)
        self.raw_content = raw_content
