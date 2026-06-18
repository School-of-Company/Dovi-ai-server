from pathlib import PurePosixPath

from app.review.schema import ContextFile

_SECRET_EXTENSIONS = {".env", ".pem", ".p8", ".key"}
_SECRET_KEYWORDS = ("private-key", "private_key")

_MAX_FILE_CHARS = 8000
_MAX_TOTAL_CHARS = 20000


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
    usable = [f for f in context_files if not _is_secret(f.path)]
    usable.sort(key=lambda f: _priority(f.path))

    blocks: list[str] = []
    total = 0
    for file in usable:
        header = f"# {file.path}\n"
        remaining = max_total_chars - total - len(header)
        if remaining <= 0:
            break

        limit = min(max_file_chars, remaining)
        if len(file.content) > limit:
            trunc_msg = "\n...(truncated)"
            if limit < len(trunc_msg):
                break
            content_limit = limit - len(trunc_msg)
            content = file.content[:content_limit] + trunc_msg
        else:
            content = file.content

        block = header + content
        blocks.append(block)
        total += len(block)

    return "\n\n".join(blocks)
