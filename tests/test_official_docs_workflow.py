from app.context import official_docs_workflow as odw
from app.context.npm_registry_client import DeprecationLookupResult
from app.context.official_docs_workflow import OfficialDocsWorkflow
from app.context.release_notes_cache import CachedReleaseNotes
from app.review.schema import ChangedFile

_AXIOS_BUMP_PATCH = """\
@@ -8472,9 +8473,9 @@
       }
     },
     "node_modules/axios": {
-      "version": "1.19.0",
-      "resolved": "https://registry.npmjs.org/axios/-/axios-1.19.0.tgz",
-      "integrity": "sha512-old==",
+      "version": "1.20.0",
+      "resolved": "https://registry.npmjs.org/axios/-/axios-1.20.0.tgz",
+      "integrity": "sha512-new==",
       "license": "MIT",
       "dependencies": {
         "follow-redirects": "^1.16.0",
"""


class FakeRegistryClient:
    def __init__(self, results: dict[tuple[str, str], DeprecationLookupResult]) -> None:
        self._results = results
        self.received: list[tuple[str, str]] = []

    async def check_deprecation(self, name: str, version: str) -> DeprecationLookupResult:
        self.received.append((name, version))
        return self._results.get((name, version), DeprecationLookupResult(ok=False, message=None))


class _Result:
    def __init__(self, ok: bool, notes: str | None) -> None:
        self.ok = ok
        self.notes = notes


class FakeReleaseClient:
    def __init__(self, results: dict[tuple[str, str], _Result]) -> None:
        self._results = results
        self.received: list[tuple[str, str, str]] = []

    async def find_release_notes(self, owner_repo: str, name: str, version: str) -> _Result:
        self.received.append((owner_repo, name, version))
        return self._results[(owner_repo, version)]


class FakeCache:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], CachedReleaseNotes] = {}
        self.set_calls: list[tuple[str, str, str | None]] = []

    async def get(self, owner_repo: str, version: str) -> CachedReleaseNotes | None:
        return self.store.get((owner_repo, version))

    async def set(self, owner_repo: str, version: str, notes: str | None) -> None:
        self.set_calls.append((owner_repo, version, notes))
        self.store[(owner_repo, version)] = CachedReleaseNotes(notes=notes)


async def test_build_evidence_includes_release_notes_for_bumped_dependency() -> None:
    registry = FakeRegistryClient(
        {
            ("axios", "1.20.0"): DeprecationLookupResult(
                ok=True, message=None, github_repo="axios/axios"
            )
        }
    )
    release_client = FakeReleaseClient(
        {("axios/axios", "1.20.0"): _Result(ok=True, notes="Fixed a security issue")}
    )
    workflow = OfficialDocsWorkflow(registry, release_client, FakeCache())
    changed_files = [
        ChangedFile(file_path="package-lock.json", status="modified", patch=_AXIOS_BUMP_PATCH)
    ]

    evidence = await workflow.build_evidence(changed_files)

    assert "axios@1.20.0" in evidence
    assert "Fixed a security issue" in evidence
    assert "공식 릴리즈 노트" in evidence


async def test_build_evidence_returns_empty_string_when_no_lockfile_changed() -> None:
    workflow = OfficialDocsWorkflow(FakeRegistryClient({}), FakeReleaseClient({}), FakeCache())
    changed_files = [ChangedFile(file_path="app/main.py", status="modified", patch="@@ -1 +1 @@")]

    assert await workflow.build_evidence(changed_files) == ""


async def test_build_evidence_skips_package_when_registry_has_no_github_repo() -> None:
    registry = FakeRegistryClient(
        {("axios", "1.20.0"): DeprecationLookupResult(ok=True, message=None, github_repo=None)}
    )
    workflow = OfficialDocsWorkflow(registry, FakeReleaseClient({}), FakeCache())
    changed_files = [
        ChangedFile(file_path="package-lock.json", status="modified", patch=_AXIOS_BUMP_PATCH)
    ]

    assert await workflow.build_evidence(changed_files) == ""


