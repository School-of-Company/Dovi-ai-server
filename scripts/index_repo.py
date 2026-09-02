"""로컬 경로의 레포를 청킹해 Qdrant에 인덱싱하는 부트스트랩 CLI.

레포를 처음 연동할 때 1회성으로 실행한다 (전체 인덱싱). 이후 증분 업데이트는
push 이벤트 기반 컨슈머가 담당한다 (노션 "repo 인덱싱(RAG) 트리거 설계" 참고).

재실행하면 이번에 순회한 파일들은 예전 point를 지우고 다시 넣지만(재인덱싱
안전), 지난 실행 이후 레포에서 아예 삭제된 파일의 point는 이번 순회 대상에
없으므로 지워지지 않는다 — 그런 정리(prune)까지 필요하면 컬렉션을 비우고
처음부터 다시 실행한다.

사용법:
    uv run python scripts/index_repo.py --path /path/to/cloned/repo
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from qdrant_client import QdrantClient

from app.core.config import get_settings
from app.rag.embeddings import CodeRankEmbedClient, Embedder
from app.rag.vector_store import QdrantVectorStore
from app.review.chunking import detect_language, extract_all_chunks, merge_small_chunks

logger = logging.getLogger(__name__)

_EXCLUDED_DIR_NAMES = {
    "node_modules",
    "dist",
    "build",
    "target",
    "vendor",
    "venv",
    "__pycache__",
    "mypy_cache",
    "pytest_cache",
    "ruff_cache",
}


def iter_source_files(root: Path) -> list[Path]:
    """root 아래에서 chunking이 지원하는 언어의 파일 경로를 모두 찾는다.

    숨김 디렉터리(.git 등)와 generated/vendor성 디렉터리는 건너뛴다.
    """
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in _EXCLUDED_DIR_NAMES
        ]
        for filename in filenames:
            path = Path(dirpath) / filename
            if detect_language(str(path)) is not None:
                files.append(path)
    return files


def index_directory(
    root: Path,
    *,
    embedder: Embedder,
    vector_store: QdrantVectorStore,
    min_chars: int = 200,
) -> int:
    """root 아래 지원 언어 파일을 전부 청킹해 vector_store에 적재한다.

    반환값은 인덱싱된 chunk 총 개수.
    """
    vector_store.ensure_collection()
    total_chunks = 0

    for path in iter_source_files(root):
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            logger.warning("failed to read file, skipping path=%s", path)
            continue

        relative_path = str(path.relative_to(root))
        chunks = extract_all_chunks(relative_path, content)

        # 재인덱싱 시 이 파일의 예전 point를 항상 먼저 비운다 — chunk 위치가
        # 바뀌거나 함수가 삭제되면 upsert만으로는 예전 point가 안 지워진다.
        vector_store.delete_by_file(relative_path)
        if chunks is None:
            continue
        chunks = merge_small_chunks(chunks, min_chars=min_chars)

        vectors = embedder.embed_documents([chunk.source for chunk in chunks])
        vector_store.upsert_chunks(relative_path, chunks, vectors)
        total_chunks += len(chunks)
        logger.info("indexed file=%s chunks=%d", relative_path, len(chunks))

    return total_chunks


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="로컬 레포를 Qdrant에 인덱싱한다")
    parser.add_argument("--path", required=True, help="인덱싱할 로컬 레포 경로")
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"디렉토리를 찾을 수 없음: {root}", file=sys.stderr)
        sys.exit(1)

    settings = get_settings()
    embedder = CodeRankEmbedClient(settings.embedding_model)
    client = QdrantClient(url=settings.qdrant_url)
    vector_store = QdrantVectorStore(
        client, settings.rag_collection_name, vector_size=embedder.dimension
    )

    total = index_directory(root, embedder=embedder, vector_store=vector_store)
    print(f"인덱싱 완료: {total}개 chunk")


if __name__ == "__main__":
    main()
