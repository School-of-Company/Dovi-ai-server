# 샌드박스 프로브 Phase 1 구현 계획 (ai-server)

## Context

#97. 스펙 v3(`docs/superpowers/specs/2026-09-23-build-verify-agent-design.md`, 브랜치 `docs/build-verify-agent-design`, 미머지)를 구현한다. 목표는 PR 코드를 전용 VM에서 clone → install → build → 기동해서, 정적 리뷰가 놓치는 런타임 버그를 **LLM 없이 결정론적 프로브**로 잡는 것이다. 대상 버그는 두 가지다.
- 순환 import로 인한 초기화 순서 에러 (Expo-Form-Server #5)
- SIGTERM 시 shutdown hook 미실행 (Expo-Form-Server #12)

메인 리뷰 경로에는 영향이 없어야 한다.

확정한 결정:
- **토큰**: github-app에 내부 API를 추가해서 받는다. App private key는 github-app에만 둔다.
- **범위**: 이 레포만 상세 계획으로 세운다. github-app 쪽은 계약만 정의하고 별도 이슈로 뺀다.
- **fixture 재생 테스트**: 기본 CI에서는 제외하고, 별도 워크플로에서 수동 실행하거나 관련 경로가 바뀐 PR에서만 돌린다.

## 0단계: 스펙 보강 + 스펙/계획 PR (코드 없음)

`docs/build-verify-agent-design` 브랜치에서 스펙의 빈 곳을 채우고, 이 계획을 `docs/superpowers/plans/2026-09-30-sandbox-probe-plan.md`로 추가해 PR 하나로 올린 뒤 머지한다.

채울 빈 곳:
1. **토큰 계약**
   - 호출: `POST {GITHUB_APP_INTERNAL_URL}/internal/sandbox-probe/token`
   - 요청: `{installationId, repositoryId}`
   - 인증: 헤더 `X-Dovi-Internal-Secret`
   - 응답: `{token, expiresAt}`
   - 토큰 스코프는 `contents:read`에 `repositories=[repositoryId]`로 좁힌다.
2. **이벤트 필드**: `pr.sandbox.probe.requested`에 `installationId: int`를 추가한다. 스펙의 "installation 조회에 필요한 최소 정보"를 이 필드로 확정하는 것이다.
3. **동시성**: 한 프로세스 안에서 같은 그룹의 `AIOKafkaConsumer`를 `sandbox_probe_concurrency`(기본 4)개 띄운다. 파티션이 4개 이상이면 컨슈머마다 순차 처리 규약을 그대로 유지할 수 있다.
4. **install 단계 egress**: 잡 네트워크를 `--internal`로 만들고, 레지스트리 allowlist를 건 forward proxy 컨테이너(tinyproxy)만 외부와 연결한다. install 컨테이너에는 `npm_config_proxy`와 `https_proxy`만 준다.
5. **fixture 테스트 위치**: 3번 결정대로 별도 워크플로로 적는다.

## 1단계: ai-server 구현

작업 위치: 워크트리 `.claude/worktrees/feat-sandbox-probe`, 브랜치 `feat/sandbox-probe`, `origin/main`에서 분기.

새 모듈은 `app/sandbox_probe/` 아래에 둔다. 판정과 파싱 로직은 순수 함수로 만들고, Docker·git·HTTP는 Protocol 뒤에 숨긴다. 테스트에서는 fake로 바꿔 끼운다.

### Task 1. 컨슈머별 플래그 (선행, 단독 PR 가능)
- `app/core/config.py`에 다음을 추가한다.
  - `review_consumer_enabled=True`, `comment_answer_consumer_enabled=True`, `sandbox_probe_consumer_enabled=False`
  - `sandbox_probe_*` 설정: 토픽 2개, 동시성, 잡 timeout 900초, `max_poll_interval_ms` 1,200,000, github-app 내부 URL과 secret, 작업 디렉터리, 디스크 하한 GB, 프로브 이미지
- `app/main.py` lifespan을 플래그별로 컨슈머를 따로 띄우게 바꾼다.
  - review가 꺼져 있으면 LLM client, pipeline, RAG를 만들지 않는다.
  - 샌드박스 VM에는 GPU가 없으므로 llama-server나 qdrant에 연결을 시도하지 않게 하는 게 핵심이다.
- `.env.example`을 갱신한다.
- 테스트(`tests/test_main.py`, `tests/test_config.py`)
  - 기본값에서는 기존 동작이 그대로다.
  - review/comment가 false이고 sandbox가 true면 review와 comment 컨슈머, LLM client가 생성되지 않는다.

### Task 2. 이벤트 스키마 — `app/sandbox_probe/schema.py`
- 기존 `CamelModel`(`app/review/schema.py`)을 재사용한다.
- 모델: `SandboxProbeRequestedEvent`, `SandboxProbeCompletedEvent`, `Finding`, `ProbeStatus`, `ProbeName`
- 크기 제한 헬퍼 `cap_event()`:
  - evidence는 8KB, finding별 evidence는 4KB까지 두고 **뒤쪽을 보존**한다.
  - finding은 최대 10개다.
  - 직렬화 크기가 512KB를 넘으면 evidence부터 줄인다.
- 테스트: camelCase 왕복, 각 크기 제한 경계, `line=None` 허용.

### Task 3. 판정 로직 — `app/sandbox_probe/verdict.py` (순수 함수)
- `ProbeOutcome`(probe, status passed/found_issue/skip/inconclusive, findings, evidence)
- `aggregate(install_ok, build_result, init_order, lifecycle)`에 스펙의 전이 규칙을 그대로 옮긴다.
  - install 실패는 inconclusive다.
  - build 실패는 `found_issue(probe=build)`이고, 이후 프로브는 건너뛴다.
  - 하나라도 found_issue면 전체가 found_issue다.
  - lifecycle의 기동 실패는 inconclusive다.
  - positive control이 없으면 skip이다.
- `render_summary(event)`: 고정 템플릿으로 요약을 만든다. 상태 이모지는 ✅/🐛/⚠️를 쓴다.
- 테스트: 전이 표의 모든 행.

### Task 4. 레포 분석 — `app/sandbox_probe/repo_inspect.py` (순수 함수)
- `classify_env(env_example_text, source_texts)`
  - 스펙의 정규식 두 개로 변수명을 모은다.
  - 4개 버킷으로 나눈다: DB/Redis→사이드카, URL/웹훅→mock, 일반 설정→기본값, 그 외→32바이트 hex.
  - `rewrite_db_url(url, host, port)`로 연결 문자열을 사이드카 주소로 바꾼다.
- `detect_toolchain(package_json, lockfiles, ci_workflows)`
  - lockfile로 패키지 매니저를 정하고, `engines`/`packageManager`로 버전을 정한다. 둘 다 없으면 `.github/workflows/*.yml`의 `node-version`을 쓴다.
  - 그래도 못 정하면 `None`을 반환하고, 잡은 inconclusive로 끝난다.
- `extract_compose_services(compose_yaml)`: `image`, `environment`, `command`, `healthcheck`만 남긴다. `yaml.safe_load`를 쓴다. pyyaml이 없으면 의존성에 추가한다.
- 테스트는 Expo-Form-Server 실제 파일을 축약한 fixture 문자열로 한다.
  - PORT에는 기본값이 들어간다.
  - DATABASE_URL이 재작성된다.
  - `ports`, `volumes`, `privileged`는 드롭된다.

### Task 5. Docker·git·토큰 어댑터 — `app/sandbox_probe/docker.py`, `checkout.py`, `token_client.py`
- `DockerRunner` Protocol과 `SubprocessDockerRunner` 구현
  - 모든 명령은 `asyncio.create_subprocess_exec`에 argv 배열로 넘긴다.
  - 생성하는 리소스 전부에 라벨 `dovi.sandbox.job=<id>`를 붙인다.
  - 컨테이너 공통 옵션: `--network`(internal), `--user`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--pids-limit`, `--memory`, `--cpus`, `--init`, 가능한 곳은 `--read-only`
  - 제공하는 동작: `network_create(internal)`, `run`, `exec`, `kill(signal)`, `logs`, `reap(label)`
- `GithubAppTokenClient`: httpx로 0단계의 계약을 호출한다. 토큰은 로그에 절대 남기지 않는다.
- `checkout(repo_full_name, head_sha, token, dest)`
  - `git -c http.extraHeader="Authorization: Basic …" fetch --depth 1 origin <sha>`로 가져온다. 토큰이 `.git/config`에 남지 않는다.
  - checkout 뒤 remote URL에 자격 증명이 없는지 확인한다.
- 테스트
  - 조립된 argv 스냅샷에 보안 옵션이 모두 들어 있다.
  - 토큰이 argv나 로그, `.git/config`에 남지 않는다. checkout은 로컬 bare repo로 검증한다.
  - token client는 `httpx.MockTransport`로 테스트한다.

### Task 6. 프로브와 잡 러너 — `app/sandbox_probe/probes.py`, `runner.py`, `mock_server.py`
- `mock_server.py`: 표준 라이브러리만 쓰는 캐치올 HTTP 서버다. 받은 요청을 `GET /_received`로 돌려준다. `python:3.13-slim` 컨테이너에 읽기 전용으로 마운트한다.
- `init_order_probe`
  - `dist/**/*.js` 각각을 `node --input-type=module -e "await import(...)"`로 개별 import한다.
  - 한 프로브 컨테이너 안에서 xargs 대신 Python이 exec를 병렬 호출한다(상한 4).
  - 실패한 파일마다 `Finding(probe=init_order, filePath=src 경로 추정)`을 만든다.
- `lifecycle_probe`
  1. 앱을 기동하고 판정 사다리로 기동 여부를 본다(`/health` → TCP → positive control).
  2. mock에 시작 알림이 왔는지 본다. 안 왔으면 skip이다.
  3. SIGTERM을 보내고 5초 기다린다.
  4. 종료 알림이 오지 않으면 found_issue다.
- `SandboxJobRunner.run(event) -> SandboxProbeCompletedEvent`
  - 순서: 디스크 여유 확인 → 토큰 발급 → checkout → 레포 분석 → 네트워크·proxy·사이드카·mock 기동 → install 컨테이너 → node_modules 볼륨 → 프로브 컨테이너 build → 프로브 2개 → `aggregate`
  - 잡 전체를 `asyncio.timeout(900)`으로 감싼다.
  - `finally`에서 라벨로 리소스를 정리한다.
- 테스트: fake `DockerRunner`에 시나리오를 주입해 다음을 확인한다.
  - build 실패
  - init_order 실패
  - positive control 없음
  - 기동 실패
  - timeout이 나면 inconclusive로 끝나고 정리가 호출된다.

### Task 7. 컨슈머 — `app/sandbox_probe/consumer.py` + 배선
- 구조는 `CommentAnswerConsumer`(`app/comment_answer/consumer.py`)와 같게 한다. 수동 커밋, graceful shutdown, `CancelledError`에서 락 해제를 따른다.
- dedup은 `RedisDedupStore`(`app/review/dedup.py`)에 prefix `ai-review:sandbox-probe-dedup:`, TTL 1800을 주어 재사용한다.
- 포이즌 잡 방지: 프로브 시작 직전에 `INCR ai-review:sandbox-probe-attempts:<id>`를 올린다. 2회를 넘으면 inconclusive를 발행하고 커밋한다.
- `app/kafka/client.py`에 `create_sandbox_probe_consumer`를 추가한다. 그룹은 `dovi-ai-sandbox-probe-engine`, `max_poll_interval_ms`는 설정값을 쓴다.
- `app/kafka/producer.py`에 `SandboxProbeEventProducer`를 추가한다. key는 reviewJobId다.
- `main.py`: 플래그가 켜지면 기동 시 먼저 `reap`하고, 컨슈머를 N개 띄운다.
- 테스트는 `tests/test_comment_answer_consumer.py` 패턴을 따른다: 중복 skip, 시도 횟수 초과, 발행, 커밋.

### Task 8. 배포와 fixture 재생
- **VM 배포**: 스펙대로 워커는 호스트 프로세스로 돈다.
  - `deploy/sandbox/dovi-sandbox-probe.service`(systemd, `uv run uvicorn app.main:app`)를 둔다.
  - VM용 env 예시 `deploy/sandbox/.env.sandbox.example`을 둔다. review와 comment 플래그는 false다.
  - `.github/workflows/cd-sandbox.yml`: 별도 SSH 시크릿으로 git pull → `uv sync` → `systemctl restart`한다.
  - VM은 지금 비밀번호 인증뿐이라 CD용 키 등록이 필요하다.
- **fixture 재생**
  - 테스트: `tests/test_sandbox_probe_fixtures.py`, `@pytest.mark.fixture_replay`
  - `pyproject`의 기본 pytest 설정에서 `-m "not fixture_replay"`로 제외한다.
  - 대상은 4개 SHA(`07045bcb0d9a`, `b13836e9136d`, `8a4f5f9a2dc7`, `482db26e3993`)다. 각각 `SandboxJobRunner`를 실제 Docker로 돌려 기대 status가 나오는지 확인한다.
  - 토큰은 env `FIXTURE_GITHUB_TOKEN`을 쓰는 fake token client로 받는다.
  - `.github/workflows/sandbox-fixtures.yml`: `workflow_dispatch`와 `paths: app/sandbox_probe/**`에서 돈다.

## github-app 쪽 계약 (별도 이슈, 이 계획에서 구현하지 않음)

- **installation-token**: 스코프 지정 토큰을 발급하고, 캐시 키에 scopeHash를 넣는다. 0단계의 내부 API를 추가한다.
- **webhook DTO**: `head.repo.{id,full_name}`을 추가하고 fork를 판별한다. 발행 조건은 다음을 모두 만족할 때다.
  - `@nestjs/core`가 쓰인 레포
  - 같은 레포 브랜치에서 연 PR
  - `DOVI.md` opt-in
  - 발행 킬스위치가 켜져 있음
- **발행**: 메인 리뷰를 발행한 뒤 독립 경로로 `pr.sandbox.probe.requested`를 발행한다. 토픽은 파티션 4개 이상으로 명시적으로 생성한다.
- **결과 컨슈머**: `pr.sandbox.probe.completed`를 받아 sticky 코멘트를 upsert한다. `comment-answer-result`를 선례로 삼는다. 코드펜스 길이를 조정하고 `@` 멘션을 무력화한다.
- **권한**: `issues: write`를 확인한다.
- **문서**: `docs/kafka-event-schema.md`를 갱신한다.
- 이 이슈는 ai-server 이슈(#97)와 서로 링크해서 생성한다(assignee @me).

배포 순서: ai-server 컨슈머 배포 → github-app 토큰 API → github-app 발행(킬스위치 off) → opt-in 레포 하나로 켜기.

## 열린 인프라 확인 (구현 전 VM에서 확인)
- VM에서 prod의 Kafka, Redis, github-app 내부 API에 닿는 경로. 지금은 prod가 127.0.0.1 바인딩에 SSH 터널을 쓰므로, 터널로 갈지 방화벽으로 허용할지 정해야 한다.
- VM의 Docker 버전, `--internal` 네트워크 동작, 비-root 사용자.

## 검증
- 매 Task마다 `uv run pytest`, `uv run ruff check .`, `uv run mypy .`를 돌린다.
- Task 1 배포 뒤 prod에서 기존 컨슈머가 정상인지 로그로 확인한다(기본값이라 동작 변화가 없어야 한다).
- merge 조건: VM에서 `uv run pytest -m fixture_replay`를 돌려 4개 fixture가 기대값(found_issue/passed/found_issue/passed)과 정확히 일치해야 한다. 별도 워크플로에서도 녹색이어야 한다.
- E2E: github-app 작업이 끝나면 opt-in 테스트 레포에 버그 커밋 PR을 열고, 🐛 sticky 코멘트가 달리는지와 메인 리뷰 지연이 없는지 확인한다.

## PR 분할
1. 0단계: 스펙 보강과 계획 문서
2. Task 1: 컨슈머 플래그. 독립 가치가 있어 먼저 머지한다.
3. Task 2~4: 스키마, 판정, 레포 분석(순수 로직)
4. Task 5~7: 어댑터, 러너, 컨슈머
5. Task 8: 배포와 fixture

모든 PR은 워크트리에서 작업하고, CI를 통과하면 squash-merge한다.
