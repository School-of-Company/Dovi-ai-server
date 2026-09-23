# 샌드박스 프로브 (Sandbox Probe) 설계

> v1 문서 리비전. Opus 아키텍처 리뷰(2026-09-23)에서 원안(LLM 에이전트 루프 기반)이 최소 3개 지점에서 산술적/논리적으로 동작 불가능하고, 본질적 위협인 프롬프트 인젝션이 누락되었으며, 두 동기 사례 모두 LLM 없이 결정론적 프로브로 잡을 수 있다는 지적을 받아 전면 재설계했다. 기능명도 `app/review/pipeline.py`의 기존 `_verify`/`_build_verify_messages`/`VerificationResult`와의 이름 충돌을 피해 "빌드/실행 검증 에이전트" → "샌드박스 프로브"로 바꿨다.

## 배경

최근 5일 내 실제 PR 두 건에서, 사람 리뷰어(cfcromn)가 로컬에서 직접 빌드/서버 기동을 해봐서 Dovi가 놓친 런타임 버그를 잡아냈다.

1. **`School-of-Company/Expo-Form-Server` PR #5** — `src/form/entities/dynamic-form.entity.ts:42`의 `form: FormEntity` 필드가 ESM 순환참조 + `emitDecoratorMetadata` 상호작용으로 `Cannot access 'FormEntity' before initialization`을 일으킴. 일반적인 `pnpm build && pnpm start`로는 재현되지 않고, `FormEntity`를 먼저 import하는 특정 순서에서만 재현됨.
2. **`School-of-Company/Expo-Form-Server` PR #12** — `app.enableShutdownHooks()` 누락으로 `SIGTERM` 시 `OnApplicationShutdown` 훅이 실행되지 않아, Discord 웹훅 종료 알림이 발송되지 않음. cfcromn이 로컬에서 실제로 서버를 띄우고 SIGTERM을 보내 웹훅 도착 여부를 확인해서 발견했다.

