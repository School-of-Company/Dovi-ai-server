from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from app.rag.vector_store import QdrantVectorStore
from scripts.index_repo import index_directory, iter_source_files


class FakeEmbedder:
    def __init__(self, dimension: int = 4) -> None:
        self._dimension = dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t))] * self._dimension for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return [float(len(text))] * self._dimension

    @property
    def dimension(self) -> int:
        return self._dimension


def _vector_store(dimension: int = 4) -> QdrantVectorStore:
    client = QdrantClient(location=":memory:")
    return QdrantVectorStore(client, "test_collection", vector_size=dimension)


def test_iter_source_files_finds_supported_language_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def a(): pass\n")
    (tmp_path / "b.ts").write_text("function b() {}\n")
    (tmp_path / "README.md").write_text("# readme\n")

    found = {p.name for p in iter_source_files(tmp_path)}
    assert found == {"a.py", "b.ts"}


def test_iter_source_files_skips_excluded_and_hidden_dirs(tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("function x() {}\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hook.py").write_text("x = 1\n")
    (tmp_path / "app.py").write_text("def real(): pass\n")

    found = {p.name for p in iter_source_files(tmp_path)}
    assert found == {"app.py"}


def test_index_directory_indexes_chunks_into_vector_store(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("def bar():\n    return 1\n")

    embedder = FakeEmbedder()
    store = _vector_store()

    total = index_directory(tmp_path, embedder=embedder, vector_store=store)

    assert total == 1
    results = store.search(embedder.embed_query("bar"), limit=5)
    assert len(results) == 1
    assert results[0].file_path == "foo.py"
    assert results[0].name == "bar"


def test_index_directory_skips_files_with_no_extractable_chunks(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# hi\n")
    (tmp_path / "no_functions.py").write_text("import os\n")

    total = index_directory(tmp_path, embedder=FakeEmbedder(), vector_store=_vector_store())

    assert total == 0


def test_index_directory_reruns_drop_stale_chunks_at_shifted_positions(tmp_path: Path) -> None:
    # 첫 실행: 파일 맨 위에 함수가 있음(start_line=1 근처)
    target = tmp_path / "foo.py"
    target.write_text("def bar():\n    return 1\n")
    embedder = FakeEmbedder()
    store = _vector_store()
    index_directory(tmp_path, embedder=embedder, vector_store=store)

    # 두 번째 실행: 위에 코드가 추가돼 함수 위치가 아래로 밀림
    target.write_text("import os\n\n\ndef bar():\n    return os.getcwd()\n")
    index_directory(tmp_path, embedder=embedder, vector_store=store)

    results = store.search(embedder.embed_query("bar"), limit=10)
    # 재인덱싱 후에도 같은 함수의 point가 하나만 남아야 한다 (밀린 위치의 stale
    # point가 옛 위치에 남아 중복되면 안 된다)
    bar_results = [r for r in results if r.name == "bar"]
    assert len(bar_results) == 1
    assert bar_results[0].start_line == 4


def test_index_directory_skips_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "foo.py").write_text("def bar(): pass\n")

    def _boom(self: Path, encoding: str | None = None) -> str:
        raise UnicodeDecodeError("utf-8", b"", 0, 1, "boom")

    monkeypatch.setattr(Path, "read_text", _boom)

    total = index_directory(tmp_path, embedder=FakeEmbedder(), vector_store=_vector_store())

    assert total == 0
