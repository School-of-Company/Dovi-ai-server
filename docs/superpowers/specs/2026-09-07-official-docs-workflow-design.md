# 5단계 — 공식문서 Workflow (Design Spec)

## 배경

로드맵 7.5절/25절 5단계("공식문서 Workflow"). 4단계는 npm 패키지의
`deprecated` 여부만 확인하는 결정론적(non-LLM) 경로였다. 이번 스코프는
그보다 넓다 — **버전이 바뀐 모든 npm 의존성**(deprecated 여부 무관)에
대해 GitHub 릴리즈 노트/CHANGELOG를 찾아, "이 버전 업그레이드가 실제로
API를 깨뜨리는 변경을 포함하는가"를 메인 리뷰 LLM이 판단할 수 있는
근거 텍스트를 만든다.

4단계와의 핵심 차이: 4단계는 "deprecated"라는 100% 객관적 사실이라
LLM을 아예 거치지 않고 `ReviewComment`를 직접 만들었다. 이번 스코프는
"릴리즈 노트의 이 문구가 실제로 이 PR의 코드 사용법과 충돌하는 breaking
change인가"를 판단해야 하는데, 이건 해석이 필요한 문제라 원안(7.5절)
그대로 **판단은 메인 리뷰 LLM에게 넘기고, 이 Workflow는 근거(릴리즈
노트 원문) 수집만 한다** — `Official Docs Search Workflow = 문서
검색/근거 수집`, `Code Review Model = 최종 코드 리뷰 판단`이라는 원안의
분리 원칙을 그대로 따른다.

## 스코프 결정 (브레인스토밍에서 확정)

- **트리거**: 버전이 바뀐 모든 npm 의존성 (deprecated 플래그와 무관).
  4단계보다 넓은 범위 — registry 조회량이 늘어나므로 캐싱이 더
  중요해진다.
- **문서 수집 범위**: GitHub Releases API + `CHANGELOG.md`만. 원안
  25절이 스스로 "공식문서 URL 구조가 라이브러리마다 다르고, rate
  limit/robots/HTML 구조 문제가 있어 가장 나중"이라고 명시한 임의
  사이트 크롤링은 이번 스코프에 포함하지 않는다.
- **버전 범위**: 타겟(신규) 버전 하나의 릴리즈 노트만 본다. 구버전→
  신버전 사이 여러 릴리즈를 전부 훑는 것(range coverage)은 GitHub API
  호출량과 구현 복잡도가 커서 다음 단계 후보로 미룬다.
- **오케스트레이션**: LangGraph를 도입하지 않는다. 이번 스코프(GitHub
  Releases + CHANGELOG, 재시도 1회)는 노드/엣지가 몇 개 안 되는 단순
  선형 파이프라인이라 그래프 추상화가 이득을 못 준다 — 노드/분기가
  실제로 늘어나면(예: 6단계 이후 공식문서 크롤링 도입 시) 그때 재검토.
- **registry 호출 통합**: 4단계가 이미 만든 `NpmRegistryClient`를
  확장해서 deprecated 확인과 GitHub repo URL 추출을 한 번의 registry
  호출로 처리한다(패키지당 2번 호출하지 않는다).
- **GitHub API 인증**: 새 `GITHUB_TOKEN`(read-only, public repo 대상,
  GitHub App의 private key와 무관한 별도 PAT)을 추가한다. 미인증
  60회/시간은 PR 트래픽이 조금만 늘어도 소진된다.

## 아키텍처 / 데이터 흐름

```
ReviewPipeline.run()  (targets가 있을 때만 — 코드 변경 자체가 없으면
                        "이 버전 변경이 이 코드에 영향 있는지" LLM이
                        판단할 문맥이 없다)
  → OfficialDocsWorkflow.build_evidence(event.changed_files)
      → npm_lockfile_diff.extract_dependency_changes()로 변경된
        (name, version) 쌍 전부 추출 (4단계와 별개로 자체 파싱 —
        아래 "Ruling: 파싱 중복" 참고)
      → 각 패키지: NpmRegistryClient에서 GitHub repo URL 조회
        (deprecated 조회와 통합된 단일 호출)
      → GithubReleaseClient로 해당 버전 릴리즈 노트/CHANGELOG 섹션 조회
        (Redis 캐시 우선 확인)
      → 근거 텍스트 블록 조립, 상한 적용
  → 리뷰 메시지의 api_spec_context 뒤에 이어붙임
  → 메인 리뷰 LLM이 이 텍스트를 참고해 최종 판단(breaking change
    여부 포함)
```