두 케이스 모두 Dovi의 현재 파이프라인(diff + AST 컨텍스트 + RAG를 프롬프트에 넣고 LLM 한 번 호출)으로는 구조적으로 잡을 수 없다 — 정적으로 코드를 읽는 것만으로는 불가능하고(#1은 타입 체크도 통과하는 정상 코드), 고정된 테스트 스위트를 도는 것으로도 잡히지 않는다(둘 다 기존 테스트에 없는 시나리오).

**원안은 "diff를 보고 가설을 세우는 LLM 에이전트"가 필요하다고 가정했지만, 실제로는 둘 다 결정론적 프로브로 잡힌다:**

- PR #12 유형(생명주기 훅 누락) → 기동 → **시작 알림 수신 확인(positive control)** → SIGTERM → **종료 알림 수신 확인**. 고정 스크립트다.
- PR #5 유형(ESM 순환참조) → `madge --circular`(순환참조 전용 정적 도구) + 엔티티 파일들을 각각 개별 진입점으로 import해보는 결정론적 순열 프로브.

LLM이 필요 없다는 건 이 기능의 위험 표면(프롬프트 인젝션, 스텝 캡 산정, 프로토콜 파싱 신뢰성, 컨텍스트 예산)이 통째로 없어진다는 뜻이다. 그래서 v1은 **LLM 없는 결정론적 프로브 하네스**로 간다. 에이전트 루프는 Phase 2로 명시적으로 미룬다(아래 참고).

## 목표

Dovi 리뷰 파이프라인에, PR 코드를 실제로 빌드/기동해서 정적 분석으로 못 잡는 런타임 이슈를 잡는 별도 비동기 트랙을 추가한다. 메인 리뷰(현재 SLA, 현재 LLM 호출량)에 영향을 주지 않는다.

## 비목표 (v1 범위 제외)

- Java/Gradle, Python 등 NestJS 외 스택 지원 — 인터페이스는 확장 가능하게 설계하되, v1 구현체는 NestJS 하나만.
- 실제 서드파티 API(결제, 실제 외부 LLM 등)처럼 로컬로 흉내낼 수 없는 의존성 검증.
- **LLM 기반 가설 탐색 에이전트 루프** — Phase 1(이 문서)이 안정화되고, 고정 프로브로 못 잡는 케이스가 실제로 쌓인 뒤에 별도 스펙으로 재검토한다. 이번 스펙에서 다루지 않는다.
- fork PR 지원 — v1은 같은 레포 브랜치에서 연 PR만 대상으로 한다(아래 "남용 방지" 참고).
- 메인 리뷰의 응답 시간이나 LLM 호출량에 영향을 주는 어떤 변경도 포함하지 않음.

## 단계 구분

- **Phase 0** (선행, 이 기능과 독립적인 인프라 정리): VM 프로비저닝 완료(끝남), llama-server 접근 경로를 방화벽 룰이 아니라 **기존 관례(SSH 터널/WireGuard)에 맞는 방식**으로 정리. 아래 "인프라" 절 참고.
- **Phase 1** (이 스펙의 구현 대상): LLM 없는 결정론적 프로브 하네스.
- **Phase 2** (비목표, 별도 스펙): Phase 1 위에 에이전트 루프를 얹는 것. 이때는 install/build가 이미 끝난 상태에서 시작하므로 스텝 캡·타임아웃 압박이 자연히 줄어든다.

## 아키텍처 개요

```
GitHub PR 이벤트
  → Dovi-github-app: review-dispatcher (pr-data-collector가 아니라 이 모듈이 발행 주체)
      - 기존: 파일 content 수집 → ReviewRequestPayload 발행
        (TS 쪽 타입명은 ReviewRequestPayload, Python 쪽 스키마명은 ReviewRequestedEvent —
        와이어 포맷은 동일 이벤트의 두 언어 표현이다)
      - 신규: 스택 감지(package.json에 @nestjs/core 존재) + 같은 레포 브랜치 PR일 때만,
        installation-token 모듈을 확장해 받은 contents:read 스코프 단기 참조를
        cloneRef 필드로 실어 별도 신규 이벤트로 발행 (기존 메인 리뷰 발행 경로와 완전히 분리 —
        아래 "이벤트 분리" 참고)
  → Dovi-ai-server: 기존 review 컨슈머 그룹 (dovi-ai-review-engine) — 완전히 영향 없음
  → Dovi-ai-server: 신규 dovi-sandbox-probe 컨슈머 그룹, 신규 토픽 전용 구독
      - 전용 VM에서 실행 (샌드박스 격리를 프로덕션 GPU 박스와 분리)
      1. 워커(컨테이너 밖)가 참조로 실제 installation token을 교환받아 head_sha 고정 clone
         (컨테이너에는 토큰이 제거된 워킹트리만 볼륨 마운트 — 토큰 자체는 컨테이너에 넣지 않음)
      2. 스택 어댑터가 env 3버킷 분류 + 사이드카 필요 여부 결정
      3. 격리된 잡 전용 Docker 네트워크 기동
      4. 고정 프로브 스위트 순차 실행 (LLM 호출 없음)
      5. 종료 시 컨테이너·네트워크·볼륨 전부 폐기, 상태 확정
  → Kafka로 SandboxProbeCompletedEvent 발행 — 신규 토픽 `pr.build.verify.completed`
     (dot-separated 세그먼트 컨벤션, `docs/kafka-event-schema.md` 갱신 포함)
  → Dovi-github-app: 신규 독립 컨슈머 모듈 (기존 `comment-answer-result`를 선례로 삼음 —
     기존 `github-app-review-result` 그룹에 토픽을 얹지 않는다. 책임이 섞이고
     재배포 때 불필요한 리밸런스가 생긴다)
      - SandboxProbeCompletedEvent 수신 시 마커 주석 기반 sticky 코멘트를 upsert
        (재푸시마다 새 코멘트가 쌓이지 않게)
```

### 이벤트 분리 (원안의 구조적 문제 해결)

원안은 메인 리뷰 이벤트에 `stack`/`clone_token` 필드를 얹고 새 컨슈머 그룹이 같은 토픽을 구독하는 방식이었다. 세 가지 문제가 있었다:

1. 새 그룹이 기존 `pr.review.requested` 토픽에 처음 붙으면 리텐션에 남은 과거 요청을 전부 리플레이한다 — 닫힌 옛날 PR에 대량으로 코멘트가 붙는다.
2. 발행 직전에 스택 감지+토큰 발급을 끼워넣으면 메인 리뷰 이벤트 자체가 지연되고, 토큰 발급 실패가 메인 경로에 전파될 수 있다.
3. 토큰이 필요 없는 기존 review 컨슈머 그룹도 같은 토픽이라 토큰을 함께 수신한다 — 최소 권한 위반.

그래서 **신규 이벤트를 완전히 별도 토픽으로 분리**한다: `review-dispatcher`가 메인 리뷰 이벤트를 발행한 **이후**, 독립적인 비동기 경로에서 스택 감지+토큰 발급을 수행해 `pr.build.verify.requested`(신규 토픽)로 발행한다. 이 경로가 실패해도 메인 리뷰 발행에는 영향이 없다. 새 컨슈머 그룹은 새 토픽만 구독하므로 과거 이벤트 리플레이 문제 자체가 없다.

## 인프라

전용 VM 1대 (프로덕션 GPU 박스와 분리):

- **스펙**: 4 vCPU / 15Gi RAM / 58G 디스크, GPU 없음
- **동시 처리**: 최대 4개 잡
- **LLM 추론**: v1(Phase 1)은 LLM 호출이 없다 — 프로브 결과의 사람이 읽을 요약 문구 정도만 필요하면 그건 별도 저비용 후처리이고, 판정(`passed`/`found_issue`/`inconclusive`) 자체는 프로브 스크립트가 결정한다.
- **llama-server 접근**: Phase 2를 대비해 네트워크 경로만 미리 확인해둔다. 프로덕션 박스는 현재 방화벽이 없고 `127.0.0.1` 바인딩 + SSH 터널이 실제 보안 모델이다(`docker-compose.yml` 주석 확인). "방화벽 룰로 이 VM IP만 허용"은 방화벽이 없는 호스트에서 포트를 외부로 여는 것이라 오히려 노출을 넓힌다 — **기존 관례에 맞게 SSH 터널이나 WireGuard로 연결**하거나, 혹은 호스트 방화벽 도입 자체를 이 기능과 무관한 별도 선행 작업(Phase 0)으로 분리한다. Phase 1은 이 경로를 안 쓰므로 지금 결정할 필요는 없다.
- **분리 이유**: 이 트랙은 PR에 포함된, 신뢰할 수 없는 코드를 실제로 빌드/실행한다. 프로덕션 GPU 박스에 이 워크로드를 얹으면 악의적인 PR이 자원을 고갈시켜 실제 서비스에 영향을 줄 수 있다. 물리적으로 분리하면 cgroup 설정 실수가 있어도 블라스트 반경이 이 VM 안으로 국한된다.
- **접속 정보**: 별도로 로컬 메모리에만 보관, 이 스펙 문서나 git에는 포함하지 않는다.

## 배포 단위

`app/main.py`의 현재 lifespan은 모든 Kafka 컨슈머를 한 프로세스에서 `kafka_consumer_enabled` 플래그 하나로 게이팅해서 띄운다. `dovi-sandbox-probe` 컨슈머는 이 전용 VM에서만 돌아야 하므로:

- 컨슈머별 개별 enable 플래그를 추가한다(`sandbox_probe_enabled: bool = False`, 이 프로젝트의 기존 관례 — `rag_enabled`, `notion_sync_enabled` 등과 동일한 패턴).
- 이 VM에는 같은 이미지를 다른 환경변수 조합으로 배포하되 `sandbox_probe_enabled=true` + 다른 컨슈머는 전부 꺼서, 이 프로세스가 오직 이 컨슈머만 돈다.
- 이 VM용 별도 `docker-compose.yml`(또는 override 파일)이 필요하다 — 프로덕션 박스의 compose 파일과는 다른 스택(sandbox-probe 서비스 + Docker-in-Docker 접근)을 정의해야 한다.

## 샌드박스 설계

### 격리 단위

잡 하나당 전용 Docker 네트워크(`sandbox-probe-<job-id>`)를 즉석 생성한다. 이 네트워크는 외부 인터넷으로 나가는 라우트가 없다 — **잡 안의 모든 컨테이너(프로브 실행 컨테이너뿐 아니라 DB/Redis 사이드카까지)에 예외 없이** 적용한다. 잡 종료(성공/실패/타임아웃 상관없이) 시 컨테이너·네트워크·볼륨을 `finally`에서 강제 삭제한다.

같은 네트워크 **안**에서의 컨테이너 간 통신(예: 프로브 스크립트가 mock 서버나 방금 띄운 앱 자신에게 `curl`)은 막지 않는다.

**컨테이너 격리 세부 사항** (원안에서 누락됐던 부분, 명시):

- `--user`로 비-root 실행, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, 기본 seccomp 프로파일 유지, 가능한 경로는 `--read-only` 루트FS + 필요한 경로만 별도 볼륨.
- `/var/run/docker.sock`은 프로브 실행 컨테이너에 **마운트하지 않는다** — 마운트가 필요한 오케스트레이션 로직은 워커(호스트 프로세스)에만 있다.
- 워커가 컨테이너 안에서 명령을 실행할 때는 셸 문자열 조립이 아니라 **argv 배열**로 전달한다(호스트 셸 인젝션 방지).
- 클론한 워킹트리에는 **토큰이 빠진 상태로만** 마운트한다(아래 "토큰 처리" 참고) — 프로브 스크립트가 `cat .git/config`를 실행해도 유출될 게 없다.

### 클론

- **ref**: `head_sha`를 고정해서 clone/checkout한다. 브랜치 tip을 clone하면 이벤트 발행과 clone 사이에 새 커밋이 푸시됐을 때 다른 코드를 검증하고 결과를 옛 `head_sha`에 귀속시키는 TOCTOU가 생긴다.
- **수행 주체**: 워커(컨테이너 밖)가 clone을 수행한다. 컨테이너 안에서 `git clone https://x-access-token:TOKEN@...`을 실행하면 토큰이 `.git/config`에 평문으로 남기 때문이다. 워커가 clone한 뒤 `.git/config`에서 인증 정보를 제거한 워킹트리 디렉토리만 컨테이너에 볼륨 마운트한다.

### 환경변수 3버킷 분류

분류 기준 파일: **PR head 커밋 시점의 `.env.example`**을 우선 사용하되, 거기 없는 변수라도 소스 전체를 정적으로 스캔(`process.env.X`, `configService.get('X')` 패턴 매칭)해서 참조되는 모든 환경변수를 후보에 포함시킨다. `.env.example`만 보면 그 파일이 갱신 안 된 레포에서 신규 변수를 놓친다(실제로 PR #12 이전 `Expo-Form-Server`의 `.env.example`에는 `DISCORD_WEBHOOK_URL`이 없었다 — 그 PR이 추가한 변수였다).

1. **DB/Redis 패턴** (`DATABASE_URL`, `POSTGRES_*`, `REDIS_*` 등) → 대상 레포가 이미 자체 `docker-compose.yml`에 해당 서비스를 선언하고 있으면 **그 정의를 그대로 재사용**한다(이미지, 자격증명, healthcheck까지 정확함 — `Expo-Form-Server`가 실제로 이렇게 하고 있다). 없으면 즉석으로 표준 사이드카를 띄운다.
2. **URL/웹훅 패턴** (`*_URL`, `*_WEBHOOK*` 등, 1번 제외) → 범용 mock 캐치올 서버(같은 네트워크 안)를 가리키게 함. 이 mock 서버는:
   - 들어오는 모든 요청을 타임스탬프와 함께 기록하고 200 반환
   - `GET /_received`로 지금까지 받은 요청 목록 조회 — 프로브 스크립트가 확인하는 데 씀
3. **그 외 필수 환경변수** (JWT_SECRET, 암호화 키 등) → 랜덤 더미 값(32바이트 hex) 주입.

`.env.example`이 아예 없는 레포는 소스 스캔 결과만으로 진행하고, 스캔으로도 못 찾은 변수 때문에 부팅이 실패하면 그 잡은 `inconclusive`로 마감한다(추측으로 값을 만들어내지 않는다).

### 리소스 캡

동시 4잡 기준으로 VM 전체 예산(4 vCPU / 15Gi RAM)에서 **호스트 OS/Docker 데몬/워커 프로세스 몫을 먼저 뺀 뒤**(약 1 vCPU / 2GB 예약) 나머지를 잡당 나눠 컨테이너별 `--memory`/`--cpus` 하드캡을 건다. DB/Redis 사이드카 포함 **잡 안의 모든 컨테이너**에 캡을 적용한다.

볼륨은 **일반 docker volume**을 쓴다(디스크 기반 quota). tmpfs는 쓰지 않는다 — tmpfs는 RAM을 소모하므로 디스크 고갈을 막으려다 이미 빠듯한 잡별 메모리 예산에서 OOM을 유발한다.

## 고정 프로브 스위트

LLM 없이 순차 실행하는 결정론적 스크립트. 각 프로브는 독립적으로 통과/실패/스킵 판정을 내고, 하나라도 `found_issue`면 잡 전체가 `found_issue`로 끝난다(나머지 프로브는 계속 실행해서 evidence를 모을 수 있음).

1. **빌드 프로브**: 오프라인 설치(아래 "오프라인 빌드" 참고) + `nest build`(또는 레포의 실제 빌드 스크립트). 실패 시 `found_issue`, exit code + 마지막 출력 일부(끝부분 — 빌드 에러는 보통 출력 끝에 나온다)를 evidence로.
2. **순환참조 프로브**: `npx madge --circular --extensions ts src/`. 순환참조 발견 시 `found_issue`, 어떤 파일들이 순환하는지가 evidence.
3. **생명주기 프로브**: 앱을 `--init` 옵션으로(또는 셸 래퍼의 자식 프로세스로) 띄운다(PID 1이 시그널 핸들러 없는 시그널을 무시하는 커널 동작을 피하기 위해 — 이게 없으면 `SIGTERM`이 컨테이너 커널 레벨에서 무시되어 버그 유무와 무관하게 항상 "응답 없음"이 관측된다).
   - 기동 후 헬스 엔드포인트(있으면) 확인.
   - **positive control**: mock 서버에 "시작" 알림이 도착했는지 먼저 확인한다. 안 왔으면 애초에 이 앱이 그런 알림을 안 보내는 구성(예: 웹훅 URL 미설정)이라는 뜻이므로 이 프로브는 `skip`으로 끝낸다 — "종료 알림도 안 왔다"만 보고 `found_issue`로 단정하지 않는다(이게 원안의 오탐 원인이었다).
   - 시작 알림이 왔으면: `SIGTERM` 전송 → 정해진 대기 시간 → mock에 "종료" 알림 도착 확인. 안 왔으면 `found_issue`.
4. (필요시 스택 어댑터가 추가 프로브를 더 등록할 수 있는 확장 포인트만 남겨둔다 — v1에서 추가 구현은 안 함.)

### 타임아웃

동시 4잡이 4 vCPU를 나눠 쓰는(잡당 ~1 vCPU) 상황을 감안한 값:

- 빌드 프로브(오프라인 install + build): 10분
- 순환참조 프로브(`madge`): 1분
- 생명주기 프로브(기동 + positive control 대기 + SIGTERM + 종료 알림 대기): 2분
- 잡 전체 wall-clock 상한: 15분 — 초과 시 그 시점까지의 evidence로 `inconclusive` 처리하고 강제 종료

각 값은 실측 후 조정 가능하지만, 구현 단계에서 "타임아웃 없음"으로 넘어가지 않도록 초기값을 못박아 둔다.

### 오프라인 빌드

네트워크 완전 차단 상태에서 `pnpm install`은 그냥 실패한다. 워커가 잡 시작 전에 **호스트에서 레포의 lockfile 기준으로 `pnpm fetch`를 실행해 pnpm store를 만들고**, 이 store를 컨테이너에 읽기 전용으로 마운트한다. 컨테이너 안에서는 `pnpm install --offline`으로만 설치한다 — 격리를 깨지 않으면서 빌드가 가능해진다.

## 포이즌 잡 방지

프로브 실행 자체가 워커 프로세스를 OOM으로 죽일 수 있는 워크로드라(기존 리뷰 트랙에는 없던 성질), 실패 시도 횟수를 카운트한다. N회(예: 2회) 연속 크래시면 그 잡은 `inconclusive`로 강제 커밋하고 다음 메시지로 넘어간다 — 포이즌 메시지 하나가 컨슈머 그룹 전체를 영구 재시작 루프에 빠뜨리는 걸 막는다.

## 트리거 조건 및 남용 방지

- 지원 스택(v1: NestJS) 소스 코드를 건드리는 **같은 레포 브랜치 PR**이면 항상 실행한다(lockfile-only, 문서 전용 PR은 스킵).
- **fork PR은 v1에서 제외한다.** 외부 기여자의 fork에서 임의 코드를 실행하는 건 CI RCE의 전형적인 벡터(pwn request)다. fork PR 지원이 필요해지면 별도 검토(메인테이너 승인 라벨 등)를 거친다.
- "위험 패턴이 감지될 때만 실행" 같은 선별 트리거는 도입하지 않는다 — 그 감지 로직 자체가 또 하나의 정적 판단이라, 이번에 해결하려는 문제를 트리거 단계에서 반복하게 된다.
- 짧은 시간에 PR이 몰려 4슬롯이 꽉 차면 나머지는 큐에서 대기한다(레이트리밋은 자연히 큐 길이로 처리됨 — 별도 로직 불필요).

## 결과 표현

`SandboxProbeCompletedEvent` (신규 토픽 `pr.build.verify.completed`), 필드는 camelCase(이 프로젝트의 `CamelModel` 와이어 포맷 컨벤션):

```
reviewJobId: str      # ReviewCompletedEvent와 동일한 라우팅 키, Kafka 메시지 key로도 사용
repositoryId: int
prNumber: int
headSha: str
status: "passed" | "found_issue" | "inconclusive" | "skipped"
evidence: str          # 각 프로브의 실행 명령 + exit code + 출력 인용
findings: list[Finding]  # found_issue일 때 프로브별 상세
```

`Finding`은 `ReviewComment`를 재사용하지 않고 새로 정의한다 — `ReviewComment`는 `line: int = Field(gt=0)`이 필수인데, "생명주기 훅 누락"류 버그는 가리킬 특정 라인이 없다(없는 코드에 대한 지적). 대신:

```
probe: "build" | "circular_import" | "lifecycle"
title: str
message: str
filePath: str | None
line: int | None
evidence: str
```

- 게시는 **마커 주석 기반 sticky 코멘트 upsert** — 재푸시마다 새 코멘트가 쌓이지 않도록, 기존 코멘트를 찾아서 갱신한다(신규 독립 컨슈머 모듈이 이 로직을 갖는다 — 기존 `deleteStaleReviewComments`는 issue 코멘트를 다루지 않으므로 재사용 불가).
- **항상 게시**한다(통과/문제발견/판단불가/스킵 상관없이) — 이 기능이 실제로 동작 중이라는 걸 사람이 확인할 수 있어야 신뢰가 생긴다.
- 메인 리뷰 코멘트는 건드리지 않는다. 상태별 이모지(✅ 통과 / 🐛 문제 발견 / ⚠️ 판단 불가 / ⏭️ 스킵)를 텍스트 맨 앞에 붙인다.
- evidence/finding 텍스트는 게시 전 코드펜스로 감싸고 `@` 멘션은 무력화한다 — 빌드/런타임 출력에 우연히 포함된 마크다운이나 멘션이 코멘트에서 그대로 렌더링되지 않도록.

## Dedup

`RedisDedupStore`는 `reviewJobId` 키를 Dovi-github-app과 공유하며, 과거 실제 충돌 사고(#40)가 있었다. 샌드박스 프로브 트랙은 같은 `reviewJobId`를 쓰되 **전용 prefix**(`ai-review:sandbox-probe-dedup:`)로 락을 분리한다 — 메인 리뷰의 dedup 락을 빼앗지 않는다.

## 보안 고려사항

- installation token: `contents:read`만 스코프. 이걸 위해 `installation-token` 모듈 확장이 선행 작업으로 필요하다 — 현재 인터페이스(`getOctokit(installationId): Promise<Octokit>`)는 raw 토큰 반환도, `permissions`/`repositories` 파라미터도 지원하지 않는다. 확장 시 **캐시 키에 스코프를 포함**시켜야 한다(현재 캐시 키는 `github:token:${installationId}`뿐이라, 스코프 차원 없이 확장하면 좁힌 토큰이 캐시를 오염시켜 메인 리뷰 코멘트 게시가 403으로 깨질 수 있다).
- Kafka 메시지에는 실제 토큰 대신 **단명 참조 ID**를 싣고, 워커가 그 참조로 발급 API를 호출해 실제 토큰을 받는다 — Kafka 자체가 리텐션 기간 디스크에 남는 로그이기 때문에, 토큰 원문을 메시지 payload에 직접 싣지 않는다.
- 샌드박스는 외부 네트워크 완전 차단(모든 컨테이너 예외 없이), 진짜 시크릿 미주입, 잡 종료 시 전체 폐기.
- 프로덕션 GPU 박스와 물리적으로 분리된 전용 VM에서 실행.
- 리소스(CPU/메모리/디스크) 하드캡을 잡 안의 모든 컨테이너에 예외 없이 적용.
- fork PR 제외로 v1의 공격 표면을 같은 레포 기여자로 제한.
- LLM이 개입하지 않으므로(Phase 1), 원안의 핵심 위협이었던 "PR 코드 출력이 LLM을 거쳐 위조된 검증 결과로 둔갑" 시나리오 자체가 없다. 남는 위협은 evidence 텍스트의 마크다운/멘션 인젝션(위 "결과 표현" 절의 이스케이프로 대응)뿐이다.

## 문서화 후속 작업

구현 시 `docs/kafka-event-schema.md`에 신규 토픽 2개(`pr.build.verify.requested`, `pr.build.verify.completed`)와 이벤트 스키마를 추가해야 한다.

## 열린 리스크 / 후속 과제

- `.env.example` + 소스 스캔으로도 못 찾는 환경변수 케이스는 `inconclusive`로 처리되는데, 이게 얼마나 자주 발생하는지는 실제 운영해봐야 안다 — 자주 발생하면 3버킷 분류 로직을 더 정교하게 다듬어야 할 수 있다.
- 향후 Java/Gradle 어댑터 추가 시 VM 사이징을 재검토해야 한다(Gradle 데몬+JVM은 메모리 요구량이 훨씬 큼) — 지금은 다루지 않는다.
- Phase 2(에이전트 루프)로 넘어갈 시점과 조건 — "고정 프로브로 못 잡는 케이스가 실제로 몇 건 쌓이면"이라는 기준을 운영하면서 구체화해야 한다.