async def test_build_evidence_uses_cache_and_skips_release_client() -> None:
    registry = FakeRegistryClient(
        {
            ("axios", "1.20.0"): DeprecationLookupResult(
                ok=True, message=None, github_repo="axios/axios"
            )
        }
    )
    release_client = FakeReleaseClient({})  # 호출되면 KeyError로 즉시 실패 — 캐시 hit 검증용
    cache = FakeCache()
    cache.store[("axios/axios", "1.20.0")] = CachedReleaseNotes(notes="cached notes")
    workflow = OfficialDocsWorkflow(registry, release_client, cache)
    changed_files = [
        ChangedFile(file_path="package-lock.json", status="modified", patch=_AXIOS_BUMP_PATCH)
    ]

    evidence = await workflow.build_evidence(changed_files)

    assert "cached notes" in evidence
    assert release_client.received == []


async def test_build_evidence_does_not_cache_transient_release_client_failure() -> None:
    registry = FakeRegistryClient(
        {
            ("axios", "1.20.0"): DeprecationLookupResult(
                ok=True, message=None, github_repo="axios/axios"
            )
        }
    )
    release_client = FakeReleaseClient({("axios/axios", "1.20.0"): _Result(ok=False, notes=None)})
    cache = FakeCache()
    workflow = OfficialDocsWorkflow(registry, release_client, cache)
    changed_files = [
        ChangedFile(file_path="package-lock.json", status="modified", patch=_AXIOS_BUMP_PATCH)
    ]

    evidence = await workflow.build_evidence(changed_files)

    assert evidence == ""
    assert cache.set_calls == []


async def test_build_evidence_caches_confirmed_absent_notes() -> None:
    registry = FakeRegistryClient(
        {
            ("axios", "1.20.0"): DeprecationLookupResult(
                ok=True, message=None, github_repo="axios/axios"
            )
        }
    )
    release_client = FakeReleaseClient({("axios/axios", "1.20.0"): _Result(ok=True, notes=None)})
    cache = FakeCache()
    workflow = OfficialDocsWorkflow(registry, release_client, cache)
    changed_files = [
        ChangedFile(file_path="package-lock.json", status="modified", patch=_AXIOS_BUMP_PATCH)
    ]

    evidence = await workflow.build_evidence(changed_files)

    assert evidence == ""
    assert cache.set_calls == [("axios/axios", "1.20.0", None)]


def _make_many_packages_patch(names: list[str]) -> str:
    """`_AXIOS_BUMP_PATCH`와 동일한 node_modules/<name> 블록을 name마다 하나씩
    이어붙여, 여러 개의 독립된 (name, version) 변경 쌍을 갖는 lockfile patch를
    만든다."""
    lines = ["@@ -1,999 +1,999 @@"]
    for name in names:
        lines.append(f'     "node_modules/{name}": {{')
        lines.append('-      "version": "1.0.0",')
        lines.append('+      "version": "2.0.0",')
        lines.append('       "license": "MIT"')
        lines.append("     },")
    return "\n".join(lines) + "\n"


async def test_build_evidence_caps_at_max_packages() -> None:
    names = [f"pkg{i}" for i in range(odw._MAX_PACKAGES + 2)]  # 12개, 상한(10)보다 2개 많음
    registry = FakeRegistryClient(
        {(name, "2.0.0"): DeprecationLookupResult(ok=True, message=None, github_repo=f"o/{name}")
         for name in names}
    )
    release_client = FakeReleaseClient(
        {(f"o/{name}", "2.0.0"): _Result(ok=True, notes=f"notes for {name}") for name in names}
    )
    workflow = OfficialDocsWorkflow(registry, release_client, FakeCache())
    changed_files = [
        ChangedFile(
            file_path="package-lock.json",
            status="modified",
            patch=_make_many_packages_patch(names),
        )
    ]

    evidence = await workflow.build_evidence(changed_files)

    looked_up_names = [name for name, _ in registry.received]
    assert looked_up_names == names[: odw._MAX_PACKAGES]
    for name in names[: odw._MAX_PACKAGES]:
        assert f"{name}@2.0.0" in evidence
    for name in names[odw._MAX_PACKAGES :]:
        assert f"{name}@2.0.0" not in evidence