**Ruling — 파싱 중복 허용**: `DependencyResolver`(4단계)와
`OfficialDocsWorkflow`(이번 스코프)는 둘 다 같은
`extract_dependency_changes()`를 각자 독립적으로 호출한다. 파이프라인
레벨에서 한 번만 파싱해 공유하는 최적화도 가능하지만, patch 텍스트
라인 스캔은 계산 비용이 무시할 만한 수준이고, 공유하려면
`ReviewPipeline._find_dependency_findings()` 흐름을 건드려야 한다 —
지금은 두 기능의 결합도를 낮추는 쪽(각자 독립 파싱)이 낫다. 실제로
비용이 문제가 되면 그때 합친다(YAGNI).

## `NpmRegistryClient` 확장

기존 `DeprecationLookupResult`에 필드 하나 추가(이름 변경 없음 — 4단계
코드가 그대로 호환):

```python
@dataclass
class DeprecationLookupResult:
    ok: bool
    message: str | None
    github_repo: str | None = None  # "owner/repo" 형태, 파싱 실패/없음이면 None
```

`check_deprecation()` 내부에서 이미 파싱하는 registry JSON 응답의
`repository.url` 필드(예: `"git+https://github.com/axios/axios.git"`,
`"https://github.com/axios/axios"`, `"git://github.com/axios/axios.git"`
등 여러 변형이 실제로 관측된다)에서 정규식으로 `owner/repo`를
추출한다:

```python
_GITHUB_REPO_PATTERN = re.compile(
    r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$"
)

def _extract_github_repo(repository_field: object) -> str | None:
    url = None
    if isinstance(repository_field, dict):
        url = repository_field.get("url")
    elif isinstance(repository_field, str):
        url = repository_field
    if not isinstance(url, str):
        return None
    match = _GITHUB_REPO_PATTERN.search(url)
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}"
```

`check_deprecation()`의 반환 직전에 `github_repo=_extract_github_repo(data.get("repository"))`
를 추가한다. 4단계의 `DependencyResolver`는 이 필드를 그냥 무시하므로
동작 변화 없음.

## `GithubReleaseClient` (신규 `app/context/github_release_client.py`)

```python
@dataclass
class ReleaseNotesResult:
    ok: bool           # 조회 자체(네트워크/인증)가 성공했는지
    notes: str | None  # 찾았으면 텍스트, 못 찾았으면(404 등) None


class GithubReleaseClient:
    """owner/repo + 버전으로 GitHub 릴리즈 노트 또는 CHANGELOG 섹션을 찾는다.

    best-effort: 모든 실패(네트워크/인증/404/rate limit)는 예외를 던지지
    않고 ok=False 또는 notes=None으로 표현한다.
    """

    def __init__(
        self,
        *,
        token: str = "",
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(
            base_url="https://api.github.com", timeout=timeout_seconds, headers=headers
        )

    async def find_release_notes(
        self, owner_repo: str, name: str, version: str
    ) -> ReleaseNotesResult:
        for tag in (f"v{version}", f"{name}@{version}", version):
            notes = await self._try_tag(owner_repo, tag)
            if notes is not None:
                return ReleaseNotesResult(ok=True, notes=notes)
        changelog_notes = await self._try_changelog(owner_repo, version)
        if changelog_notes is not None:
            return ReleaseNotesResult(ok=True, notes=changelog_notes)
        return ReleaseNotesResult(ok=True, notes=None)  # 조회는 성공, 못 찾았을 뿐

    async def _try_tag(self, owner_repo: str, tag: str) -> str | None:
        try:
            response = await self._client.get(f"/repos/{owner_repo}/releases/tags/{tag}")
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        body = data.get("body") if isinstance(data, dict) else None
        return body if isinstance(body, str) and body.strip() else None

    async def _try_changelog(self, owner_repo: str, version: str) -> str | None:
        for branch in ("main", "master"):
            try:
                response = await self._client.get(
                    f"https://raw.githubusercontent.com/{owner_repo}/{branch}/CHANGELOG.md"
                )
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            section = _extract_changelog_section(response.text, version)
            if section is not None:
                return section
        return None

    async def aclose(self) -> None:
        await self._client.aclose()
```

`_extract_changelog_section(text, version)`: `## [1.2.3]` / `## 1.2.3`
형태의 헤딩부터 다음 `## ` 헤딩(또는 문서 끝) 전까지를 정규식으로
잘라낸다. 못 찾으면 `None`.

**주의**: raw.githubusercontent.com은 GitHub API가 아니라 별도 호스트다
— `self._client`의 `base_url`(`api.github.com`)을 쓰지 않고 전체 URL을
넘긴다(httpx는 절대 URL이 오면 `base_url`을 무시하고 그 URL을 그대로
쓴다). `Authorization` 헤더는 이 호스트에서도 그냥 무시되므로 문제
없다(공개 레포 raw 파일은 인증 불필요).

