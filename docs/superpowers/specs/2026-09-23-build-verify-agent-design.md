# 샌드박스 프로브 (Sandbox Probe) 설계 — v3

> v3 리비전. v1(LLM 에이전트 루프)은 Opus 1차 리뷰에서 네트워크 차단·npm install 모순, 프롬프트 인젝션 누락, 컨텍스트 예산 산술 불가 등으로 기각. v2(결정론적 프로브, LLM 제거)는 Opus 2차 리뷰에서 핵심 전제(`madge --circular`가 PR #5를 잡는다)가 **실측으로 반증**됨 — madge는 버그 수정 여부와 무관하게 이 레포에 상시 존재하는 순환 import를 보고해 판별력이 없었다. v3는 그 대안 프로브를 실제로 PR #5/#12의 수정 전/후 fixture 4개에 대해 **직접 clone→checkout→build→실행해서 검증**했고(2026-09-23), 둘 다 정확히 판별함을 확인한 뒤 이 검증 결과를 근거로 다시 썼다. 2차 리뷰가 지적한 나머지 P0/P1(격리 우회 경로, 배포 구조가 실제로는 메인 리뷰 컨슈머를 탈취하는 문제, 토큰 교환 미정의 등)도 모두 반영했다.

## 배경

최근 5일 내 실제 PR 두 건에서, 사람 리뷰어(cfcromn)가 로컬에서 직접 빌드/서버 기동을 해봐서 Dovi가 놓친 런타임 버그를 잡아냈다.

1. **`School-of-Company/Expo-Form-Server` PR #5** — `src/form/entities/dynamic-form.entity.ts`의 `form: FormEntity` 필드가 ESM 순환참조 + `emitDecoratorMetadata` 상호작용으로 `Cannot access 'FormEntity' before initialization`을 일으킴. 일반적인 `pnpm build && pnpm start`로는 재현되지 않고, `FormEntity`를 먼저 import하는 특정 순서에서만 재현됨.
2. **`School-of-Company/Expo-Form-Server` PR #12** — `app.enableShutdownHooks()` 누락으로 `SIGTERM` 시 `OnApplicationShutdown` 훅이 실행되지 않아, Discord 웹훅 종료 알림이 발송되지 않음.

두 케이스 모두 Dovi의 현재 파이프라인(diff + AST 컨텍스트 + RAG를 프롬프트에 넣고 LLM 한 번 호출)으로는 구조적으로 잡을 수 없다. 정적 분석도, 고정 테스트 스위트도 안 걸린다.

## 목표

Dovi 리뷰 파이프라인에, PR 코드를 실제로 빌드/기동해서 정적 분석으로 못 잡는 런타임 이슈를 잡는 별도 비동기 트랙을 추가한다. 메인 리뷰(현재 SLA, 현재 LLM 호출량)에 영향을 주지 않는다. **LLM을 호출하지 않는다** — 결정론적 프로브 스크립트만 쓴다.

## 비목표 (v1 범위 제외)

- Java/Gradle, Python 등 NestJS 외 스택 지원.
- 실제 서드파티 API(결제 등)처럼 로컬로 흉내낼 수 없는 의존성 검증.
- **LLM 기반 가설 탐색 에이전트 루프** — Phase 2로 명시적으로 미룬다. 이번 스펙에서 다루지 않는다.
- fork PR 지원 — v1은 같은 레포 브랜치에서 연 PR만 대상으로 한다.
- 메인 리뷰의 응답 시간이나 LLM 호출량에 영향을 주는 어떤 변경도 포함하지 않음.

## 수용 기준 (구현의 merge 조건)

아래 4개 fixture 상태에 대한 재생 테스트가 **정확히** 아래 결과를 내야 한다. 실측 검증 완료(2026-09-23):

| 레포 | 커밋 SHA | 상태 | 프로브 | 기대 결과 |
|---|---|---|---|---|
| Expo-Form-Server | `07045bcb0d9a` | PR #5 버그 있음 | 초기화 순서 프로브 | `found_issue` |
| Expo-Form-Server | `b13836e9136d` | PR #5 수정(cfcromn 지적 반영) | 초기화 순서 프로브 | `passed` |
| Expo-Form-Server | `8a4f5f9a2dc7` | PR #12 버그 있음 | 생명주기 프로브 | `found_issue` |
| Expo-Form-Server | `482db26e3993` | PR #12 수정 | 생명주기 프로브 | `passed` |

구현체는 이 4개 상태를 실제로 재생하는 통합 테스트(예: `tests/test_sandbox_probe_fixtures.py`)를 CI에 포함해야 한다.

## 단계 구분

- **Phase 1** (이 스펙의 구현 대상): LLM 없는 결정론적 프로브 하네스.
- **Phase 2** (비목표, 별도 스펙): Phase 1 위에 에이전트 루프를 얹는 것.

> v2에 있던 "Phase 0"은 제거했다 — llama-server 네트워크 경로 정리는 Phase 1이 전혀 쓰지 않는 Phase 2 전용 선행 작업인데 "Phase 0(선행)"이라는 이름 때문에 구현자가 불필요하게 먼저 손대게 될 위험이 있었다. Phase 1의 실제 선행 작업은 아래 "선행 작업" 절에 모아뒀다.

## 선행 작업 (Phase 1 착수 전 필요)

- `installation-token` 모듈(Dovi-github-app) 확장: raw 토큰 반환 + `permissions`/`repositories` 파라미터 지원, 캐시 키에 스코프 포함(`github:token:${installationId}:${scopeHash}`) — 현재는 `getOctokit(installationId)` 하나뿐이라 스코프 축소된 토큰을 만들 수 없다.
- GitHub webhook payload DTO에 `pull_request.head.repo.{id,full_name}` 추가 + fork 판별 헬퍼 — 현재 DTO는 `head.sha`만 파싱해서 fork PR을 구분할 방법이 없다.
- GitHub App 권한에 issue 코멘트 게시(`issues: write`)가 포함되는지 확인 — 현재 Dovi-github-app은 `issues.createComment`를 한 번도 호출한 적이 없어 권한이 실제로 있는지 미검증.
- `app/core/config.py`에 컨슈머별 개별 enable 플래그 추가(`review_consumer_enabled`, `comment_answer_consumer_enabled`, `sandbox_probe_consumer_enabled`) — 아래 "배포/컨슈머 격리" 참고.

## 아키텍처 개요

```
GitHub PR 이벤트
  → Dovi-github-app: review-dispatcher가 메인 리뷰 이벤트 발행 (변경 없음, 완전히 독립)
  → Dovi-github-app: 신규 독립 경로 — 스택 감지(package.json에 @nestjs/core) +
    같은 레포 브랜치 PR + 레포 opt-in(DOVI.md) 확인 후에만
    pr.sandbox.probe.requested 발행 (신규 토픽, 메인 리뷰 경로와 완전 분리)
  → Dovi-ai-server: 기존 review/comment-answer 컨슈머 그룹 — 완전히 영향 없음
  → Dovi-ai-server: 신규 dovi-ai-sandbox-probe-engine 컨슈머 그룹
    (전용 VM에서만 기동, review/comment-answer 컨슈머는 그 VM에서 명시적으로 꺼둠)
      1. 워커(호스트 프로세스)가 잡 시작 직전 installation token 발급받아
         head_sha 고정 clone, 토큰 제거된 워킹트리만 준비
      2. install 단계 컨테이너(레지스트리 egress만 허용)에서 의존성 설치 →
         node_modules 볼륨 스냅샷
      3. 프로브 컨테이너(외부 네트워크 완전 차단) 기동, env 3버킷+화이트리스트 분류 적용
      4. 고정 프로브 스위트 순차 실행 (LLM 호출 없음)
      5. 종료 시 라벨 기반 리소스 정리
  → Kafka로 pr.sandbox.probe.completed 발행
  → Dovi-github-app: 신규 독립 컨슈머 모듈 (comment-answer-result를 선례로 삼음)
      - 마커 주석 기반 sticky 코멘트 upsert, 항상 게시
```

## 인프라

전용 VM 1대 (프로덕션 GPU 박스와 분리):

- **스펙**: 4 vCPU / 15Gi RAM / 58G 디스크, GPU 없음
- **동시 처리**: 최대 4개 잡. 기존 컨슈머들은 전부 엄격히 순차 처리(`async for` 안에서 처리 후 커밋)이므로, 신규 토픽(`pr.sandbox.probe.requested`)은 **파티션 4개 이상**으로 생성해야 동시성 목표가 성립한다. 멀티 인스턴스 또는 단일 프로세스 내 세마포어 중 구현 방식은 구현 단계에서 정한다.
- **LLM 추론**: 이 VM에서 전혀 없다(Phase 1). Phase 2 대비 llama-server 네트워크 경로는 나중에 별도로 검토한다(프로덕션 박스는 현재 방화벽 없이 `127.0.0.1` 바인딩 + SSH 터널이 실제 보안 모델이므로, 이 기능과 무관하게 그 관례에 맞춰 처리해야 한다).
- **분리 이유**: 신뢰할 수 없는 PR 코드를 실제로 빌드/실행하므로, 프로덕션 GPU 박스와 물리적으로 분리해 자원 고갈이 전이되지 않게 한다.
- **접속 정보**: 로컬 메모리에만 보관, 스펙 문서나 git에는 포함하지 않는다.

## 배포/컨슈머 격리

`app/main.py`의 현재 lifespan은 `kafka_consumer_enabled` 플래그 하나가 true면 review·comment-answer 컨슈머가 **무조건 같이** 뜬다. 샌드박스 VM이 이 이미지를 그대로 배포하면 GPU도 llama-server도 없는 그 VM이 프로덕션 리뷰 컨슈머 그룹에 합류해 실제 리뷰를 타임아웃으로 죽인다 — 이 기능이 막으려는 사고를 정확히 재현하는 셈이라 반드시 고쳐야 한다.

- 컨슈머별 개별 플래그(`review_consumer_enabled`/`comment_answer_consumer_enabled`/`sandbox_probe_consumer_enabled`, 위 "선행 작업" 참고, 기존 두 개는 기본 true로 하위호환).
- 샌드박스 VM 배포는 `review_consumer_enabled=false`, `comment_answer_consumer_enabled=false`, `sandbox_probe_consumer_enabled=true`.
- 컨슈머 그룹명은 기존 `dovi-ai-<domain>-engine` 패턴에 맞춰 `dovi-ai-sandbox-probe-engine`.
- Kafka 컨슈머 기본 `max_poll_interval_ms`(5분)가 잡 wall-clock 상한(15분)보다 짧다 — 이 컨슈머는 `max_poll_interval_ms`를 20분 이상으로 명시적으로 설정하거나, 프로브 실행을 poll 루프 밖 태스크로 빼고 `pause()`/`resume()` 패턴을 쓴다.
- 이 VM용 별도 `docker-compose.yml`과 CD 잡(별도 SSH 시크릿, 별도 compose 경로)이 필요하다 — 기존 `cd.yml`은 단일 호스트/단일 compose 파일을 하드코딩한다.
- 신규 이벤트는 `review-dispatcher`의 메인 리뷰 발행 **이후** 완전히 독립된 경로에서 발행한다 — 이 경로가 실패해도 메인 리뷰 발행에 영향 없다. 새 컨슈머 그룹은 새 토픽만 구독하므로 과거 이벤트 리플레이 문제가 없다.

## 워커 실행 위치

워커는 **호스트 프로세스로 고정**한다(compose 서비스로 컨테이너화하지 않음). clone/git/docker CLI가 전부 호스트에 이미 있고, 형제 컨테이너 볼륨 마운트 경로 문제가 생기지 않는다. Docker 데몬 접근 권한은 이 프로세스만 가지며, 이 프로세스가 뚫리면 VM 전체(동시 실행 중인 다른 잡들의 clone 자격증명 포함)가 블라스트 반경이라는 전제를 명시해둔다.

## 클론

- **ref**: `head_sha` 고정 checkout. 브랜치 tip을 clone하면 이벤트 발행과 clone 사이 새 커밋이 푸시됐을 때 다른 코드를 검증하고 결과를 옛 `head_sha`에 귀속시키는 TOCTOU가 생긴다.
- **토큰 처리**: Kafka 이벤트에는 토큰을 싣지 않고 `repoFullName`(+ installation 조회에 필요한 최소 정보)만 싣는다. 워커가 잡을 **실제로 시작하기 직전에** 확장된 `installation-token` 모듈을 호출해 `contents:read` 스코프 토큰을 그 자리에서 받는다 — 대기열에서 기다리는 동안 토큰을 들고 있지 않으므로 만료 문제가 없다. 컨테이너 안에서 `git clone https://x-access-token:TOKEN@...`을 실행하면 토큰이 `.git/config`에 평문으로 남으므로, **워커가 컨테이너 밖에서 clone**하고 `.git/config`에서 인증 정보를 제거한 워킹트리만 볼륨 마운트한다.

## 오프라인 빌드 — 2단계 컨테이너

네트워크 완전 차단 상태에서 의존성 설치는 그냥 실패한다. 표준 CI 2단계 패턴을 쓴다:

1. **install 단계 컨테이너**: 네트워크는 열려 있되 패키지 레지스트리로만 egress 허용, 시크릿 없음, `--cap-drop=ALL`. 락파일 종류(`pnpm-lock.yaml`/`package-lock.json`/`yarn.lock`)로 분기해서 `install --frozen-lockfile` 실행(미지원 락파일이면 이 잡은 `inconclusive`). 결과 `node_modules`를 볼륨으로 스냅샷.
2. **프로브 컨테이너**: 그 볼륨을 마운트하고 **외부 네트워크 완전 차단**(같은 잡 네트워크 안의 사이드카/mock 서버만 접근 가능). 여기서 빌드+프로브 실행.

"완전 차단"은 install 단계에 한해 레지스트리 접근만 허용하는 걸로 완화된다 — 임의 코드가 실제로 도는 지점(빌드/실행)은 여전히 완전 격리라는 트레이드오프를 문서화해둔다. egress 화이트리스트를 어떻게 강제할지(DNS/IP 필터 vs 사내 레지스트리 프록시)는 구현 단계에서 정한다.

## 격리 단위 및 컨테이너 격리 세부사항

잡 하나당 전용 Docker 네트워크(`sandbox-probe-<job-id>`)를 즉석 생성, 외부 인터넷 라우트 없음(잡 안의 모든 컨테이너에 예외 없이 적용). 잡 종료 시(성공/실패/타임아웃 상관없이) 정리한다.

- `--user`로 비-root 실행, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, 가능한 곳은 `--read-only` 루트FS, `--pids-limit`(포크 폭탄 방지).
- `/var/run/docker.sock`은 프로브/install 컨테이너에 마운트하지 않는다 — 워커 프로세스만 갖는다.
- 워커→컨테이너 명령 전달은 셸 문자열 조립이 아니라 argv 배열로.
- 같은 네트워크 **안**에서의 컨테이너 간 통신(프로브 스크립트가 mock 서버나 앱 자신에게 요청)은 막지 않는다.

### 대상 레포 `docker-compose.yml` 재사용 — 화이트리스트 필드만

대상 레포가 자체 `docker-compose.yml`에 DB 등을 선언하고 있으면(`Expo-Form-Server`가 실제로 그렇다) 재사용하되, 그 파일은 PR이 수정 가능한 공격자 제어 입력이므로 **그대로 실행하지 않는다.** 워커가 해당 서비스 정의에서 `image`, `environment`, `command`, `healthcheck`만 화이트리스트로 추출하고, `ports`/`volumes`/`privileged`/`network_mode`/`pid`/`cap_add`/`devices`/`build`/`extends`는 전부 드롭한 뒤 `docker run` argv를 조립한다. `docker compose up`을 그대로 호출하지 않는다.

## 환경변수 분류

분류 기준: PR head 커밋 시점의 `.env.example` ∪ 소스 정적 스캔(`\.(get|getOrThrow)(<[^>]*>)?\(\s*['"\`]([A-Z0-9_]+)['"\`]` + `process\.env(\.|\[['"])([A-Z0-9_]+)` 패턴 — 특정 변수명(`configService` 등)에 의존하지 않고 메서드 호출 형태로 매칭).

1. **DB/Redis 패턴** (`DATABASE_URL`, `POSTGRES_*`, `REDIS_*` 등) → 사이드카 컨테이너(가능하면 대상 레포 compose 정의 화이트리스트 재사용). **연결 문자열의 host/port를 반드시 사이드카 주소로 재작성**한다 — 재작성 안 하면 앱이 `localhost`(컨테이너 자기 자신)에 연결하려다 부팅이 깨지고, 그 결과 생명주기 프로브의 positive control이 `skip`되어 정작 잡아야 할 버그를 못 잡는다(실측으로 확인됨).
2. **URL/웹훅 패턴** (`*_URL`, `*_WEBHOOK*`, 1번 제외) → mock 캐치올 서버. `GET /_received`로 조회 가능.
3. **일반 설정값** (`PORT`, `NODE_ENV`, `TZ`, `LOG_LEVEL`, `HOST`, `*_PORT`, `*_TIMEOUT*`, 불리언성 `*_ENABLED`/`*_REJECT_*` 등) → **랜덤값이 아니라 합리적 기본값**(`PORT=3000`, `NODE_ENV=test` 등)을 주입한다. 랜덤 hex를 `PORT`에 넣으면 `app.listen("randomhex")`로 부팅이 즉시 깨진다는 게 실측으로 확인됐다. `NODE_ENV`는 반드시 non-production(스키마 자동 생성이 여기 걸린 레포가 있다).
4. **그 외 불투명한 시크릿**(JWT_SECRET, 암호화 키 등) → 랜덤 더미 값(32바이트 hex).

`.env.example`이 없는 레포는 소스 스캔 결과만으로 진행하고, 그래도 못 찾은 변수 때문에 부팅이 실패하면 그 잡은 `inconclusive`로 마감한다(추측으로 값을 만들어내지 않는다).

## 고정 프로브 스위트

각 프로브는 독립적으로 판정하며, 하나라도 `found_issue`면 잡 전체가 `found_issue`다(나머지 프로브도 계속 실행해서 evidence를 모은다).

### 프로브 1: 초기화 순서 프로브

**v2는 `madge --circular`를 썼으나 실측으로 반증됐다** — 현재 `develop` HEAD에도 순환 import가 2건 상시 존재해서(PR #9가 하나 더 추가함), 버그 수정 여부와 무관하게 항상 `found_issue`가 나온다. 대신 실제로 검증한 방식:

빌드된 `dist/` 트리 안의 **각 `.js` 파일을 개별 진입점으로 import 시도**한다:

```
node --input-type=module -e "await import('<dist>/<path>.js')"
```

실측 결과(2026-09-23, `Expo-Form-Server` 수용 기준 fixture):
- 버그 커밋(`07045bcb0d9a`): `dist/form/entities/form.entity.js` 개별 import → `ReferenceError: Cannot access 'FormEntity' before initialization` (exit 1)
- 수정 커밋(`b13836e9136d`): 같은 import → 성공(exit 0)

반대 순서(`dynamic-form.entity.js`를 먼저 import)는 두 fixture 모두 통과해 판별력이 없었다 — 그래서 특정 파일을 먼저 시도하는 게 아니라 **`dist/` 안의 모든 `.js` 파일 각각을 독립적으로 시도하고, 하나라도 실패하면 `found_issue`**로 일반화한다. 외부 도구나 네트워크가 필요 없다(플레인 `node`만, 이미 빌드 단계에 있음) — v2의 "네트워크 차단인데 madge를 받아야 하는" 모순이 이 교체로 해소된다.

파일 수가 많은 레포에서는 이 방식이 O(n) 프로세스 기동이라 느릴 수 있다 — 병렬 실행하거나 순환참조가 실제로 있는 파일만 후보로 좁히는 최적화는 구현 단계에서 조정.

### 프로브 2: 생명주기 프로브

1. env 분류로 구성한 샌드박스에서 앱 기동.
2. **positive control**: mock 서버에 시작 알림이 도착하는지 확인(수 초 대기). 안 오면 이 앱이 애초에 그런 알림을 보내는 구성이 아니라는 뜻이므로 `skip` 처리 — "종료 알림도 안 왔다"만 보고 `found_issue`로 단정하지 않는다.
3. 왔으면: `SIGTERM` 전송 → 5초 대기(실측 기준 여유 확보 — 실제로는 ~450ms 내 도착했다) → mock에 종료 알림 도착 확인. 안 오면 `found_issue`.

실측 결과(2026-09-23, 로컬 postgres 사이드카 + mock 웹훅 서버, 컨테이너 아닌 bare 프로세스로 검증):
- 두 fixture 다 기동 1초 내 시작 알림 도착(positive control 정상 동작 확인).
- 버그 커밋(`8a4f5f9a2dc7`): `SIGTERM` → 5초 대기해도 종료 알림 없음.
- 수정 커밋(`482db26e3993`): `SIGTERM` → ~450ms 후 종료 알림 도착.

**v2의 PID 1 근거를 정정한다.** v2는 "컨테이너에서 PID 1은 핸들러 없는 시그널을 커널이 무시하므로 `--init`이 없으면 판정이 안 된다"고 했다. 실측은 **컨테이너/PID 1이 아닌 평범한 프로세스**로도 버그 커밋에서 동일하게 SIGTERM이 무시됨을 보여준다 — 원인은 컨테이너의 PID 1 시그널 처리가 아니라, **`enableShutdownHooks()`가 SIGTERM 리스너를 등록하는지 여부**다. 리스너가 없으면 Node의 기본 동작(핸들러 없는 SIGTERM = 즉시 종료, 알림 없음)이 적용될 뿐이다. 판정은 컨테이너 환경과 무관하게 정확히 갈린다. `--init`은 여전히 컨테이너 안에서 좀비 프로세스 정리 등 무해한 보강으로 유지하되, "이게 없으면 판별이 안 된다"는 인과관계 서술은 삭제한다.

### 기동 성공 판정 사다리

`/health` 엔드포인트(있으면) → 없으면 지정 PORT로 TCP 연결 성공 → 그래도 판정 안 되면 positive control 도착 여부. 셋 다 실패하면 `inconclusive`.

## 프로브 실패 전이 규칙

- install 실패 → 전체 `inconclusive`, 이후 프로브 실행 안 함.
- 빌드 실패 → 초기화 순서/생명주기 프로브 둘 다 실행 불가(산출물 없음)이므로 스킵, 빌드 실패 자체를 `found_issue`로 보고.
- 빌드 성공 → 초기화 순서 프로브 실행(실패해도 나머지 프로브는 계속). 생명주기 프로브 진행.
- 생명주기 프로브에서 기동 자체가 실패(빌드는 성공했는데 앱이 안 뜸) → 환경 기인(Node/pnpm 버전 불일치, 사이드카 문제)인지 코드 버그인지 자동 구분이 어려우므로 `inconclusive`로 보수적 처리(오탐 방지 우선).

## 환경 버전(Node/pnpm) 불일치로 인한 오탐 방지

대상 레포에 `engines`/`packageManager` 필드가 없으면 CI 워크플로 파일에서 버전을 파싱해 프로브 이미지 버전을 맞춘다. 그래도 못 찾으면 레포별 설정 기본값을 쓰거나 지원 대상에서 제외한다 — 추측한 버전으로 밀어붙여서 환경 기인 실패를 `found_issue`로 오보하지 않는다.

## 리소스 캡 / 디스크

VM 전체 예산(4 vCPU / 15Gi RAM)에서 호스트 OS/Docker 데몬/워커 프로세스 몫(~1 vCPU / 2GB)을 먼저 예약한 뒤, 나머지를 동시 잡 수로 나눠 컨테이너별 `--memory`/`--cpus` 캡을 건다(DB/Redis 사이드카 포함 잡 안의 모든 컨테이너에 예외 없이).

일반 docker named volume에는 (overlay2/ext4 기본 구성에서) 실질적 크기 제한이 없다 — tmpfs는 RAM을 소모하므로 쓰지 않는다. 대신: 잡 시작 전 여유 디스크 체크 + 워커의 주기적 사용량 감시 + 상한 초과 시 강제 종료. pnpm store처럼 잡 간 공유되는 캐시는 총량 상한 + LRU 축출.

## 포이즌 잡 방지

`ai-review:sandbox-probe-attempts:<reviewJobId>` 키로 Redis에 카운터를 두고, 프로브 **시작 직전**에 `INCR`한다(프로세스가 도중에 죽어도 증가분은 남는다 — 인메모리 카운터는 워커가 죽으면 리셋되므로 안 씀). N회(2회) 초과 시 `inconclusive` 발행 후 강제 커밋.

## Dedup

`RedisDedupStore`는 `reviewJobId` 키를 Dovi-github-app과 공유하며 과거 실제 충돌 사고(#40)가 있었다. 전용 prefix(`ai-review:sandbox-probe-dedup:`)로 락을 분리한다. 워커 프로세스가 통째로 죽으면 키가 `in_progress`로 남을 수 있어, 이 트랙은 짧은 TTL(예: 30분, 메인 리뷰의 24시간보다 짧게)을 쓴다.

## 고아 리소스 정리

모든 잡 리소스(컨테이너/네트워크/볼륨)에 `label=dovi.sandbox.job=<id>`를 붙인다. 워커 기동 시 이 라벨 기준으로 고아 리소스를 먼저 수거(reaper)한다 — `graceful_shutdown_seconds`(130초)가 15분짜리 잡보다 훨씬 짧아 배포 중 `finally`가 못 돌 가능성이 높으므로, "배포 중 진행 잡은 유실 전제, reaper가 다음 기동 때 정리"를 명시적 정책으로 둔다.

## 트리거 조건 및 남용 방지

- 지원 스택(v1: NestJS) 소스 코드를 건드리는 **같은 레포 브랜치 PR**이면 실행한다(문서 전용 PR은 스킵). **lockfile-only PR은 스킵하지 않는다** — 의존성 범프야말로 정적 분석이 못 잡고 실제 빌드/기동으로만 드러나는 대표 사례라 목표와 상충하기 때문이다.
- **레포별 opt-in 필요**: 이 코드베이스의 기존 `DOVI.md` 레포별 설정 선례를 따라, 샌드박스 프로브를 켤지 명시하게 한다 — 조직 전체 설치 상태에서 트리거 조건만으로 전체 NestJS 레포가 자동 대상이 되는 걸 막는다.
- **fork PR은 v1에서 제외**한다(webhook DTO 확장이 선행 작업, 위 참고).
- **발행 측(Dovi-github-app) 킬스위치**도 필요하다 — 컨슈머 플래그만으로는 이벤트가 계속 쌓여서 재활성화 시 리플레이된다. 발행 여부를 끄는 플래그를 github-app 쪽에도 두고, 컨슈머를 발행보다 먼저 배포한다.
- "위험 패턴 감지 시에만 실행" 같은 선별 트리거는 도입하지 않는다.

## 결과 표현

`pr.sandbox.probe.requested` (신규 토픽, dot-separated 컨벤션):

```
reviewJobId: str        # Kafka key로도 사용
repositoryId: int
repoFullName: str       # owner/repo — clone에 필수. "한쪽만 쓰는 필드는 이벤트에 안 싣는다"는
                         # 기존 컨벤션의 의도적 예외(ai-server가 실제로 clone에 소비) —
                         # docs/kafka-event-schema.md 갱신 시 이 예외를 명시한다.
prNumber: int
headSha: str
baseSha: str
```

(이 이벤트가 발행됐다는 것 자체가 스택 감지+opt-in+같은 레포 브랜치 조건을 통과했다는 뜻이므로 `stack` 필드는 두지 않는다 — v1 범위에선 NestJS 고정.)

`pr.sandbox.probe.completed`:

```
reviewJobId: str
repositoryId: int
prNumber: int
headSha: str
status: "passed" | "found_issue" | "inconclusive"
evidence: str            # 최대 8KB, 초과 시 뒷부분 우선 보존(빌드 에러는 보통 출력 끝에 나온다)
findings: list[Finding]  # 최대 10개
```

`Finding` (`ReviewComment` 재사용 안 함 — `line: int = Field(gt=0)`이 필수인데 "생명주기 훅 누락"류 버그는 가리킬 특정 라인이 없다):

```
probe: "init_order" | "lifecycle" | "build"
title: str
message: str
filePath: str | None
line: int | None
evidence: str             # 최대 4KB
```

이벤트 전체 직렬화 크기 상한 512KB(Kafka `message.max.bytes` ~1MB, 기존 `CHANGED_FILE_CONTENT_TOTAL_BUDGET` 512KB 관례에 맞춤). 게시 전 evidence는 본문에 등장하는 최장 백틱 런보다 긴 코드펜스로 감싸고 `@` 멘션을 무력화한다.

**Phase 1은 어떤 경로로도 LLM을 호출하지 않는다.** 요약 문구는 프로브 스크립트가 고정 템플릿으로 생성한다(v2에 있던 "필요하면 LLM으로 다듬는다"는 조건부 서술은 "LLM 개입 없음" 보안 주장과 같은 문서에서 모순되므로 완전히 삭제). LLM 요약이 필요해지면 Phase 2에서 인젝션 대응과 함께 다룬다.

- 게시는 마커 주석 기반 **sticky 코멘트 upsert**(신규 독립 컨슈머 모듈, `comment-answer-result`를 선례로 삼음). 항상 게시(통과/문제발견/판단불가 상관없이). 상태별 이모지(✅/🐛/⚠️).
- 메인 리뷰 코멘트는 건드리지 않는다.

## 보안 고려사항 요약

- LLM이 개입하지 않으므로 v1의 핵심 위협(PR 코드 출력이 LLM을 거쳐 위조된 검증 결과로 둔갑)이 원천적으로 없다. 남는 위협은 evidence 텍스트의 마크다운/멘션 인젝션(코드펜스+멘션 무력화로 대응)뿐이다.
- installation token: `contents:read`만 스코프, 잡 시작 직전 발급, Kafka에 원문 미포함, 컨테이너에는 인증정보 제거된 워킹트리만.
- 대상 레포의 `docker-compose.yml`/일반 셸 명령은 화이트리스트 필드/argv 배열로만 다뤄 인젝션·권한 상승 경로를 차단.
- 네트워크는 install 단계(레지스트리만)를 제외하고 완전 차단, 모든 컨테이너 예외 없이.
- 프로덕션 GPU 박스와 물리적으로 분리된 전용 VM.
- fork PR 제외로 v1 공격 표면을 같은 레포 기여자로 제한.

## 문서화 후속 작업

`Dovi-github-app`의 `docs/kafka-event-schema.md`에 신규 토픽 2개(`pr.sandbox.probe.requested`/`.completed`)와 스키마, `repoFullName` 필드가 기존 컨벤션의 의도적 예외라는 점을 기록한다.

## 테스트 전략

- "수용 기준" 4개 fixture 재생을 통합 테스트로 만들어 CI에 포함.
- Docker 오케스트레이션 워커는 Docker 클라이언트를 추상화하고 테스트에서는 fake/stub으로 교체.
- 프로브 판정 로직(초기화 순서 파싱, positive control 체크, 상태 전이)은 Docker 실행과 분리된 순수 함수로 만들어 단위 테스트 가능하게 한다.

## 열린 리스크 / 후속 과제

- install 단계의 "레지스트리로만 egress 허용"을 실제로 어떻게 강제할지(DNS/IP 화이트리스트 vs 사내 프록시)는 구현 단계에서 정한다.
- GitHub App의 `issues: write` 권한 보유 여부 확인이 선행돼야 한다(위 "선행 작업" 참고).
- 초기화 순서 프로브가 파일 수가 많은 레포에서 느릴 수 있다 — 최적화는 실제 운영 데이터를 보고 판단.
- 향후 Java/Gradle 어댑터 추가 시 VM 사이징 재검토 필요.
- Phase 2(에이전트 루프)로 넘어갈 시점/조건은 운영하면서 구체화한다.
