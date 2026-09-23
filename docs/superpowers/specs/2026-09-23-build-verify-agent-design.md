# 빌드/실행 검증 에이전트 (Build-Verify Agent) 설계

## 배경

최근 5일 내 실제 PR 두 건에서, 사람 리뷰어(cfcromn)가 로컬에서 직접 빌드/서버 기동을 해봐서 Dovi가 놓친 런타임 버그를 잡아냈다.

1. **`School-of-Company/Expo-Form-Server` PR #5** — `src/form/entities/dynamic-form.entity.ts:42`의 `form: FormEntity` 필드가 ESM 순환참조 + `emitDecoratorMetadata` 상호작용으로 `Cannot access 'FormEntity' before initialization`을 일으킴. 일반적인 `pnpm build && pnpm start`로는 재현되지 않고, `FormEntity`를 먼저 import하는 특정 순서에서만 재현됨. cfcromn이 직접 빌드 후 import 순서를 바꿔가며 재현했다.
2. **`School-of-Company/Expo-Form-Server` PR #12** — `app.enableShutdownHooks()` 누락으로 `SIGTERM` 시 `OnApplicationShutdown` 훅이 실행되지 않아, Discord 웹훅 종료 알림이 발송되지 않음. cfcromn이 로컬에서 실제로 서버를 띄우고 SIGTERM을 보내 웹훅 도착 여부를 확인해서 발견했다.