## 캐싱 (`app/context/release_notes_cache.py`, 신규)

```python
class RedisReleaseNotesCache:
    """(owner/repo, name, version)별 릴리즈 노트 조회 결과를 캐싱한다.

    "찾음"과 "이 버전엔 릴리즈노트가 없음"을 둘 다 캐싱한다 — 둘 다
    해당 버전에 대해 불변인 사실이라(버전이 바뀌지 않는 한) 30일 TTL로
    안전하게 재사용 가능하다(RedisNpmDeprecationCache와 동일 정책).
    """

    def __init__(
        self, redis: RedisLike, *,
        key_prefix: str = "ai-review:release-notes:",
        ttl_seconds: int = 2592000,
    ) -> None: ...

    async def get(self, owner_repo: str, version: str) -> str | None | _Missing: ...
    # 캐시 미스는 _Missing sentinel, 캐시에 "없음"이 기록된 경우는 None,
    # 값이 있으면 그 문자열 — 3-state를 구분해야 "캐시 미스"와
    # "찾아봤는데 없었다"를 혼동하지 않는다 (4단계 CachedResult와 동일한
    # 문제의식).
    async def set(self, owner_repo: str, version: str, notes: str | None) -> None: ...
```

## `OfficialDocsWorkflow` (신규 `app/context/official_docs_workflow.py`)

```python
_MAX_PACKAGES = 10          # 프롬프트 토큰 예산 보호 — 4단계의 50개보다
                             # 훨씬 작다(이건 boolean이 아니라 텍스트 전체가
                             # 프롬프트에 들어간다).
_MAX_NOTES_CHARS_PER_PACKAGE = 800
_MAX_TOTAL_CHARS = 3000


class OfficialDocsWorkflow:
    """package-lock.json 변경분의 의존성에 대해 GitHub 릴리즈 노트/CHANGELOG를
    찾아 근거 텍스트를 만든다. 판단은 하지 않는다 — breaking change 여부는
    메인 리뷰 LLM이 이 텍스트를 보고 직접 판단한다.

    best-effort: 실패해도 빈 문자열을 반환한다(리뷰 자체를 막지 않는다).
    """

    def __init__(
        self,
        registry_client: RegistryClient,       # 4단계와 동일 Protocol 재사용
        release_client: GithubReleaseClient,
        cache: ReleaseNotesCache,
    ) -> None: ...

    async def build_evidence(self, changed_files: list[ChangedFile]) -> str:
        ...  # lockfile당 extract_dependency_changes → dedup → 상한 적용
             # → 각 패키지에 대해 repo URL 조회 → 캐시 확인 → 없으면
             # release_client 조회 → 캐시 저장 → 텍스트 조립
```

반환 형식 (근거가 하나도 없으면 빈 문자열):

```
\n\n#### 의존성 버전 변경 근거 (공식 릴리즈 노트)
axios@1.20.0:
<릴리즈 노트 텍스트, 최대 800자>

zustand@5.0.15:
<릴리즈 노트 텍스트>
```

전체 블록이 `_MAX_TOTAL_CHARS`를 넘으면 뒤 패키지부터 자른다(다른
컨텍스트 블록들과 동일한 "예산 안에서 앞부터 채우고 나머지는 버린다"
방식 — `pipeline.py`의 `_truncate_diff_blocks`와 동일 원칙).

## 파이프라인 연결

`app/review/pipeline.py`:

```python
class OfficialDocsContextBuilder(Protocol):
    async def build_evidence(self, changed_files: list[ChangedFile]) -> str: ...
```

`ReviewPipeline.__init__`에 `official_docs_workflow:
OfficialDocsContextBuilder | None = None` 추가(기존
`api_spec_retriever`와 동일한 optional-injection 패턴).

`run()`에서 `targets`가 있을 때만(= `if not targets:` 이후) 호출:

```python
official_docs_context = await self._build_official_docs_context(event)
messages = self._build_messages(
    event, targets, related_context, api_spec_context, official_docs_context
)
```

```python
async def _build_official_docs_context(self, event: ReviewRequestedEvent) -> str:
    if self._official_docs_workflow is None:
        return ""
    try:
        return await self._official_docs_workflow.build_evidence(event.changed_files)
    except Exception:
        logger.warning("official docs workflow failed", exc_info=True)
        return ""
```

`_build_messages`에서 `user += api_spec_context + official_docs_context`
— api_spec_context 바로 뒤에 이어붙인다.

## 설정 / 배선

`app/core/config.py`:
```python
official_docs_workflow_enabled: bool = False
github_token: str = ""
```

