# 6단계 — 평가 루프 (Design Spec)

## 배경

로드맵 21절/27절 6단계("평가 루프"). 지금까지 리뷰 파이프라인은 결과를
Kafka로 발행하고 나면 그걸로 끝이다 — 어떤 리뷰가 실제로 유용했는지,
모델/프롬프트 버전을 바꿨을 때 품질이 좋아졌는지 측정할 방법이 없다.
이 스코프는 review_records를 영속 저장하고, Claude Code가 PR에서
리뷰를 반영/미반영했다는 신호를 모아 acceptance rate 같은 지표로
집계하는 평가 인프라를 구축한다.

## 왜 관계형 DB가 필요한가

현재 저장소는 Redis(캐시/dedup, TTL 기반 휘발성)와 Qdrant(벡터)뿐이다.
평가 데이터는 정반대 성격이다 — **영구 보관**해야 하고, "severity별
반영률", "모델 버전별 비교" 같은 **집계 쿼리**가 핵심 요구사항이다.
Redis에 JSON으로 넣으면 매번 전체 키를 스캔해야 하고, 영구 보관 용도로
설계된 저장소도 아니다. PostgreSQL을 새로 도입한다 — 다른 프로젝트
(Expo/Flooding)와 인프라 성격이 같아 운영 부담도 익숙한 범위다.

## 이벤트 계약 신설 — 크로스팀 의존성 (중요)

노션 원안(21절)은 "PR에 Claude Code가 반영/미반영 코멘트를 달면 →
매칭"이라고만 되어 있지만, **AI Review Engine은 GitHub API를 직접
호출하지 않는다**는 원칙(6.5절, A안)이 이미 확정되어 있고 GitHub App은
다른 팀이 담당한다. 따라서 이 신호는 GitHub App 팀이 새 Kafka 이벤트로
발행해줘야 한다 — 이번 스코프에 GitHub App 레포 변경은 포함되지 않는다.

**GitHub App 팀에 전달할 이벤트 계약:**

```
Topic: pr.comment.reflected
Kafka message key: reviewJobId (필수) — 같은 reviewJobId의 여러 findingIndex 피드백이
같은 파티션으로 가야, 단일 인스턴스가 순차 처리한다는 가정(review_feedback의
upsert가 dedup 없는 get-then-write인 이유)이 실제로 성립한다. key 없이 발행하면
파티션이 여러 개이거나 인스턴스가 2대 이상일 때 같은 (reviewJobId, findingIndex)의
동시 upsert가 유니크 제약 위반으로 조용히 유실될 수 있다.

ReviewFeedbackEvent:
  reviewJobId    # repositoryId:prNumber:headSha, 원본 리뷰와 매칭
  findingIndex   # ReviewCompletedEvent.reviews 배열의 인덱스 (0-based)
  reflected      # true | false
  reason         # optional, 반영/미반영 이유 요약
```

`findingIndex`로 매칭하는 이유: `ReviewComment`에는 안정적인 ID가 없다.
`reviews` 배열은 완료 시점 그대로 저장되므로 인덱스가 안정적인 키가 된다.

**이번 스코프가 실제로 하는 일**: 이 이벤트를 소비할 준비(스키마 정의 +
consumer + 저장 로직)를 AI Engine 쪽에 전부 만들어둔다. 실제 프로덕션
트래픽은 GitHub App 팀이 발행 측을 구현해야 흐르기 시작하므로, 테스트는
fixture 이벤트로 consumer 로직만 검증한다(기존 comment-answer consumer
테스트와 동일 패턴).

## DB 스키마 (PostgreSQL, 3테이블)