async def test_build_evidence_caps_packages_across_multiple_lockfiles() -> None:
    # 모노레포처럼 lockfile이 2개면, 상한은 파일당이 아니라 PR 전체(합계)에
    # 적용돼야 한다 — 파일당 상한이면 최대 20개까지 조회하게 된다.
    names_a = [f"a{i}" for i in range(6)]
    names_b = [f"b{i}" for i in range(6)]
    all_names = names_a + names_b
    registry = FakeRegistryClient(
        {
            (name, "2.0.0"): DeprecationLookupResult(
                ok=True, message=None, github_repo=f"o/{name}"
            )
            for name in all_names
        }
    )
    release_client = FakeReleaseClient(
        {(f"o/{name}", "2.0.0"): _Result(ok=True, notes=f"notes for {name}") for name in all_names}
    )
    workflow = OfficialDocsWorkflow(registry, release_client, FakeCache())
    changed_files = [
        ChangedFile(
            file_path="frontend/package-lock.json",
            status="modified",
            patch=_make_many_packages_patch(names_a),
        ),
        ChangedFile(
            file_path="backend/package-lock.json",
            status="modified",
            patch=_make_many_packages_patch(names_b),
        ),
    ]

    await workflow.build_evidence(changed_files)

    looked_up_names = [name for name, _ in registry.received]
    assert len(looked_up_names) == odw._MAX_PACKAGES
    assert looked_up_names == all_names[: odw._MAX_PACKAGES]


async def test_build_evidence_dedupes_same_package_across_lockfiles() -> None:
    registry = FakeRegistryClient(
        {
            ("axios", "2.0.0"): DeprecationLookupResult(
                ok=True, message=None, github_repo="o/axios"
            )
        }
    )
    release_client = FakeReleaseClient(
        {("o/axios", "2.0.0"): _Result(ok=True, notes="shared notes")}
    )
    workflow = OfficialDocsWorkflow(registry, release_client, FakeCache())
    patch = _make_many_packages_patch(["axios"])
    changed_files = [
        ChangedFile(file_path="frontend/package-lock.json", status="modified", patch=patch),
        ChangedFile(file_path="backend/package-lock.json", status="modified", patch=patch),
    ]

    evidence = await workflow.build_evidence(changed_files)

    assert registry.received == [("axios", "2.0.0")]
    assert release_client.received == [("o/axios", "axios", "2.0.0")]
    assert evidence.count("axios@2.0.0") == 1


async def test_build_evidence_truncates_notes_longer_than_per_package_limit() -> None:
    long_notes = "A" * (odw._MAX_NOTES_CHARS_PER_PACKAGE + 200)
    registry = FakeRegistryClient(
        {
            ("axios", "1.20.0"): DeprecationLookupResult(
                ok=True, message=None, github_repo="axios/axios"
            )
        }
    )
    release_client = FakeReleaseClient(
        {("axios/axios", "1.20.0"): _Result(ok=True, notes=long_notes)}
    )
    workflow = OfficialDocsWorkflow(registry, release_client, FakeCache())
    changed_files = [
        ChangedFile(file_path="package-lock.json", status="modified", patch=_AXIOS_BUMP_PATCH)
    ]

    evidence = await workflow.build_evidence(changed_files)

    truncated = "A" * odw._MAX_NOTES_CHARS_PER_PACKAGE + "..."
    assert truncated in evidence
    assert long_notes not in evidence
    assert "A" * (odw._MAX_NOTES_CHARS_PER_PACKAGE + 1) not in evidence


async def test_build_evidence_bounds_total_chars_across_many_packages() -> None:
    # 패키지당 노트는 800자 상한 밑이지만(700자), 5개를 합치면 3500자 + 구분자로
    # 3000자 총 상한을 넉넉히 넘긴다.
    names = [f"pkg{i}" for i in range(5)]
    per_package_notes = "B" * 700
    registry = FakeRegistryClient(
        {(name, "2.0.0"): DeprecationLookupResult(ok=True, message=None, github_repo=f"o/{name}")
         for name in names}
    )
    release_client = FakeReleaseClient(
        {(f"o/{name}", "2.0.0"): _Result(ok=True, notes=per_package_notes) for name in names}
    )
    workflow = OfficialDocsWorkflow(registry, release_client, FakeCache())
    changed_files = [
        ChangedFile(
            file_path="package-lock.json",
            status="modified",
            patch=_make_many_packages_patch(names),
        )
    ]

    evidence = await workflow.build_evidence(changed_files)

    assert evidence.startswith(odw._HEADER + "\n")
    assembled = evidence[len(odw._HEADER) + 1 :]
    # "..."로 인한 최대 3자 초과분을 제외하면 조립된 엔트리 문자열은
    # _MAX_TOTAL_CHARS를 넘지 않는다 — entry 개수가 늘어나도 초과분이
    # 계속 커지지 않아야 한다(구분자 "\n\n" 길이도 예산에 포함돼야 함).
    assert len(assembled) <= odw._MAX_TOTAL_CHARS + 3
