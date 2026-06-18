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

_GENERATED_DIRS = (
    "node_modules/",
    "vendor/",
    "dist/",
    "build/",
    "__pycache__/",
    ".venv/",
)


def _extension(name: str) -> str:
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def _should_skip(file: ChangedFile) -> bool:
    if file.status == "removed":
        return True
    if not file.patch.strip():
        return True

    path = file.file_path
    name = path.rsplit("/", 1)[-1]

    if name in _LOCKFILES:
        return True
    if _extension(name) in _BINARY_EXT:
        return True
    if path.endswith(_GENERATED_SUFFIXES):
        return True
    if any(directory in path for directory in _GENERATED_DIRS):
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


def analyze(event: ReviewRequestedEvent) -> list[ReviewTarget]:
    targets: list[ReviewTarget] = []
    for file in event.changed_files:
        if _should_skip(file):
            continue
        targets.append(
            ReviewTarget(
                file_path=file.file_path,
                status=file.status,
                hunks=_split_hunks(file.patch),
            )
        )
    return targets