```sql
review_jobs
  review_job_id   TEXT PRIMARY KEY
  repository_id   BIGINT NOT NULL
  pr_number       INT NOT NULL
  head_sha        TEXT NOT NULL
  status          TEXT NOT NULL      -- completed | failed
  fail_reason     TEXT NULL          -- parse_error | timeout | server_error
  created_at      TIMESTAMPTZ NOT NULL

review_records
  review_job_id   TEXT PRIMARY KEY REFERENCES review_jobs(review_job_id)
  reviews         JSONB NOT NULL     -- ReviewComment[] 그대로 (model_dump)
  summary         TEXT NOT NULL
  model_version   TEXT NOT NULL
  prompt_version  TEXT NOT NULL

review_feedback
  id              SERIAL PRIMARY KEY
  review_job_id   TEXT NOT NULL REFERENCES review_jobs(review_job_id)
  finding_index   INT NOT NULL
  reflected       BOOLEAN NOT NULL
  reason          TEXT NULL
  updated_at      TIMESTAMPTZ NOT NULL
  UNIQUE (review_job_id, finding_index)
```

**Ruling — `status`에 `pending` 없음**: 노션 원안은 `status`를
pending/completed/failed 3가지로 뒀지만, 진행 중 상태는 이미
`DedupStore`(Redis)가 별도로 추적하고 있어 여기서 중복 관리할 이유가
없다. `review_jobs`/`review_records`는 파이프라인이 **완료되거나
실패한 시점에 한 번만** 쓴다 — "리뷰가 오래 안 끝난다"를 보는 건
dedup/모니터링의 책임이지 평가 인프라의 책임이 아니다. 이 판단이
틀렸다고 밝혀지면(예: 평가 스크립트가 처리 중인 작업 수를 알아야
한다는 요구가 생기면) `status='pending'` 삽입을 나중에 추가해도
스키마 변경 없이 가능하다.

**Ruling — `review_records.reviews`는 실패 이벤트엔 없음**: `failed`
상태는 `review_jobs`에만 기록하고 `review_records`는 만들지 않는다
(실패 시 `reviews`가 없으므로 자연스러운 결과).

`reviews` 컬럼은 SQLAlchemy `JSON` 타입에 `.with_variant(JSONB,
"postgresql")`를 적용한다 — 프로덕션(Postgres)에서는 JSONB로,
테스트(SQLite in-memory)에서는 일반 TEXT-backed JSON으로 동작해
테스트에 실제 Postgres가 필요 없다.

## 아키텍처 / 파일 구성

새 최상위 패키지 `app/evaluation/`을 만든다(리뷰 파이프라인과는 관심사가
분리된 별도 서브시스템이라 `app/review/` 밑에 두지 않는다):

```
app/evaluation/
  models.py             # SQLAlchemy 선언형 모델 3개 (ReviewJobRow/ReviewRecordRow/ReviewFeedbackRow)
  db.py                 # create_engine()/create_session_factory() — settings.database_url 기반
  schema.py             # ReviewFeedbackEvent (CamelModel, app/review/schema.py와 동일 패턴)
  repository.py         # EvaluationRepository Protocol + SqlAlchemyEvaluationRepository
  feedback_consumer.py  # ReviewFeedbackConsumer (pr.comment.reflected 소비)

alembic/
  env.py
  versions/0001_create_evaluation_tables.py
alembic.ini

scripts/
  evaluate_reviews.py
```

### `repository.py`

```python
class EvaluationRepository(Protocol):
    async def save_completed(self, event: ReviewCompletedEvent) -> None: ...
    async def save_failed(self, event: ReviewFailedEvent) -> None: ...
    async def upsert_feedback(self, feedback: ReviewFeedbackEvent) -> None: ...
```

- `save_completed`/`save_failed`: `review_jobs` + (completed면) `review_records`를
  하나의 트랜잭션으로 upsert(같은 `review_job_id`가 재전달되는 경우를
  대비해 `ON CONFLICT DO UPDATE`).
- `upsert_feedback`: `(review_job_id, finding_index)` 유니크 키 기준
  upsert. **`review_job_id`가 `review_jobs`에 없으면(FK 위반) 예외를
  삼키고 로깅만 한다** — 평가 데이터 유실이 리뷰 파이프라인이나 다른
  기능에 영향을 주면 안 된다(기존 `DependencyResolver`/`ApiSpecRetriever`와
  동일한 best-effort 계약).

