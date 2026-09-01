import logging


def configure_logging(level: str = "INFO") -> None:
    """앱 전역 로깅을 설정한다.

    uvicorn은 자신의 uvicorn.* 로거만 설정하고 app.* 로거는 건드리지 않으므로,
    이 호출 없이는 루트 로거 기본 레벨(WARNING)에 막혀 info 로그가 출력되지 않는다.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
