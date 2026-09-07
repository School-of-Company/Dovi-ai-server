# 4단계 — npm 의존성 deprecated 감지 (Design Spec)

## 배경

로드맵 4단계("의존성 근거")의 첫 스코프. 원래 계획은 lockfile 파싱 + registry
조회 + Dependency Context Resolver + Evidence Generator를 묶어 LLM에게
근거를 제공하는 것이었으나, 이번 스코프는 **LLM 판단이 필요 없는 좁고 확실한
케이스 하나**로 시작한다: PR diff에서 npm `package-lock.json`에 새로
추가/변경되는 의존성이 npm registry 기준 deprecated인지 확인해, 결정론적으로
`ReviewComment`를 생성한다.

지원 생태계는 Node/TS(`package-lock.json`)만. Spring/Maven, yarn/pnpm은
이번 스코프 밖 — 두 번째 생태계가 실제로 필요해지는 시점에 인터페이스를
뽑는다(YAGNI, `.claude/rules/architecture.md`의 "두 번째 실사용 사례
전까지 추상화 금지" 원칙).

## 왜 LLM을 거치지 않는가

이번 세션에서 리뷰 파이프라인의 반복된 버그(summary 프로즈 누출, 헤지된
근거, Project Context 오염 — PR #69/#71/#72/#73)는 전부 "LLM이 애매한
컨텍스트를 잘못 해석"하는 패턴이었다. deprecated 여부는 npm registry가
`deprecated` 필드로 직접 알려주는 100% 결정론적 사실이라 LLM 판단이 애초에
불필요하다 — 코드가 직접 `ReviewComment`를 만들어 그 계열의 위험을
구조적으로 없앤다.

## 제약 확인 (사전 조사 완료)

- **github-app은 건드리지 않는다** (사용자 확정). `event.changedFiles[].content`는
  실제 프로덕션 Kafka 이벤트로 직접 확인한 결과 항상 `null`이고 `patch`만
  온다 — lockfile 전체를 JSON으로 파싱하는 설계는 불가능, patch(unified
  diff) 텍스트만으로 파싱해야 한다.
- 실제 프로덕션 이벤트에서 캡처한 `package-lock.json` patch 샘플(lockfileVersion
  2/3 형식)로 구조를 확인함 — `"node_modules/<pkg>": {` 키 다음에
  `"version": "X.Y.Z"` 필드가 온다. 이 구조는 정규식이 아니라 라인 스캐너로
  안정적으로 파싱 가능.
- `app/review/diff.py`의 `_LOCKFILES` 집합에 `package-lock.json`이 이미
  포함되어 있어 `analyze()`가 만드는 `targets`에는 절대 안 들어온다(일반
  코드 리뷰 대상에서 의도적으로 제외됨). 따라서 이 기능은 `targets`가 아니라
  `event.changed_files` 원본 리스트를 별도로 훑어야 한다.
  > **최종 리뷰에서 발견된 실수 (사후 기록)**: 원래 계획/이번 스펙은 여기까지만
  > 확인하고, `ReviewPipeline.run()`이 `if not targets: return ...`로 조기
  > 반환한다는 사실을 놓쳤다. `package-lock.json`만 바뀐 PR(이 기능의 핵심
  > 대상 시나리오인 `npm audit fix`/renovate/dependabot lockfile-maintenance
  > PR)은 `targets == []`가 되어 resolver가 호출되기 전에 파이프라인이 이미
  > 반환해버려, 기능이 있으나 마나였다. 전체 브랜치 최종 리뷰에서만 발견되어
  > `run()`이 `if not targets` 체크보다 먼저 resolver를 호출하도록 수정했다.
- 프로덕션 AI 서버 컨테이너에서 `registry.npmjs.org`로 아웃바운드 HTTPS
  접속 가능함을 직접 확인함(200 응답).
- `ReviewComment.line`은 필수(`Field(gt=0)`) — 기존 코드엔 unified diff
  hunk 헤더에서 new-file 라인 번호를 계산하는 유틸이 없어(LLM이 직접
  읽고 판단하는 구조라서), 이번 기능에서 새로 구현해야 한다.

## 아키텍처 / 파일 구성

`app/context/`(기존 `api_spec_retriever.py`/`api_spec_link_store.py`와
같은 계층)에 4개 파일 추가:

```
app/context/
  npm_registry_client.py       # 외부 API I/O만 (client.py 원칙)
  npm_lockfile_diff.py         # patch 텍스트 파싱 (순수 함수, I/O 없음)
  npm_deprecation_cache.py     # Redis 캐시
  dependency_resolver.py       # 위 셋을 엮는 조정자, best-effort
```

### `npm_lockfile_diff.py` — patch 파서 (순수 함수)

```python
@dataclass
class DependencyChange:
    name: str
    version: str
    evidence_line: str    # 예: '      "version": "5.102.8",'
    new_file_line: int    # GitHub 인라인 코멘트용 new-file 라인 번호


def extract_dependency_changes(patch: str) -> list[DependencyChange]:
    """package-lock.json patch에서 새로 추가/변경된 (패키지명, 버전) 쌍을 뽑는다.

    npm lockfileVersion 2/3 구조만 지원한다:
    "node_modules/<name>": {
      "version": "X.Y.Z",
      ...
    }
    라인 스캔 방식: hunk 헤더(@@ -a,b +c,d @@)에서 new_file_line 카운터를
    c로 초기화하고, 라인을 순서대로 훑으며
    - " "(context)로 시작 -> 카운터 +1
    - "-"(삭제)로 시작 -> 카운터 불변
    - "+"(추가)로 시작 -> 카운터를 이 라인의 new_file_line으로 기록 후 +1
    "node_modules/<name>": {  형태의 컨텍스트(또는 추가) 라인을 만나면
    "현재 패키지명"으로 기억해두고, 그 뒤 가장 가까운
    +      "version": "X.Y.Z",  라인이 나오면 (name, version, line, new_file_line)
    하나를 확정한다. 다음 "node_modules/ 라인을 만나면 현재 패키지명을 갱신한다.
    "version" 라인을 못 찾고 다음 node_modules/ 라인이 나오면 그 패키지는 스킵.

    중첩된 transitive dependency는 키가
    "node_modules/parent-pkg/node_modules/child-pkg" 형태로 여러 번
    "node_modules/"가 반복될 수 있다 — 패키지명은 마지막 "node_modules/"
    다음부터 닫는 큰따옴표 전까지다(예: 위 예시의 패키지명은 "child-pkg",
    "parent-pkg"가 아니다). 즉 `key.rsplit("node_modules/", 1)[-1]`로
    추출한다.
    """
```

**테스트 fixture**: 실제 프로덕션 Kafka 이벤트에서 캡처한 진짜 patch(위
"제약 확인" 절 참고)를 그대로 고정 fixture로 사용한다 — `axios`,
`@tanstack/query-core` 등 여러 패키지가 동시에 bump되는 케이스를 실제
검증할 수 있다.

**스코프 밖으로 명시할 것**: lockfileVersion 1(node_modules/ 접두사 없이
패키지명이 바로 키인 구조), yarn.lock/pnpm-lock.yaml(완전히 다른 포맷).
이런 파일은 파서가 그냥 빈 리스트를 반환한다(에러 아님, 조용히 스킵).

### `npm_registry_client.py` — registry I/O

```python
class NpmRegistryClient:
    def __init__(self, *, timeout_seconds: float = 3.0) -> None: ...

    async def get_deprecation_message(self, name: str, version: str) -> str | None:
        """GET https://registry.npmjs.org/<name>/<version>의 `deprecated` 필드를
        반환한다. 없으면(비-deprecated) None. 네트워크 실패/타임아웃/404 등
        모든 예외는 여기서 삼키고 None을 반환한다(best-effort — 개별 조회
        실패가 캐시 오염이나 전체 리뷰 실패로 번지지 않게 이 계층에서 끊는다).
        """
```

> **최종 리뷰에서 발견된 실수 (사후 기록)**: 위 설계와 실제 초기 구현
> (`get_deprecation_message`)은 "실패"와 "조회 성공했지만 deprecated 아님"을
> 둘 다 `None`으로 뭉뚱그렸다. 그 결과 `DependencyResolver`는 registry가
> 일시적으로 타임아웃/네트워크 실패해도 이를 `CachedResult(deprecated=False,
> message=None)`으로 캐시에 30일간 저장해버려, "실패가 캐시 오염으로 번지지
> 않는다"는 위 문장과 반대로 실제로는 번졌다. 최종 리뷰에서 발견되어
> `NpmRegistryClient.check_deprecation()`이 `DeprecationLookupResult(ok: bool,
> message: str | None)`을 반환하도록 바꿨다 — `ok=False`(네트워크 실패, 타임아웃,
> 404, malformed JSON 등)는 캐시에 전혀 쓰지 않고 다음 PR에서 다시 조회하며,
> `ok=True`(조회 자체는 성공, `message`가 `None`이면 단지 deprecated가 아니라는
> 뜻)일 때만 `DependencyResolver`가 캐시에 기록한다.

`httpx.AsyncClient` 사용(architecture.md의 async I/O 원칙). 패키지명에
`/`가 포함되는 scoped package(`@tanstack/query-core`)는 npm registry
API 규칙대로 `/`만 `%2F`로 인코딩하고 `@`는 그대로 둬야 한다
(`https://registry.npmjs.org/@tanstack%2Fquery-core/5.102.8`) —
`urllib.parse.quote(name, safe="@")`로 `@`를 인코딩 대상에서 제외한다.
`safe=""`로 `@`까지 인코딩하면 registry가 다른 URL로 취급해 항상 404가
난다.

### `npm_deprecation_cache.py` — Redis 캐시

기존 `RedisNotionLinkStore`(`app/context/api_spec_link_store.py`)와 동일한
구조:

```python
class RedisNpmDeprecationCache:
    def __init__(self, redis: RedisLike, *, ttl_seconds: int = 2592000) -> None: ...
    # key: "ai-review:npm-deprecation:<name>@<version>"
    # value: JSON {"deprecated": true, "message": "..."} 또는 {"deprecated": false}
    # (버전 자체는 불변이지만 deprecated 플래그는 이미 배포된 버전에
    #  나중에 붙을 수 있어 무기한 캐싱은 위험 — 30일 TTL, NotionLinkStore와 동일 정책)

    async def get(self, name: str, version: str) -> CachedResult | None: ...
    async def set(self, name: str, version: str, result: CachedResult) -> None: ...
```

Redis 값은 bytes로 오므로 `RedisNotionLinkStore.get()`에서 이미 겪은
bytes/str 버그(PR #67 Task 2 리뷰에서 발견)를 재발시키지 않도록 처음부터
명시적으로 `isinstance(value, bytes)` 체크 후 decode한다.

### `dependency_resolver.py` — 조정자

```python
class DependencyResolver:
    def __init__(
        self,
        registry_client: NpmRegistryClient,
        cache: RedisNpmDeprecationCache,
    ) -> None: ...

    async def find_deprecated_dependencies(
        self, changed_files: list[ChangedFile]
    ) -> list[ReviewComment]:
        """package-lock.json 변경분에서 deprecated 의존성을 찾아
        ReviewComment 리스트로 반환한다. 실패해도(네트워크/파싱 어떤 이유든)
        예외를 밖으로 던지지 않고 그때까지 확인된 것만 반환한다
        (ProjectContextRetriever/ApiSpecRetriever와 동일한 best-effort 계약).
        """
```

생성하는 `ReviewComment`:
```python
ReviewComment(
    severity="minor",
    confidence=1.0,
    file_path="package-lock.json",
    line=change.new_file_line,
    title=f"deprecated 패키지 추가/변경됨: {change.name}@{change.version}",
    message=f"npm registry: '{deprecation_message}'",
    evidence=[change.evidence_line],
    suggested_fix=None,
)
```

## 데이터 흐름 (파이프라인 연결 지점)

`app/review/pipeline.py`의 `ReviewPipeline.run()`에서, LLM `generate()`가
성공한 직후 · `filter_reviews(output.reviews)` 호출 **이전**에 한 줄
추가한다:

```python
if self._dependency_resolver is not None:
    dependency_findings = await self._dependency_resolver.find_deprecated_dependencies(
        event.changed_files
    )
    output.reviews.extend(dependency_findings)

reviews = filter_reviews(output.reviews)
...
summary = self._build_summary(output.summary, output.reviews)  # 기존 코드, 변경 없음
```

`severity="minor"`이므로 기존 라우팅 규칙을 그대로 탄다:
- `filter_reviews()`는 critical/major만 추출하므로 이 finding은 여기 안 걸림
  (= `_verify()` 2차 검증도 안 거침, 의도된 동작 — 이미 결정론적으로 확정된
  사실이라 재검증 대상이 아님)
- `_build_summary(output.summary, output.reviews)`가 원본(raw) `output.reviews`를
  받으므로(PR #69에서 확정된 계약), `summarize_minor()`를 통해 자동으로
  "참고(경미한 항목)" summary bullet로 나타남

`result_filter.py`, `_verify()`, 프롬프트에는 **아무 변경도 필요 없다**.

`ReviewPipeline.__init__`에 `dependency_resolver: DependencyResolver | None = None`
파라미터 추가(기존 `retriever: ContextRetriever | None = None`과 동일한
optional-injection 패턴).

## 설정 / 배선

`app/core/config.py`에 추가:
```python
dependency_check_enabled: bool = False
```

`app/main.py`의 `lifespan()`에 `if settings.dependency_check_enabled:` 블록
추가(기존 `rag_enabled`/`notion_sync_enabled` 패턴과 동일) — 지역 import로
`NpmRegistryClient`/`RedisNpmDeprecationCache`/`DependencyResolver` 생성.
이 기능은 qdrant/embedding처럼 무거운 의존성이 없으므로(httpx는 이미
프로젝트 전역 의존성) qdrant_client 회귀 패턴과 무관 — 다만 관례상
optional 기능이므로 flag는 유지한다.

레지스트리는 public이라 새 토큰/시크릿 불필요.

## 에러 처리 요약

| 실패 지점 | 처리 |
|---|---|
| patch 파싱 실패(알 수 없는 lockfile 포맷) | 빈 리스트 반환, 에러 아님 |
| registry 타임아웃/네트워크 실패 | 해당 패키지만 스킵(None), 나머지는 계속 진행 |
| Redis 연결 실패 | 캐시 미스로 간주하고 live 조회로 폴백(예외 삼킴) |
| `DependencyResolver` 전체 예외 | pipeline이 catch해서 빈 리스트 취급 — **리뷰 자체를 절대 실패시키지 않는다** |

## 테스트 전략

- `npm_lockfile_diff.py`: 실제 캡처한 patch 샘플(다중 패키지 동시 bump)로
  `extract_dependency_changes()` 검증 — 이름/버전/evidence/new_file_line
  정확히 매칭되는지. lockfileVersion 1 포맷, yarn.lock 포맷을 흉내낸
  입력도 빈 리스트 반환하는지 확인.
- `npm_registry_client.py`: httpx mock으로 deprecated/non-deprecated/404/timeout
  케이스.
- `npm_deprecation_cache.py`: FakeRedis, bytes/str 디코드 케이스(PR #67
  Task 2와 동일 계열 버그 방지).
- `dependency_resolver.py`: fake client+cache 조합, 일부 실패해도 나머지는
  반환되는지.
- `pipeline.py`: fake `DependencyResolver`가 minor finding 하나 반환 →
  최종 `summary`에 "참고(경미한 항목)"로 나타나고 인라인 코멘트로는 안
  나타나는지(`filter_reviews`/`_verify` 안 거침) 확인.

## 스코프 밖 (다음 단계 후보, 지금 안 함)

- Spring/Maven(`pom.xml`/`build.gradle`) 지원 — 두 번째 생태계 실사용
  필요해지면 `LockfileDiffParser` Protocol을 뽑아 npm 구현체와 나란히 둔다
- yarn.lock/pnpm-lock.yaml, npm lockfileVersion 1
- 4단계 원안의 나머지(semver major bump 감지, 공식문서/changelog 연동) —
  5단계(공식문서 Workflow)와 겹치므로 그쪽에서 재검토
- Evidence Generator라는 별도 모듈 — 지금 스코프에선 결정론적 finding
  생성만으로 충분해 불필요