### `feedback_consumer.py`

`app/kafka/consumer.py`의 `ReviewRequestConsumer`, `app/comment_answer/consumer.py`의
`CommentAnswerConsumer`와 동일한 모양의 3번째 consumer:

```python
class ReviewFeedbackConsumer:
    def __init__(self, source: MessageSource, repository: EvaluationRepository) -> None: ...
    async def run(self, shutdown: asyncio.Event | None = None) -> None: ...
    async def handle(self, raw: bytes) -> None:
        # ReviewFeedbackEvent 파싱 실패 시 로깅 후 skip (다른 consumer들과 동일)
        # repository.upsert_feedback() 호출 — dedup 불필요(upsert가 멱등)
```

발행 측이 없으므로 producer/publish 로직은 없다 — 순수 consume-only.

## 파이프라인/consumer 통합 지점

`app/kafka/consumer.py`의 `ReviewRequestConsumer`에 `evaluation_repository:
EvaluationRepository | None = None` 파라미터를 추가한다(기존
`dependency_resolver` optional-injection과 동일한 패턴). `handle()`에서
기존 발행 직후, **best-effort로 감싸서** 저장한다:

```python
result = await self._pipeline.run(event)
if isinstance(result, ReviewCompletedEvent):
    await self._producer.publish_completed(result)
    await self._dedup.mark_completed(event.review_job_id)
    await self._save_evaluation_record(result)
else:
    await self._producer.publish_failed(result)
    await self._dedup.mark_failed(event.review_job_id)
    await self._save_evaluation_record(result)

...

async def _save_evaluation_record(
    self, result: ReviewCompletedEvent | ReviewFailedEvent
) -> None:
    if self._evaluation_repository is None:
        return
    try:
        if isinstance(result, ReviewCompletedEvent):
            await self._evaluation_repository.save_completed(result)
        else:
            await self._evaluation_repository.save_failed(result)
    except Exception:
        logger.exception(
            "failed to persist evaluation record reviewJobId=%s", result.review_job_id
        )
```

DB 저장 실패가 리뷰 발행/커밋을 절대 막지 않는다 — Kafka 발행이 먼저,
저장은 그 뒤에 best-effort로.

## 설정 / 배선

`app/core/config.py`에 추가:

```python
evaluation_enabled: bool = False
database_url: str = "postgresql+asyncpg://dovi:dovi@localhost:5432/dovi"
kafka_review_feedback_topic: str = "pr.comment.reflected"
```

`app/kafka/client.py`에 `create_review_feedback_consumer(settings)` 추가
(기존 `create_comment_answer_consumer`와 동일한 모양, 새 group_id
`"dovi-ai-review-feedback-engine"`).

`app/main.py`의 `lifespan()`에 `if settings.evaluation_enabled:` 블록 추가
(기존 `rag_enabled`/`notion_sync_enabled`/`dependency_check_enabled`와
동일한 지연-import + optional 배선 패턴):

- async SQLAlchemy 엔진/세션 팩토리 생성
- `SqlAlchemyEvaluationRepository` 생성 → `ReviewRequestConsumer`에 주입
- `pr.comment.reflected`용 Kafka consumer 생성 → `ReviewFeedbackConsumer`
  생성 → 기존 `tasks` 튜플에 3번째 task로 추가(`_run_consumer_forever`
  재사용)
- shutdown 시 엔진 dispose

`docker-compose.yml`(prod)에 `postgres` 서비스 추가 — 호스트 포트를
열지 않고 컴포즈 내부 네트워크로만 노출한다(`api`가 서비스명 `postgres`로
접속, qdrant처럼 호스트에서 직접 붙을 CLI 툴이 없으므로). `api`의
`depends_on`에 `postgres` 추가. `docker-compose.override.yml`(로컬
개발)에는 개발자가 `psql`로 직접 붙어볼 수 있도록 `5432:5432` 포트를
연다.

