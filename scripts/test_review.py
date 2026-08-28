"""
Kafka 없이 로컬 sample event로 리뷰 파이프라인을 실행하는 스크립트.

app 패키지를 import하므로 반드시 -m으로 모듈 실행해야 한다 (레포 루트에서).

사용법:
  uv run python -m scripts.test_review                     # fake LLM (네트워크 불필요)
  uv run python -m scripts.test_review --real               # 실제 llama-server 호출
  uv run python -m scripts.test_review --event path/to.json # 다른 샘플 이벤트 사용
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.core.config import get_settings
from app.llm.client import ChatMessage, LLMClient
from app.llm.openai_compatible_client import OpenAICompatibleLLMClient
from app.review.pipeline import ReviewPipeline
from app.review.schema import ReviewModelOutput, ReviewRequestedEvent

_DEFAULT_EVENT_PATH = (
    Path(__file__).resolve().parent.parent / "sample_events" / "pr_review_requested.json"
)


class FakeLLM:
    """네트워크 없이 파이프라인 흐름만 확인할 때 쓰는 고정 응답 LLM."""

    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int = 1500
    ) -> ReviewModelOutput:
        return ReviewModelOutput(
            summary="[FAKE] 잔액 검증 완화 및 과인출 가능성이 있습니다.",
            reviews=[
                {
                    "severity": "major",
                    "confidence": 0.9,
                    "filePath": "app/service/payment.py",
                    "line": 12,
                    "title": "[FAKE] 잔액 부족 체크 누락",
                    "message": "잔액 차감 전 충분한 잔액이 있는지 확인하지 않습니다.",
                    "evidence": ["balance -= amount"],
                }
            ],
        )


def _load_event(path: Path) -> ReviewRequestedEvent:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ReviewRequestedEvent.model_validate(data)


async def _run(event_path: Path, use_real: bool) -> None:
    settings = get_settings()
    event = _load_event(event_path)

    llm: LLMClient
    real_client: OpenAICompatibleLLMClient | None = None

    if use_real:
        real_client = OpenAICompatibleLLMClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
        llm = real_client
        model_version = settings.llm_model
    else:
        llm = FakeLLM()
        model_version = "fake"

    pipeline = ReviewPipeline(llm, model_version=model_version, prompt_version="v1")

    print(f"이벤트: {event_path}")
    print(f"리뷰 대상 파일: {len(event.changed_files)}개")
    print(f"LLM: {'실제 (' + settings.llm_base_url + ')' if use_real else 'fake'}")
    print()

    result = await pipeline.run(event)
    print(json.dumps(result.model_dump(by_alias=True), indent=2, ensure_ascii=False))

    if real_client is not None:
        await real_client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event", type=Path, default=_DEFAULT_EVENT_PATH, help="샘플 이벤트 JSON 경로"
    )
    parser.add_argument("--real", action="store_true", help="fake 대신 실제 LLM 서버 호출")
    args = parser.parse_args()

    if not args.event.exists():
        print(f"이벤트 파일을 찾을 수 없습니다: {args.event}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(_run(args.event, args.real))


if __name__ == "__main__":
    main()