`app/main.py`의 `lifespan()`에 `if settings.official_docs_workflow_enabled:`
블록 추가(기존 패턴과 동일한 지연 import). 이 기능은 4단계의
`NpmRegistryClient` 인스턴스를 공유해야 하므로(패키지당 registry 호출
1번 통합), 두 optional 블록의 조합을 아래처럼 정리한다:

```python
npm_registry_client = None
dependency_resolver = None
official_docs_workflow = None
if settings.dependency_check_enabled or settings.official_docs_workflow_enabled:
    from app.context.npm_registry_client import NpmRegistryClient
    npm_registry_client = NpmRegistryClient()

if settings.dependency_check_enabled:
    from app.context.dependency_resolver import DependencyResolver
    from app.context.npm_deprecation_cache import RedisNpmDeprecationCache
    npm_deprecation_cache = RedisNpmDeprecationCache(redis_client)
    dependency_resolver = DependencyResolver(npm_registry_client, npm_deprecation_cache)

if settings.official_docs_workflow_enabled:
    from app.context.github_release_client import GithubReleaseClient
    from app.context.official_docs_workflow import OfficialDocsWorkflow
    from app.context.release_notes_cache import RedisReleaseNotesCache
    github_release_client = GithubReleaseClient(token=settings.github_token)
    release_notes_cache = RedisReleaseNotesCache(redis_client)
    official_docs_workflow = OfficialDocsWorkflow(
        npm_registry_client, github_release_client, release_notes_cache
    )
```

**Ruling — `npm_registry_client`가 None일 수 있는 경우 없음을 보장**:
두 플래그 중 하나라도 켜지면 `npm_registry_client`가 만들어지므로,
`dependency_check_enabled`와 `official_docs_workflow_enabled`를 각각
켜는 조합 4가지 전부에서 `npm_registry_client`가 필요한 곳엔 항상
있다. `finally:` 블록의 `npm_registry_client.aclose()` 가드(`is not
None`)는 그대로 유지 — 이제 "둘 다 꺼짐"일 때만 None이다.

`.env.example`에 추가:
```
OFFICIAL_DOCS_WORKFLOW_ENABLED=false
GITHUB_TOKEN=
```

## 에러 처리 요약

| 실패 지점 | 처리 |
|---|---|
| registry 조회 실패(repo URL 못 얻음) | 해당 패키지 스킵, 나머지는 계속 |
| GitHub API 실패/404/rate limit | 해당 패키지 근거 없음(빈 항목 아님 — 아예 목록에서 제외) |
| CHANGELOG raw fetch 실패 | 마지막 폴백도 실패한 것으로 처리, 해당 패키지 스킵 |
| Redis 캐시 읽기/쓰기 실패 | 캐시 미스로 간주, live 조회로 폴백 |
| `OfficialDocsWorkflow` 전체 예외 | pipeline이 catch, 빈 문자열 취급 — 리뷰 자체를 절대 실패시키지 않음 |

## 테스트 전략

- `npm_registry_client.py` 확장분: `repository.url`의 여러 실제 변형
  (`git+https://...`, `git://...`, 순수 `https://...`, 값 없음)에서
  `github_repo` 파싱 검증.
- `github_release_client.py`: httpx mock으로 태그 3종 시도 순서, 전부
  실패 시 CHANGELOG 폴백, CHANGELOG도 실패 시 `notes=None`,
  네트워크/타임아웃 시 `ok=False` 케이스.
- `release_notes_cache.py`: FakeRedis, hit/미스/"없음-캐싱" 3-state
  구분.
- `official_docs_workflow.py`: fake registry+release client 조합 —
  일부 패키지만 성공, 상한(`_MAX_PACKAGES`/`_MAX_TOTAL_CHARS`) 적용
  확인.
- `pipeline.py`: fake workflow가 텍스트 반환 → 최종 유저 메시지에
  `api_spec_context` 뒤로 포함되는지, `targets` 없을 때는 호출 자체가
  안 되는지, workflow가 예외를 던져도 리뷰가 정상 완료되는지.

## 스코프 밖 (다음 단계 후보, 지금 안 함)

- Maven/Spring 등 non-npm 생태계 (4단계와 동일한 스코프 경계)
- 구버전→신버전 사이 여러 릴리즈 전체 커버(range coverage) — 타겟
  버전 하나의 릴리즈 노트만 봄
- 임의 공식문서 사이트 fetch/크롤링 — GitHub Releases/CHANGELOG만
- LangGraph 도입 — 노드/분기가 실제로 늘어나면 재검토
- `DependencyResolver`와의 lockfile 파싱 결과 공유 최적화