`pyproject.toml`에 추가: `sqlalchemy[asyncio]`, `asyncpg`, `alembic`.

## evaluate_reviews.py

```
python -m scripts.evaluate_reviews [--json]

- review_jobs ⋈ review_records ⋈ review_feedback (LEFT JOIN, reflected가
  없는 finding도 "미평가"로 셀 수 있게)
- 전체 acceptance rate = count(reflected=true) / count(reflected IS NOT NULL)
- severity별 반영률 (critical/major/minor/suggestion 그룹별)
- (model_version, prompt_version) 조합별 acceptance rate 비교
- 기본은 사람이 읽을 표 형태로 콘솔 출력, --json이면 동일 데이터를 JSON으로
```

새 서드파티 표 포맷팅 라이브러리는 추가하지 않는다 — 표 형태는
`str.ljust`/f-string 정렬로 충분하다(YAGNI).

## 에러 처리 요약

| 실패 지점 | 처리 |
|---|---|
| `review_jobs`/`review_records` 저장 실패(DB 다운 등) | 로깅만, 리뷰 발행/커밋에는 영향 없음 (이미 Kafka로 나간 뒤) |
| `review_feedback` upsert 시 FK 위반(모르는 reviewJobId) | 로깅만, 메시지 skip(수동 재처리 대상 아님 — 유실 허용) |
| `pr.comment.reflected` 페이로드 파싱 실패 | 로깅 후 skip, 다른 consumer와 동일 |
| Postgres 연결 자체 실패(기동 시) | `evaluation_enabled=false`면 애초에 이 경로를 안 탄다 — 다른 optional 기능과 동일하게, 켠 상태에서 DB가 없으면 기동 실패는 허용(다른 필수 인프라와 동급으로 취급) |

## 테스트 전략

- `repository.py`: SQLite in-memory(`aiosqlite`)로 유닛 테스트 —
  save_completed/save_failed/upsert_feedback, 중복 upsert(멱등성),
  FK 위반 시 예외를 삼키는지 확인.
- `feedback_consumer.py`: mocked Kafka source + fake repository — 정상
  이벤트, 잘못된 JSON, repository 예외 발생 시에도 consumer 루프가
  죽지 않는지.
- `ReviewRequestConsumer`: fake `EvaluationRepository`로 completed/failed
  각각 저장 호출되는지, 저장이 예외를 던져도 `publish_completed`/
  `mark_completed` 이후 흐름(커밋 등)이 안 깨지는지.
- `evaluate_reviews.py`: 시드 데이터(여러 severity/버전 조합)로 집계
  결과가 손으로 계산한 값과 일치하는지.
- **Alembic 마이그레이션 자체**는 유닛 테스트 대상이 아니다 — 로컬
  `docker-compose.override.yml`의 postgres에 한 번 실제로 적용해
  스키마가 의도대로 만들어지는지 수동 확인한다(다른 마이그레이션
  도구가 이 레포에 없어 자동화 테스트 패턴이 아직 없음).

## 스코프 밖 (다음 단계 후보, 지금 안 함)

- GitHub App 레포의 `pr.comment.reflected` 발행 측 구현 — 다른 팀 작업,
  이 계약을 전달하는 것까지가 이번 스코프
- 사람 샘플 검증 프로세스(주간/월간 수동 평가) — 프로세스/운영 문제,
  코드로 자동화할 대상이 아님
- false positive 유형 분류 — 평가 데이터가 실제로 쌓인 뒤 별도 분석
  작업으로 진행
- `review_jobs.status='pending'` 도입 — 위 Ruling 참고, 필요해지면 추가
- 평가 대시보드(웹 UI) — `evaluate_reviews.py` 콘솔 출력으로 충분,
  대시보드는 실제 필요성이 생기면 별도 스코프
