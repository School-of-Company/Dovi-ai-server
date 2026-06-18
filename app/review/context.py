from pathlib import PurePosixPath

from app.review.schema import ContextFile

_SECRET_PATTERNS = (
    ".env",
    "secrets/",
    ".pem",
    ".p8",
    "private-key",
    "private_key",
    ".key",
)

_MAX_FILE_CHARS = 8000
_MAX_TOTAL_CHARS = 20000


def _is_secret(path: str) -> bool:
    lowered = path.lower()
    return any(pattern in lowered for pattern in _SECRET_PATTERNS)


def _priority(path: str) -> int:
    lowered = path.lower()
    name = PurePosixPath(lowered).name
    if name == "dovi.md":
        return 0
    if name in ("openapi.yaml", "openapi.yml", "swagger.json"):
        return 1
    if name == "readme.md":
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
    usable = [f for f in context_files if not _is_secret(f.path)]
    usable.sort(key=lambda f: _priority(f.path))

    blocks: list[str] = []
    total = 0
    for file in usable:
        content = file.content[:max_file_chars]
        if len(file.content) > max_file_chars:
            content += "\n...(truncated)"
        block = f"# {file.path}\n{content}"
        if total + len(block) > max_total_chars:
            break
        blocks.append(block)
        total += len(block)

    return "\n\n".join(blocks)
