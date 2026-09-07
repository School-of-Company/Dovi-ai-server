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

    async def check_deprecation(self, name: str, version: str) -> DeprecationLookupResult:
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
