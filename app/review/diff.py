from pathlib import PurePosixPath

from app.review.chunking import extract_context_chunks
from app.review.schema import ChangedFile, ReviewRequestedEvent, ReviewTarget

_LOCKFILES = {
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "go.sum",
    "Gemfile.lock",
    "composer.lock",
}

_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".svg",
    ".pdf", ".zip", ".gz", ".tar", ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".mp3", ".jar", ".so", ".dylib", ".dll", ".bin", ".wasm",
}

_GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", "_pb2.py", ".pb.go")

_GENERATED_DIRS = {
    "node_modules",
    "vendor",
    "dist",
    "build",
    "__pycache__",
    ".venv",
}


def _should_skip(file: ChangedFile) -> bool:
    if file.status == "removed":
        return True
    if not file.patch.strip():
        return True

    path = PurePosixPath(file.file_path)

    if path.name in _LOCKFILES:
        return True
    if path.suffix.lower() in _BINARY_EXT:
        return True
    if file.file_path.endswith(_GENERATED_SUFFIXES):
        return True
    if set(path.parts) & _GENERATED_DIRS:
        return True
    return False


def _split_hunks(patch: str) -> list[str]:
    hunks: list[str] = []
    current: list[str] = []
    for line in patch.splitlines():
        if line.startswith("@@"):
            if current:
                hunks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        hunks.append("\n".join(current))
    return hunks


def _build_context_chunks(file: ChangedFile) -> list[str]:
    # content(변경 후 파일 전체)가 있어야 AST를 파싱할 수 있다. 없으면 hunks만으로
    # 리뷰가 진행된다 (1단계 MVP 동작과 동일).
    if not file.content:
        return []
    chunks = extract_context_chunks(file.file_path, file.content, file.patch)
    if not chunks:
        return []
    return [
        f"### {c.node_type} {c.name or ''} (lines {c.start_line}-{c.end_line})\n{c.source}"
        for c in chunks
    ]


def analyze(event: ReviewRequestedEvent) -> list[ReviewTarget]:
    targets: list[ReviewTarget] = []
    for file in event.changed_files:
        if _should_skip(file):
            continue
        hunks = _split_hunks(file.patch)
        if not hunks:
            continue
        targets.append(
            ReviewTarget(
                file_path=file.file_path,
                status=file.status,
                hunks=hunks,
                context_chunks=_build_context_chunks(file),
            )
        )
    return targets
