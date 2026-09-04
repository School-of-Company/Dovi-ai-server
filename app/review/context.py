import logging
import re
from pathlib import PurePosixPath

from app.review.schema import ContextFile

logger = logging.getLogger(__name__)

_SECRET_EXTENSIONS = {".env", ".pem", ".p8", ".key"}
_SECRET_KEYWORDS = ("private-key", "private_key")

_MAX_FILE_CHARS = 8000
_MAX_TOTAL_CHARS = 20000

_OPENAPI_FILENAMES = {"openapi.yaml", "openapi.yml", "swagger.json"}
_NOTION_LINK_PATTERN = re.compile(
    r"Notion API Spec:\s*(https://(?:www\.)?notion\.so/\S+)", re.IGNORECASE
)


def has_openapi_spec(context_files: list[ContextFile]) -> bool:
    """swagger/openapi가 이미 있으면 Notion API 명세 fallback을 쓰지 않는다."""
    return any(
        PurePosixPath(f.path.lower()).name in _OPENAPI_FILENAMES for f in context_files
    )


def extract_notion_api_spec_link(context_files: list[ContextFile]) -> str | None:
    """DOVI.md의 '## API Specification' 섹션에서 Notion 링크를 찾는다.

    swagger가 있는지 여부는 호출자(review pipeline)가 has_openapi_spec()로 먼저
    판단해서, 이 함수는 링크 파싱 자체에만 집중한다 (단일 책임).
    """
    dovi = next(
        (f for f in context_files if PurePosixPath(f.path.lower()).name == "dovi.md"),
        None,
    )
    if dovi is None:
        return None
    match = _NOTION_LINK_PATTERN.search(dovi.content)
    return match.group(1) if match else None


def _is_secret(path: str) -> bool:
    pure = PurePosixPath(path.lower())
    if "secrets" in pure.parts:
        return True
    if any(suffix in _SECRET_EXTENSIONS for suffix in pure.suffixes):
        return True
    name = pure.name
    if name == ".env" or name.startswith(".env."):
        return True
    if any(keyword in name for keyword in _SECRET_KEYWORDS):
        return True
    return False


# docs/superpowers/{plans,specs}는 SDD 워크플로우가 남기는 "이 기능을 어떻게
# 만들지"에 대한 계획/설계 문서다 — "이 레포 코드가 지금 어떻게 동작하는지"에
# 대한 참고 자료가 아니다. 리뷰 대상 diff가 빈약할 때(예: YAML/셸 스크립트 몇
# 줄) LLM이 이 문서에 적힌 계획 중인 함수/파일명을 실제 diff에서 구현된 것으로
# 착각해 summary에 써버리는 사고가 실제로 재발했다 (프롬프트 지침만으로는
# 확실히 막히지 않음) — 아예 리뷰 컨텍스트 후보에서 제외한다.
_EXCLUDED_PREFIXES = ("docs/superpowers/",)


def _is_excluded(path: str) -> bool:
    lowered = path.lower()
    return any(lowered.startswith(prefix) for prefix in _EXCLUDED_PREFIXES)


def _priority(path: str) -> int:
    lowered = path.lower()
    name = PurePosixPath(lowered).name
    if name == "dovi.md":
        return 0
    if name in ("openapi.yaml", "openapi.yml", "swagger.json"):
        return 1
    if lowered == "readme.md":
        return 2
    if lowered.startswith("docs/") or "/docs/" in lowered:
        return 3
    if name == "claude.md":
        return 4
    return 5


def build_context(
    context_files: list[ContextFile],
    *,
    max_file_chars: int = _MAX_FILE_CHARS,
    max_total_chars: int = _MAX_TOTAL_CHARS,
) -> str:
    usable = [
        f for f in context_files if not _is_secret(f.path) and not _is_excluded(f.path)
    ]
    usable.sort(key=lambda f: _priority(f.path))

    blocks: list[str] = []
    total = 0
    for i, file in enumerate(usable):
        header = f"# {file.path}\n"
        remaining = max_total_chars - total - len(header)
        if remaining <= 0:
            logger.warning(
                "project context truncated: dropping %d remaining file(s)", len(usable) - i
            )
            break

        limit = min(max_file_chars, remaining)
        if len(file.content) > limit:
            trunc_msg = "\n...(truncated)"
            if limit < len(trunc_msg):
                logger.warning(
                    "project context truncated: dropping %d remaining file(s)", len(usable) - i
                )
                break
            content_limit = limit - len(trunc_msg)
            content = file.content[:content_limit] + trunc_msg
            if limit == max_file_chars:
                logger.warning(
                    "project context truncated: file=%s exceeded %d chars",
                    file.path,
                    max_file_chars,
                )
            else:
                logger.warning(
                    "project context truncated: file=%s shared budget exhausted "
                    "(%d chars remaining)",
                    file.path,
                    remaining,
                )
        else:
            content = file.content

        block = header + content
        blocks.append(block)
        total += len(block)

    return "\n\n".join(blocks)