두 케이스 모두 Dovi의 현재 파이프라인(diff + AST 컨텍스트 + RAG를 프롬프트에 넣고 LLM 한 번 호출)으로는 구조적으로 잡을 수 없다. 정적으로 코드를 읽는 것만으로는 불가능하고(#1은 타입 체크도 통과하는 정상 코드), 고정된 빌드/테스트 스크립트를 도는 것으로도 잡히지 않는다(#1은 일반 빌드로 재현 안 됨, #2는 일반 테스트 스위트에 없는 시나리오). 둘 다 **diff를 보고 "이 부분이 의심스러우니 이런 실험을 해보자"는 가설 수립 + 실제 실행이 필요**했다.

## 목표

Dovi 리뷰 파이프라인에, diff를 바탕으로 가설을 세우고 실제로 빌드/실행해서 검증하는 에이전트 트랙을 추가한다. 메인 리뷰(현재 SLA)를 막지 않는 별도 비동기 트랙으로 동작한다.

## 비목표 (v1 범위 제외)

- Java/Gradle, Python 등 NestJS 외 스택 지원 — 인터페이스는 확장 가능하게 설계하되, v1 구현체는 NestJS 하나만.
- 실제 서드파티 API(결제, 실제 외부 LLM 등)처럼 로컬로 흉내낼 수 없는 의존성 검증.
- 메인 리뷰의 응답 시간(SLA)에 영향을 주는 어떤 변경도 포함하지 않음 — 이 트랙은 항상 메인 리뷰와 독립적으로, 늦게 도착해도 무방한 트랙이다.

## 아키텍처 개요

```
GitHub PR 이벤트
  → Dovi-github-app: pr-data-collector
      - 기존: 파일 content 수집 → ReviewRequestedEvent (`pr.review.requested` 토픽, `settings.kafka_review_request_topic`)
      - 신규: 스택 감지(package.json에 @nestjs/core 존재) 성공 시,
        installation-token 모듈로 contents:read 스코프의 단기 토큰을 발급받아
        ReviewRequestedEvent에 stack/clone_token/clone_url 필드로 실어 보냄
  → Dovi-ai-server: 기존 review 컨슈머 그룹 (dovi-ai-review-engine) — 변경 없음
  → Dovi-ai-server: 신규 dovi-build-verify 컨슈머 그룹
      - 같은 `pr.review.requested` 토픽을 구독하되 stack이 채워진 이벤트만 처리
      - 전용 VM에서 실행 (샌드박스 격리를 프로덕션 GPU 박스와 분리)
      1. clone (임시 토큰 사용, 메모리에서만 보관, 로그에 절대 남기지 않음)
      2. 스택 어댑터가 샌드박스 구성 결정 (env 변수 3버킷 분류, DB/Redis 사이드카 여부 등)
      3. 격리된 잡 전용 Docker 네트워크 기동
      4. 에이전트 루프 실행 (최대 5~6 RUN 스텝)
      5. 종료 시 컨테이너·네트워크·볼륨 전부 폐기, 상태 확정
  → Kafka로 BuildVerifyCompletedEvent 발행 — 새 토픽 `pr.build_verify.completed`
     (이 프로젝트는 이벤트 타입마다 토픽을 분리하는 컨벤션이라 — `pr.review.completed`,
     `pr.comment.answer.completed`, `pr.comment.reflected`처럼 — 기존 결과 토픽에
     타입을 섞지 않고 새 토픽을 만든다)
  → Dovi-github-app: review-result-consumer 확장
      - 기존 ReviewCompletedEvent 처리 로직 유지
      - 신규: 새 토픽을 추가 구독해서 BuildVerifyCompletedEvent 수신 시
        별도 GitHub 코멘트로 게시(이모지 헤더 + evidence, 항상 게시 — 통과/문제발견/판단불가 상관없이)
```

## 인프라

전용 VM 1대 (프로덕션 GPU 박스와 분리):

- **스펙**: 4 vCPU / 15Gi RAM / 58G 디스크, GPU 없음
- **동시 처리**: 최대 4개 잡 (새 컨슈머 그룹을 파티션 4개 기준 워커 4개로 구성)
- **LLM 추론**: 이 VM에서 직접 하지 않음 — 기존 프로덕션 GPU 박스의 llama-server(Qwen2.5-Coder-32B)를 네트워크로 호출. 방화벽에서 이 VM의 IP만 llama-server 포트에 접근 허용하도록 제한한다(현재 무방비로 열려 있는 상태를 이 작업을 계기로 좁힘).
- **분리 이유**: 이 트랙은 PR에 포함된, 신뢰할 수 없는 코드를 실제로 빌드/실행한다. 프로덕션 GPU 박스(api/qdrant/llama-server/langfuse 스택이 모두 공존)에 이 워크로드를 얹으면, 악의적인 PR이 자원을 고갈시켜 실제 서비스에 영향을 줄 수 있다(이전에 겪은 OOM 장애와 같은 부류). 물리적으로 분리하면 cgroup 설정 실수가 있어도 블라스트 반경이 이 VM 안으로 국한된다.
- **접속 정보**: 별도로 로컬 메모리에만 보관, 이 스펙 문서나 git에는 포함하지 않는다.

## 샌드박스 설계

### 격리 단위

리뷰 잡 하나당 전용 Docker 네트워크(`review-verify-<job-id>`)를 즉석 생성한다. 이 네트워크는 외부 인터넷으로 나가는 라우트가 없다(브릿지 게이트웨이 라우팅 차단) — 이 차단은 **잡 안의 모든 컨테이너(앱 컨테이너뿐 아니라 DB/Redis 사이드카까지)에 예외 없이** 적용한다. 잡 종료(성공/실패/타임아웃/스텝캡 소진 상관없이) 시 컨테이너·네트워크·볼륨을 `finally`에서 강제 삭제한다 — 이건 협상 대상이 아닌 필수 안전장치다.

같은 네트워크 **안**에서의 컨테이너 간 통신(예: 에이전트가 `curl`로 mock 서버나 방금 띄운 앱 자신을 찌르는 것)은 막지 않는다. "네트워크 차단"은 샌드박스 경계 밖으로 못 나가는 것이지, 안에서 서로 통신하는 걸 막는 게 아니다.

### 환경변수 3버킷 분류

대상 레포의 `.env.example`을 읽어 필요한 환경변수를 분류한다.

1. **DB/Redis 패턴** (`DATABASE_URL`, `POSTGRES_*`, `REDIS_*` 등 잘 알려진 이름) → 같은 네트워크에 즉석으로 띄운 실제 Postgres/Redis 컨테이너를 가리키게 함. 진짜로 붙어서 마이그레이션·부팅이 되어야 하는 경우가 많아 mock으로 대체할 수 없다.
2. **URL/웹훅 패턴** (`*_URL`, `*_WEBHOOK*` 등, 1번에 해당 안 하는 것) → 범용 mock 캐치올 서버 하나(같은 네트워크 안)를 가리키게 함. 이 mock 서버는:
   - 들어오는 모든 요청을 기록하고 200을 반환
   - `GET /_received`로 지금까지 받은 요청 목록을 조회할 수 있는 인스펙션 엔드포인트 제공 — 에이전트가 `curl http://mock:9000/_received`로 "실제로 요청이 왔는지" 확인하는 데 필수 (PR #12 케이스를 잡으려면 이게 있어야 함)
3. **그 외 필수 환경변수** (JWT_SECRET, 암호화 키 등 불투명한 문자열) → 랜덤 더미 값(적당한 길이 hex/base64) 생성해서 주입. 대부분 "값이 존재하고 형식이 맞는지"만 검증하므로 충분하다.

세 버킷 다 안 맞는 예외 케이스(예: 특정 실제 값으로 뭔가를 복호화해야 하는 앱)는 정적으로 미리 다 풀려고 하지 않는다 — 에이전트 루프 자체가 "부팅 실패, 에러 메시지에 특정 변수 언급 → 다른 더미 값으로 재시도"를 스스로 처리하도록 둔다. 이게 애초에 고정 스크립트 대신 에이전트로 설계한 이유와 일치한다.

진짜 프로덕션 시크릿은 어떤 경우에도 이 샌드박스에 주입하지 않는다.

### 리소스 캡

동시 4잡을 기준으로 전체 예산(4 vCPU / 15Gi RAM)을 잡당 나눠서 컨테이너별 `--memory`/`--cpus` 하드캡을 건다. DB/Redis 사이드카를 포함해 **잡 안의 모든 컨테이너**에 캡을 적용한다(진짜 소프트웨어를 띄우는 것 자체는 위협이 아니지만, 무제한 자원 사용은 위협이다). 볼륨도 크기 제한이 있는 tmpfs/loop 디바이스로 떠서 디스크 고갈을 방지한다.

## 에이전트 루프

### 도구

`run_command(cmd: str, timeout_sec: int = 30)` 단 하나. 샌드박스 안에서 셸 명령 하나를 실행하고 exit code + stdout/stderr(각 최대 2000자로 truncate, 기존 `app/review/pipeline.py`의 diff 예산 자르는 방식과 동일한 원칙)를 반환한다. 화이트리스트 없이 임의 셸 명령을 허용한다 — `curl`, `psql`, `kill -TERM` 등 다 포함.

### 상호작용 프로토콜

로컬에서 서빙하는 Qwen2.5-Coder-32B(llama-server)의 구조화된 tool-calling 신뢰성이 불확실하므로, OpenAI 스타일 function-calling 대신 **고정 접두사 파싱**을 쓴다.

- `RUN: <명령어>` → 해당 명령 실행, 결과를 다음 턴 메시지로 추가해서 반복
- `CONCLUDE: passed|found_issue|inconclusive` 뒤에 **반드시** `EVIDENCE:` 섹션(직전까지 실행한 명령+exit code+출력 인용)이 와야 유효한 결론으로 인정한다. `EVIDENCE:` 없이 결론을 내리면 파싱 단계에서 거부하고 "증거 없이는 결론을 낼 수 없다"는 안내를 돌려보낸다 — 이슈 #95(주석만 바뀐 파일이 "크기 제한"이라고 잘못 보고된 사고)와 같은, 확인 안 된 것을 확인됐다고 하는 함정을 파서가 기계적으로 막는다.
- 위 두 형식에 안 맞는 출력은 그 턴을 실패로 간주하고 스텝 카운트를 소모시킨다.

### 종료 조건

- `RUN` 최대 5~6회(스텝 캡). `CONCLUDE` 자체는 카운트하지 않는다(결론 낼 기회는 항상 남아있어야 함). 캡에 도달하면 모델에게 더 기회를 주지 않고 시스템이 즉시 `inconclusive`로 강제 마감한다.
- 개별 명령 30초 / 전체 잡 5분 타임아웃. 먼저 걸리는 쪽으로 강제 종료 후 `inconclusive` 처리.
- 두 경우 다 그때까지의 실행 이력(명령+결과)을 evidence로 남겨서 코멘트에 포함시킨다 — "왜 판단 불가였는지"가 항상 사람이 확인 가능해야 한다.

### 초기 프롬프트 구성

- diff hunks, 변경 파일 목록, 감지된 스택
- 샌드박스 환경 안내: 어떤 경로에 clone돼 있는지, 어떤 환경변수가 실제 로컬 인스턴스를 가리키고 어떤 게 mock/더미인지
- 지시: "정적 분석으로 안 걸리는 런타임 동작(초기화 순서, 생명주기 훅, 시그널 처리, 외부 콜백 발생 여부)에 집중해서 가설을 세우고 검증하라. 타입 에러나 스타일 같은 정적 이슈는 메인 리뷰가 이미 처리하니 무시해라."
- `RUN:`/`CONCLUDE:` 프로토콜 규칙

### 모델

새 모델/새 GPU 없이 기존 프로덕션 llama-server(Qwen2.5-Coder-32B-Instruct-Q4_K_M)를 그대로 재사용한다. 스텝마다 순차 호출.

## 트리거 조건

지원 스택(v1: NestJS) 소스 코드를 건드리는 PR이면 항상 실행한다(lockfile-only, 문서 전용 PR은 스킵). "위험 패턴이 감지될 때만 실행" 같은 선별 트리거는 도입하지 않는다 — 그 감지 로직 자체가 또 하나의 정적 판단이라, 예상 못 한 패턴이면 트리거조차 안 걸려서 이번에 해결하려는 문제를 트리거 단계에서 그대로 반복하게 된다.

## 결과 표현

`BuildVerifyCompletedEvent` (새 토픽 `pr.build_verify.completed`):

```
status: "passed" | "found_issue" | "inconclusive"
evidence: str  # 실행 명령 + exit code + 출력 인용
review_comment: ReviewComment | null  # found_issue일 때, 기존 스키마 재사용해 인라인 코멘트로
```

- `review-result-consumer`가 이 이벤트를 받으면 **항상**(성공/문제발견/판단불가 상관없이) 새 GitHub 코멘트를 게시한다 — 침묵하지 않는다. 이 기능이 실제로 동작 중이라는 걸 사람이 확인할 수 있어야 신뢰가 생긴다(이 조직의 기존 커버리지 리포트 코멘트 패턴과 동일).
- 메인 리뷰 코멘트를 나중에 수정(edit)하지 않고 **별도 코멘트**로 낸다. 상태별 이모지(✅ 통과 / 🐛 문제 발견 / ⚠️ 판단 불가)를 텍스트 맨 앞에 붙인다.

## 보안 고려사항

- installation token은 `contents:read`만 스코프, 기존 정책대로 단기 만료. Kafka 메시지 payload 안에만 존재하고 로그에는 절대 남기지 않는다(`security.md` 기존 규칙 그대로 적용).
- 샌드박스는 외부 네트워크 완전 차단(모든 컨테이너 예외 없이), 진짜 시크릿 미주입, 잡 종료 시 전체 폐기.
- 프로덕션 GPU 박스와 물리적으로 분리된 전용 VM에서 실행 — 자원 고갈이 프로덕션에 전이되지 않음.
- 리소스(CPU/메모리/디스크) 하드캡을 잡 안의 모든 컨테이너(앱뿐 아니라 DB/Redis 사이드카까지)에 예외 없이 적용.

## 열린 리스크 / 후속 과제

- 로컬 32B 모델이 `RUN:`/`CONCLUDE:` 프로토콜을 얼마나 안정적으로 따르는지는 실제 구현 후 검증이 필요하다 — 포맷 이탈률이 높으면 스텝 캡을 프로토콜 위반 재시도에 다 소모해버릴 위험이 있다.
- `.env.example`이 실제 필수 변수를 다 반영하지 못하는 레포가 있을 수 있다(문서 누락) — 3버킷 분류가 놓친 변수는 결국 에이전트가 부팅 실패 메시지로 발견해서 대응해야 한다.
- 향후 Java/Gradle 어댑터 추가 시 VM 사이징을 재검토해야 한다(Gradle 데몬+JVM은 메모리 요구량이 훨씬 큼) — 지금은 다루지 않는다.
