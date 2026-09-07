# 6단계 — 평가 루프 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** review_records를 PostgreSQL에 영속 저장하고, Claude Code의 PR
반영/미반영 신호를 (미래에) 소비할 수 있는 evaluation 인프라를 구축해
`scripts/evaluate_reviews.py`로 acceptance rate를 집계할 수 있게 한다.

**Architecture:** 새 `app/evaluation/` 패키지가 PostgreSQL(3테이블:
review_jobs/review_records/review_feedback)을 SQLAlchemy(async) +
Alembic으로 관리한다. `ReviewRequestConsumer`는 리뷰 완료/실패 시
best-effort로 `EvaluationRepository`에 저장한다. 새 Kafka consumer
(`ReviewFeedbackConsumer`)는 `pr.comment.reflected` 이벤트(GitHub App
팀이 향후 발행할 예정, 이번 스코프엔 발행 측 없음)를 소비해
`review_feedback`을 갱신한다.

**Tech Stack:** SQLAlchemy 2.0(asyncio) + asyncpg(운영) + aiosqlite(테스트) +
Alembic, 기존 aiokafka/pydantic 패턴 재사용.

**Spec:** `docs/superpowers/specs/2026-09-07-evaluation-loop-design.md`

## Global Constraints

- `evaluation_enabled: bool = False` 기본값 — Postgres 없는 환경에서도
  앱이 정상 기동해야 한다 (다른 optional 기능과 동일 원칙).
- DB 저장 실패가 리뷰 파이프라인(발행/커밋/dedup)을 절대 막지 않는다 —
  모든 저장은 Kafka 발행 이후, best-effort(예외 로깅만).
- `review_jobs.status`는 `completed`/`failed` 두 값만 쓴다(`pending` 없음
  — 진행 중 상태는 이미 `DedupStore`가 추적).
- `review_feedback` upsert 시 FK 위반(모르는 reviewJobId)은 예외를
  삼키고 로깅만 한다.
- `reviews` 컬럼은 SQLAlchemy `JSON().with_variant(JSONB, "postgresql")` —
  테스트는 SQLite in-memory로, 운영은 Postgres JSONB로 동작해야 한다.
- 새 서드파티 표 포맷팅 라이브러리 추가 금지 — `evaluate_reviews.py` 출력은
  f-string 정렬로 충분하다(YAGNI).
- 타입 힌트 필수, `async def`+`await`만 사용(architecture.md), 주석은 WHY만.

---

### Task 1: 설정 / 의존성 / Postgres 인프라

**Files:**
- Modify: `app/core/config.py`
- Modify: `pyproject.toml`
- Modify: `docker-compose.yml`
- Modify: `docker-compose.override.yml`
- Modify: `.env.example`
- Test: `tests/test_config.py` (신규)

**Interfaces:**
- Consumes: 없음(이 계획의 첫 태스크)
- Produces: `Settings.evaluation_enabled: bool`, `Settings.database_url: str`,
  `Settings.kafka_review_feedback_topic: str` — Task 2~7이 전부 이 3개
  설정을 참조한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_config.py` 새로 작성:

```python
from app.core.config import Settings


def test_evaluation_settings_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.evaluation_enabled is False
    assert settings.database_url == "postgresql+asyncpg://dovi:dovi@localhost:5432/dovi"
    assert settings.kafka_review_feedback_topic == "pr.comment.reflected"
```

`Settings(_env_file=None)`을 쓰는 이유: 로컬 `.env` 파일이 있으면 그 값이
우선 로드되어 기본값 assertion이 환경에 따라 깨질 수 있다 — 이 테스트는
코드에 박힌 기본값만 확인한다.

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'evaluation_enabled'`

- [ ] **Step 3: `app/core/config.py`에 설정 3개 추가**

`class Settings(BaseSettings):` 안, 기존 `dependency_check_enabled: bool = False`
줄 바로 다음에 추가:

```python
    # 기본 False: PostgreSQL이 없는 레포/환경에서도 앱이 정상 기동해야 한다.
    # Alembic 마이그레이션(alembic/) 적용 후 .env에서 명시적으로 켠다.
    evaluation_enabled: bool = False
    database_url: str = "postgresql+asyncpg://dovi:dovi@localhost:5432/dovi"
    kafka_review_feedback_topic: str = "pr.comment.reflected"
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: `pyproject.toml`에 의존성 추가**

`[project] dependencies` 배열(알파벳 순서 유지, 기존 항목들 사이에 삽입)에:

```toml
    "alembic>=1.14.0",
    "asyncpg>=0.30.0",
```

`sentence-transformers>=5.1.2,` 다음 줄에 삽입(알파벳 순 s 다음):

```toml
    "sqlalchemy[asyncio]>=2.0.36",
```

`[dependency-groups] dev` 배열에 추가(테스트 전용, SQLite 드라이버):

```toml
    "aiosqlite>=0.21.0",
```

- [ ] **Step 6: 의존성 설치**

Run: `uv sync`
Expected: `alembic`, `asyncpg`, `sqlalchemy`, `aiosqlite`가 lockfile에
추가되고 설치 성공

- [ ] **Step 7: `docker-compose.yml`(운영)에 postgres 서비스 추가**

`services:` 아래 `qdrant:` 블록 앞에 삽입:

```yaml
  postgres:
    image: postgres:16
    environment:
      - POSTGRES_USER=dovi
      - POSTGRES_PASSWORD=dovi
      - POSTGRES_DB=dovi
    # 호스트 포트를 열지 않는다 — api가 컴포즈 내부 네트워크로 service명
    # "postgres"로 접속. qdrant와 달리 호스트에서 직접 붙을 CLI 툴이 없다.
    volumes:
      - postgres_data:/var/lib/postgresql/data
```

`api:` 서비스의 `depends_on:` 리스트에 `- postgres` 추가(기존 `- qdrant`
다음 줄).

`volumes:` 최상위 블록에 `postgres_data:` 추가(기존 `qdrant_storage:`,
`hf_cache:` 옆).

- [ ] **Step 8: `docker-compose.override.yml`(로컬 개발)에 postgres 서비스 추가**

`services:` 아래 `redis:` 블록 다음에 삽입(개발자가 `psql`로 직접 붙을 수
있도록 포트를 연다):

```yaml
  postgres:
    image: postgres:16
    environment:
      - POSTGRES_USER=dovi
      - POSTGRES_PASSWORD=dovi
      - POSTGRES_DB=dovi
    ports:
      - "5432:5432"
```

- [ ] **Step 9: docker-compose 설정 검증**

Run: `docker compose config --quiet`
Expected: 에러 없이 종료(YAML/스키마 유효성 확인)

- [ ] **Step 10: 로컬 postgres 기동 확인**

Run: `docker compose up -d postgres && sleep 3 && docker compose exec postgres pg_isready -U dovi`
Expected: `... - accepting connections`

- [ ] **Step 11: `.env.example`에 항목 추가**

`API_SPEC_COLLECTION_NAME=dovi_api_spec_chunks` 줄 다음에 빈 줄 하나 두고
추가:

```
# Alembic 마이그레이션(alembic/) 적용 후에만 true로 설정
EVALUATION_ENABLED=false
DATABASE_URL=postgresql+asyncpg://dovi:dovi@localhost:5432/dovi
KAFKA_REVIEW_FEEDBACK_TOPIC=pr.comment.reflected
```

- [ ] **Step 12: 전체 테스트 스위트 회귀 확인**

Run: `uv run pytest`
Expected: 기존 테스트 전부 PASS (설정 추가만으로는 다른 동작 안 바뀜)

- [ ] **Step 13: Commit**

```bash
git add app/core/config.py pyproject.toml uv.lock docker-compose.yml docker-compose.override.yml .env.example tests/test_config.py
git commit -m "feat :: 평가 루프 설정/의존성/Postgres 인프라 추가"
```

---

### Task 2: DB 모델 + Alembic 마이그레이션 + 세션 팩토리

**Files:**
- Create: `app/evaluation/__init__.py`
- Create: `app/evaluation/models.py`
- Create: `app/evaluation/db.py`
- Create: `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/0001_create_evaluation_tables.py`
- Test: `tests/test_evaluation_models.py`

**Interfaces:**
- Consumes: `Settings.database_url` (Task 1)
- Produces: `app.evaluation.models.Base`, `ReviewJobRow`, `ReviewRecordRow`,
  `ReviewFeedbackRow` (Task 3, 7이 사용); `app.evaluation.db.create_engine(settings) -> AsyncEngine`,
  `create_session_factory(engine) -> async_sessionmaker[AsyncSession]` (Task 3, 6이 사용)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_evaluation_models.py`:

```python
from sqlalchemy.ext.asyncio import create_async_engine


async def test_metadata_creates_all_tables() -> None:
    from app.evaluation.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_evaluation_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.evaluation'`

- [ ] **Step 3: `app/evaluation/__init__.py` 빈 파일 생성**

```python
```

- [ ] **Step 4: `app/evaluation/models.py` 작성**

```python
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_ReviewsJson = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class ReviewJobRow(Base):
    __tablename__ = "review_jobs"

    review_job_id: Mapped[str] = mapped_column(String, primary_key=True)
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    fail_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReviewRecordRow(Base):
    __tablename__ = "review_records"

    review_job_id: Mapped[str] = mapped_column(
        String, ForeignKey("review_jobs.review_job_id"), primary_key=True
    )
    reviews: Mapped[list[dict]] = mapped_column(_ReviewsJson, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)


class ReviewFeedbackRow(Base):
    __tablename__ = "review_feedback"
    __table_args__ = (
        UniqueConstraint(
            "review_job_id", "finding_index", name="uq_review_feedback_job_finding"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_job_id: Mapped[str] = mapped_column(
        String, ForeignKey("review_jobs.review_job_id"), nullable=False
    )
    finding_index: Mapped[int] = mapped_column(Integer, nullable=False)
    reflected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/test_evaluation_models.py -v`
Expected: PASS

- [ ] **Step 6: `app/evaluation/db.py` 작성**

```python
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.database_url)


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
```

- [ ] **Step 7: Alembic(async 템플릿) 스캐폴딩**

Run: `uv run alembic init -t async alembic`
Expected: `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`,
`alembic/versions/` 생성됨

- [ ] **Step 8: `alembic/env.py` 수정 — 프로젝트 설정/모델과 연결**

`import` 블록 상단에 추가:

```python
from app.core.config import get_settings
from app.evaluation.models import Base
```

`target_metadata = None` 줄을 다음으로 교체:

```python
target_metadata = Base.metadata
```

`run_migrations_online()` 함수 안에서 `connectable = async_engine_from_config(...)`
로 시작하는 블록을 다음으로 교체(alembic.ini의 `sqlalchemy.url`이 아니라
앱과 동일하게 `Settings.database_url`을 단일 진실 공급원으로 쓴다):

```python
    connectable = create_async_engine(get_settings().database_url, poolclass=pool.NullPool)
```

(`create_async_engine`은 `sqlalchemy.ext.asyncio`에서 이미 async 템플릿이
import해둔 이름이다 — 없으면 파일 상단 import에 추가한다.)

- [ ] **Step 9: 마이그레이션 파일 직접 작성**

`alembic/versions/0001_create_evaluation_tables.py` (autogenerate 대신
스키마를 스펙대로 직접 작성 — DB 연결 없이도 작성 가능하고 내용이
정확히 통제된다):

```python
"""create evaluation tables

Revision ID: 0001
Revises:
Create Date: 2026-09-07

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_jobs",
        sa.Column("review_job_id", sa.String(), primary_key=True),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("fail_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "review_records",
        sa.Column(
            "review_job_id",
            sa.String(),
            sa.ForeignKey("review_jobs.review_job_id"),
            primary_key=True,
        ),
        sa.Column("reviews", JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("model_version", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
    )
    op.create_table(
        "review_feedback",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "review_job_id",
            sa.String(),
            sa.ForeignKey("review_jobs.review_job_id"),
            nullable=False,
        ),
        sa.Column("finding_index", sa.Integer(), nullable=False),
        sa.Column("reflected", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "review_job_id", "finding_index", name="uq_review_feedback_job_finding"
        ),
    )


def downgrade() -> None:
    op.drop_table("review_feedback")
    op.drop_table("review_records")
    op.drop_table("review_jobs")
```

- [ ] **Step 10: 로컬 Postgres에 마이그레이션 적용 검증 (수동)**

Run:
```bash
docker compose up -d postgres
DATABASE_URL=postgresql+asyncpg://dovi:dovi@localhost:5432/dovi uv run alembic upgrade head
docker compose exec postgres psql -U dovi -d dovi -c '\dt'
```
Expected: `review_jobs`, `review_records`, `review_feedback` 3개 테이블이
목록에 나타남. (이 스텝은 자동화 테스트가 아니다 — 스펙의 "Alembic
마이그레이션 자체는 유닛 테스트 대상이 아니다" 원칙에 따른 1회성 수동
검증이다. `get_settings()`가 `.env`를 읽으므로, `.env`가 없으면 위처럼
환경변수를 직접 넘긴다.)

- [ ] **Step 11: 전체 테스트 스위트 회귀 확인**

Run: `uv run pytest`
Expected: 전부 PASS

- [ ] **Step 12: Commit**

```bash
git add app/evaluation/__init__.py app/evaluation/models.py app/evaluation/db.py alembic.ini alembic/ tests/test_evaluation_models.py
git commit -m "feat :: 평가 루프 DB 모델 및 Alembic 마이그레이션 추가"
```

---

### Task 3: `ReviewFeedbackEvent` 스키마 + `EvaluationRepository`

**Files:**
- Create: `app/evaluation/schema.py`
- Create: `app/evaluation/repository.py`
- Test: `tests/test_evaluation_repository.py`

**Interfaces:**
- Consumes: `app.evaluation.models.Base/ReviewJobRow/ReviewRecordRow/ReviewFeedbackRow` (Task 2),
  `app.review.schema.CamelModel/ReviewCompletedEvent/ReviewFailedEvent/ReviewComment` (기존)
- Produces: `app.evaluation.schema.ReviewFeedbackEvent(review_job_id, finding_index, reflected, reason)` (Task 4가 사용),
  `app.evaluation.repository.EvaluationRepository` Protocol + `SqlAlchemyEvaluationRepository(session_factory)`
  with `save_completed(event: ReviewCompletedEvent) -> None`,
  `save_failed(event: ReviewFailedEvent) -> None`,
  `upsert_feedback(feedback: ReviewFeedbackEvent) -> None` (Task 4, 5, 6이 사용)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_evaluation_repository.py`:

```python
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.evaluation.models import Base, ReviewFeedbackRow, ReviewJobRow, ReviewRecordRow
from app.evaluation.repository import SqlAlchemyEvaluationRepository
from app.evaluation.schema import ReviewFeedbackEvent
from app.review.schema import ReviewComment, ReviewCompletedEvent, ReviewFailedEvent


@pytest.fixture
async def repo_and_sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # SQLite는 기본적으로 FK 제약을 강제하지 않는다 — Postgres와 동일하게
    # FK 위반이 실제로 에러를 내는지 검증하려면 명시적으로 켜야 한다.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield SqlAlchemyEvaluationRepository(session_factory), session_factory
    await engine.dispose()


def _completed_event() -> ReviewCompletedEvent:
    return ReviewCompletedEvent(
        review_job_id="123:45:abcabc",
        repository_id=123,
        pr_number=45,
        head_sha="abcabc",
        summary="ok",
        reviews=[
            ReviewComment(
                severity="minor",
                confidence=1.0,
                file_path="a.py",
                line=1,
                title="t",
                message="m",
                evidence=["e"],
            )
        ],
        model_version="qwen2.5-coder-32b",
        prompt_version="v1",
    )


async def test_save_completed_persists_job_and_record(repo_and_sessions) -> None:
    repo, session_factory = repo_and_sessions

    await repo.save_completed(_completed_event())

    async with session_factory() as session:
        job = await session.get(ReviewJobRow, "123:45:abcabc")
        record = await session.get(ReviewRecordRow, "123:45:abcabc")
    assert job is not None
    assert job.status == "completed"
    assert job.repository_id == 123
    assert record is not None
    assert record.summary == "ok"
    assert record.model_version == "qwen2.5-coder-32b"
    assert len(record.reviews) == 1
    assert record.reviews[0]["severity"] == "minor"


async def test_save_completed_is_idempotent(repo_and_sessions) -> None:
    repo, session_factory = repo_and_sessions
    event_ = _completed_event()

    await repo.save_completed(event_)
    await repo.save_completed(event_)

    async with session_factory() as session:
        rows = (
            (await session.execute(select(ReviewJobRow).where(ReviewJobRow.review_job_id == "123:45:abcabc")))
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_save_failed_parses_review_job_id(repo_and_sessions) -> None:
    repo, session_factory = repo_and_sessions
    failed = ReviewFailedEvent(review_job_id="123:45:abcabc", head_sha="abcabc", reason="timeout")

    await repo.save_failed(failed)

    async with session_factory() as session:
        job = await session.get(ReviewJobRow, "123:45:abcabc")
    assert job is not None
    assert job.status == "failed"
    assert job.fail_reason == "timeout"
    assert job.repository_id == 123
    assert job.pr_number == 45


async def test_upsert_feedback_creates_then_updates(repo_and_sessions) -> None:
    repo, session_factory = repo_and_sessions
    await repo.save_completed(_completed_event())

    await repo.upsert_feedback(
        ReviewFeedbackEvent(
            review_job_id="123:45:abcabc", finding_index=0, reflected=True, reason="applied"
        )
    )
    await repo.upsert_feedback(
        ReviewFeedbackEvent(
            review_job_id="123:45:abcabc", finding_index=0, reflected=False, reason="reverted"
        )
    )

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ReviewFeedbackRow).where(
                        ReviewFeedbackRow.review_job_id == "123:45:abcabc"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].reflected is False
    assert rows[0].reason == "reverted"


async def test_upsert_feedback_unknown_review_job_id_does_not_raise(repo_and_sessions) -> None:
    repo, _ = repo_and_sessions

    await repo.upsert_feedback(
        ReviewFeedbackEvent(review_job_id="does-not-exist", finding_index=0, reflected=True, reason=None)
    )
    # 예외 없이 조용히 로깅만 하면 성공 (assert 없음 — 예외가 안 나는 것 자체가 검증)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_evaluation_repository.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.evaluation.schema'`

- [ ] **Step 3: `app/evaluation/schema.py` 작성**

```python
from app.review.schema import CamelModel


class ReviewFeedbackEvent(CamelModel):
    review_job_id: str
    finding_index: int
    reflected: bool
    reason: str | None = None
```

- [ ] **Step 4: `app/evaluation/repository.py` 작성**

```python
import logging
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.evaluation.models import ReviewFeedbackRow, ReviewJobRow, ReviewRecordRow
from app.evaluation.schema import ReviewFeedbackEvent
from app.review.schema import ReviewCompletedEvent, ReviewFailedEvent

logger = logging.getLogger(__name__)


class EvaluationRepository(Protocol):
    async def save_completed(self, event: ReviewCompletedEvent) -> None: ...

    async def save_failed(self, event: ReviewFailedEvent) -> None: ...

    async def upsert_feedback(self, feedback: ReviewFeedbackEvent) -> None: ...


def _parse_review_job_id(review_job_id: str) -> tuple[int, int, str]:
    """reviewJobId는 "repositoryId:prNumber:headSha" 형태다
    (app/review/schema.py의 make_review_job_id 참고). ReviewFailedEvent에는
    repository_id/pr_number 필드가 없어 이 문자열에서 역으로 파싱해야 한다."""
    repository_id_str, pr_number_str, head_sha = review_job_id.split(":", 2)
    return int(repository_id_str), int(pr_number_str), head_sha


class SqlAlchemyEvaluationRepository:
    """review_jobs/review_records/review_feedback을 관리한다.

    모든 쓰기는 get-then-write 방식이다(DB 네이티브 ON CONFLICT 대신) —
    Postgres/SQLite 양쪽에서 동일하게 동작해야 하고, 이 서비스는 같은
    reviewJobId가 동시에 두 번 쓰이는 경쟁 상황이 없다(Kafka consumer가
    reviewJobId당 dedup으로 직렬 처리).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save_completed(self, event: ReviewCompletedEvent) -> None:
        async with self._session_factory() as session, session.begin():
            job = await session.get(ReviewJobRow, event.review_job_id)
            if job is None:
                session.add(
                    ReviewJobRow(
                        review_job_id=event.review_job_id,
                        repository_id=event.repository_id,
                        pr_number=event.pr_number,
                        head_sha=event.head_sha,
                        status="completed",
                        fail_reason=None,
                        created_at=datetime.now(UTC),
                    )
                )
            else:
                job.status = "completed"
                job.fail_reason = None

            reviews_payload = [review.model_dump(by_alias=True) for review in event.reviews]
            record = await session.get(ReviewRecordRow, event.review_job_id)
            if record is None:
                session.add(
                    ReviewRecordRow(
                        review_job_id=event.review_job_id,
                        reviews=reviews_payload,
                        summary=event.summary,
                        model_version=event.model_version,
                        prompt_version=event.prompt_version,
                    )
                )
            else:
                record.reviews = reviews_payload
                record.summary = event.summary
                record.model_version = event.model_version
                record.prompt_version = event.prompt_version

    async def save_failed(self, event: ReviewFailedEvent) -> None:
        repository_id, pr_number, head_sha = _parse_review_job_id(event.review_job_id)
        async with self._session_factory() as session, session.begin():
            job = await session.get(ReviewJobRow, event.review_job_id)
            if job is None:
                session.add(
                    ReviewJobRow(
                        review_job_id=event.review_job_id,
                        repository_id=repository_id,
                        pr_number=pr_number,
                        head_sha=head_sha,
                        status="failed",
                        fail_reason=event.reason,
                        created_at=datetime.now(UTC),
                    )
                )
            else:
                job.status = "failed"
                job.fail_reason = event.reason

    async def upsert_feedback(self, feedback: ReviewFeedbackEvent) -> None:
        # ReviewFeedbackRow의 PK는 자동증가 id뿐이라 (review_job_id,
        # finding_index) 복합 유니크 키로 조회하려면 select()가 필요하다
        # (session.get()은 PK 조회 전용이라 여기 못 쓴다).
        try:
            async with self._session_factory() as session, session.begin():
                existing = await session.scalar(
                    select(ReviewFeedbackRow).where(
                        ReviewFeedbackRow.review_job_id == feedback.review_job_id,
                        ReviewFeedbackRow.finding_index == feedback.finding_index,
                    )
                )
                if existing is None:
                    session.add(
                        ReviewFeedbackRow(
                            review_job_id=feedback.review_job_id,
                            finding_index=feedback.finding_index,
                            reflected=feedback.reflected,
                            reason=feedback.reason,
                            updated_at=datetime.now(UTC),
                        )
                    )
                else:
                    existing.reflected = feedback.reflected
                    existing.reason = feedback.reason
                    existing.updated_at = datetime.now(UTC)
        except IntegrityError:
            logger.exception(
                "failed to upsert review_feedback (unknown reviewJobId=%s?)",
                feedback.review_job_id,
            )
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/test_evaluation_repository.py -v`
Expected: PASS (5개 테스트 전부)

- [ ] **Step 6: 전체 테스트 스위트 회귀 확인**

Run: `uv run pytest`
Expected: 전부 PASS

- [ ] **Step 7: Commit**

```bash
git add app/evaluation/schema.py app/evaluation/repository.py tests/test_evaluation_repository.py
git commit -m "feat :: EvaluationRepository로 review_jobs/records/feedback 저장 구현"
```

---

### Task 4: `ReviewFeedbackConsumer` (`pr.comment.reflected` 소비)

**Files:**
- Create: `app/evaluation/feedback_consumer.py`
- Test: `tests/test_evaluation_feedback_consumer.py`

**Interfaces:**
- Consumes: `app.kafka.consumer.MessageSource` Protocol (기존),
  `app.evaluation.repository.EvaluationRepository` Protocol (Task 3),
  `app.evaluation.schema.ReviewFeedbackEvent` (Task 3)
- Produces: `app.evaluation.feedback_consumer.ReviewFeedbackConsumer(source, repository)`
  with `async def run(shutdown=None) -> None`, `async def handle(raw: bytes) -> None`
  (Task 6이 사용)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_evaluation_feedback_consumer.py`:

```python
from collections.abc import AsyncIterator

from app.evaluation.feedback_consumer import ReviewFeedbackConsumer
from app.evaluation.schema import ReviewFeedbackEvent


class FakeMessage:
    def __init__(self, value: bytes) -> None:
        self.value = value


class FakeSource:
    def __init__(self, messages: list[FakeMessage]) -> None:
        self._messages = messages
        self.commit_count = 0

    async def __aiter__(self) -> AsyncIterator[FakeMessage]:
        for message in self._messages:
            yield message

    async def commit(self) -> None:
        self.commit_count += 1


class FakeRepository:
    def __init__(self) -> None:
        self.upserted: list[ReviewFeedbackEvent] = []

    async def save_completed(self, event: object) -> None:
        raise AssertionError("not used by this consumer")

    async def save_failed(self, event: object) -> None:
        raise AssertionError("not used by this consumer")

    async def upsert_feedback(self, feedback: ReviewFeedbackEvent) -> None:
        self.upserted.append(feedback)


async def test_handle_valid_event_upserts_feedback() -> None:
    repository = FakeRepository()
    consumer = ReviewFeedbackConsumer(FakeSource([]), repository)
    raw = (
        b'{"reviewJobId": "1:2:sha", "findingIndex": 0, '
        b'"reflected": true, "reason": "applied"}'
    )

    await consumer.handle(raw)

    assert len(repository.upserted) == 1
    assert repository.upserted[0].review_job_id == "1:2:sha"
    assert repository.upserted[0].reflected is True


async def test_handle_invalid_payload_skips_without_raising() -> None:
    repository = FakeRepository()
    consumer = ReviewFeedbackConsumer(FakeSource([]), repository)

    await consumer.handle(b"not json")

    assert repository.upserted == []


async def test_run_processes_all_messages_and_commits() -> None:
    repository = FakeRepository()
    valid = (
        b'{"reviewJobId": "1:2:sha", "findingIndex": 0, '
        b'"reflected": false, "reason": null}'
    )
    source = FakeSource([FakeMessage(valid), FakeMessage(b"bad")])
    consumer = ReviewFeedbackConsumer(source, repository)

    await consumer.run()

    assert len(repository.upserted) == 1
    assert source.commit_count == 2
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_evaluation_feedback_consumer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.evaluation.feedback_consumer'`

- [ ] **Step 3: `app/evaluation/feedback_consumer.py` 작성**

```python
import asyncio
import logging

from pydantic import ValidationError

from app.evaluation.repository import EvaluationRepository
from app.evaluation.schema import ReviewFeedbackEvent
from app.kafka.consumer import MessageSource

logger = logging.getLogger(__name__)


class ReviewFeedbackConsumer:
    """pr.comment.reflected 이벤트를 소비해 review_feedback을 갱신한다.

    발행 측(GitHub App 팀)이 아직 구현되지 않아 실제 프로덕션 트래픽은
    없다 — consumer 로직만 미리 준비해둔다. dedup은 두지 않는다
    (upsert_feedback이 멱등이라 재전달되어도 안전).
    """

    def __init__(self, source: MessageSource, repository: EvaluationRepository) -> None:
        self._source = source
        self._repository = repository

    async def run(self, shutdown: asyncio.Event | None = None) -> None:
        async for message in self._source:
            await self.handle(message.value)
            await self._source.commit()
            if shutdown is not None and shutdown.is_set():
                return

    async def handle(self, raw: bytes) -> None:
        try:
            event = ReviewFeedbackEvent.model_validate_json(raw)
        except ValidationError:
            logger.exception("invalid ReviewFeedbackEvent payload, skipping")
            return
        await self._repository.upsert_feedback(event)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_evaluation_feedback_consumer.py -v`
Expected: PASS (3개 테스트 전부)

- [ ] **Step 5: 전체 테스트 스위트 회귀 확인**

Run: `uv run pytest`
Expected: 전부 PASS

- [ ] **Step 6: Commit**

```bash
git add app/evaluation/feedback_consumer.py tests/test_evaluation_feedback_consumer.py
git commit -m "feat :: pr.comment.reflected 소비하는 ReviewFeedbackConsumer 추가"
```

---

### Task 5: `ReviewRequestConsumer`에 평가 저장 배선

**Files:**
- Modify: `app/kafka/consumer.py`
- Test: `tests/test_kafka_consumer.py`

**Interfaces:**
- Consumes: `app.evaluation.repository.EvaluationRepository` Protocol (Task 3)
- Produces: `ReviewRequestConsumer.__init__`의 새 파라미터
  `evaluation_repository: EvaluationRepository | None = None` (Task 6이 사용)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_kafka_consumer.py` 끝에 추가(기존 `FakeMessage`/`FakeSource`/
`FakeLLM`/`FailingLLM`과 이 파일의 기존 `ReviewPipeline`/이벤트 생성
헬퍼를 그대로 재사용한다 — 파일 상단은 건드리지 않는다):

```python
class FakeEvaluationRepository:
    def __init__(self, *, raise_on_completed: bool = False) -> None:
        self.completed: list[ReviewCompletedEvent] = []
        self.failed: list[ReviewFailedEvent] = []
        self._raise_on_completed = raise_on_completed

    async def save_completed(self, event: ReviewCompletedEvent) -> None:
        if self._raise_on_completed:
            raise RuntimeError("db down")
        self.completed.append(event)

    async def save_failed(self, event: ReviewFailedEvent) -> None:
        self.failed.append(event)

    async def upsert_feedback(self, feedback: object) -> None:
        raise AssertionError("not used by ReviewRequestConsumer")


async def test_handle_saves_completed_event_to_evaluation_repository() -> None:
    output = ReviewModelOutput(summary="ok", reviews=[])
    pipeline = ReviewPipeline(FakeLLM(output), model_version="m", prompt_version="v1")
    evaluation_repository = FakeEvaluationRepository()
    consumer = ReviewRequestConsumer(
        FakeSource([]),
        pipeline,
        FakePublisher(),
        FakeDedup(),
        evaluation_repository=evaluation_repository,
    )
    event = ReviewRequestedEvent(
        review_job_id="1:2:sha",
        repository_id=1,
        pr_number=2,
        head_sha="sha",
        base_sha="base",
        changed_files=[ChangedFile(file_path="a.py", status="modified", patch="@@")],
    )

    await consumer.handle(event.model_dump_json().encode())

    assert len(evaluation_repository.completed) == 1


async def test_handle_evaluation_repository_failure_does_not_break_pipeline_flow() -> None:
    output = ReviewModelOutput(summary="ok", reviews=[])
    pipeline = ReviewPipeline(FakeLLM(output), model_version="m", prompt_version="v1")
    publisher = FakePublisher()
    dedup = FakeDedup()
    evaluation_repository = FakeEvaluationRepository(raise_on_completed=True)
    consumer = ReviewRequestConsumer(
        FakeSource([]), pipeline, publisher, dedup, evaluation_repository=evaluation_repository
    )
    event = ReviewRequestedEvent(
        review_job_id="1:2:sha",
        repository_id=1,
        pr_number=2,
        head_sha="sha",
        base_sha="base",
        changed_files=[ChangedFile(file_path="a.py", status="modified", patch="@@")],
    )

    await consumer.handle(event.model_dump_json().encode())

    assert len(publisher.completed) == 1
    assert dedup.completed_calls == ["1:2:sha"]


async def test_handle_without_evaluation_repository_still_works() -> None:
    output = ReviewModelOutput(summary="ok", reviews=[])
    pipeline = ReviewPipeline(FakeLLM(output), model_version="m", prompt_version="v1")
    consumer = ReviewRequestConsumer(FakeSource([]), pipeline, FakePublisher(), FakeDedup())
    event = ReviewRequestedEvent(
        review_job_id="1:2:sha",
        repository_id=1,
        pr_number=2,
        head_sha="sha",
        base_sha="base",
        changed_files=[ChangedFile(file_path="a.py", status="modified", patch="@@")],
    )

    await consumer.handle(event.model_dump_json().encode())
    # evaluation_repository=None이어도 예외 없이 끝나면 성공
```

이 스텝 작성 전, `tests/test_kafka_consumer.py`에 이미 있는 `FakePublisher`/
`FakeDedup` 클래스의 정확한 이름과 속성(`completed`/`completed_calls` 등)을
먼저 `Read`로 확인하고, 위 코드의 이름이 실제와 다르면 실제 이름에 맞춰
쓴다 — 이 파일은 임의로 새 fake를 또 만들지 않고 기존 것을 재사용한다.

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_kafka_consumer.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'evaluation_repository'`

- [ ] **Step 3: `app/kafka/consumer.py` 수정**

`from app.review.dedup import DedupStore` 다음 줄에 추가:

```python
from app.evaluation.repository import EvaluationRepository
```

`ReviewRequestConsumer.__init__`을 다음으로 교체:

```python
    def __init__(
        self,
        source: MessageSource,
        pipeline: ReviewPipeline,
        producer: EventPublisher,
        dedup: DedupStore,
        evaluation_repository: EvaluationRepository | None = None,
    ) -> None:
        self._source = source
        self._pipeline = pipeline
        self._producer = producer
        self._dedup = dedup
        self._evaluation_repository = evaluation_repository
```

`handle()`의 `try:` 블록을 다음으로 교체(마지막에 `_save_evaluation_record`
호출 한 줄만 추가):

```python
        try:
            result = await self._pipeline.run(event)
            if isinstance(result, ReviewCompletedEvent):
                await self._producer.publish_completed(result)
                await self._dedup.mark_completed(event.review_job_id)
            else:
                await self._producer.publish_failed(result)
                await self._dedup.mark_failed(event.review_job_id)
            await self._save_evaluation_record(result)
        except asyncio.CancelledError:
            # graceful shutdown 유예시간을 넘겨 강제 취소된 경우 — 락을 풀어
            # 재전달된 메시지를 새 인스턴스가 TTL을 기다리지 않고 재처리하게 한다.
            await self._dedup.mark_failed(event.review_job_id)
            raise
```

클래스 끝에 새 메서드 추가:

```python
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
                "failed to persist evaluation record reviewJobId=%s",
                result.review_job_id,
            )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_kafka_consumer.py -v`
Expected: PASS

- [ ] **Step 5: 전체 테스트 스위트 회귀 확인**

Run: `uv run pytest && uv run ruff check . && uv run mypy .`
Expected: 전부 PASS/clean

- [ ] **Step 6: Commit**

```bash
git add app/kafka/consumer.py tests/test_kafka_consumer.py
git commit -m "feat :: ReviewRequestConsumer가 완료/실패 시 평가 레코드 저장"
```

---

### Task 6: Kafka client / `app/main.py` 배선

**Files:**
- Modify: `app/kafka/client.py`
- Modify: `app/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `app.evaluation.db.create_engine/create_session_factory` (Task 2),
  `app.evaluation.repository.SqlAlchemyEvaluationRepository` (Task 3),
  `app.evaluation.feedback_consumer.ReviewFeedbackConsumer` (Task 4)
- Produces: `app.kafka.client.create_review_feedback_consumer(settings) -> AIOKafkaConsumer`

- [ ] **Step 1: `app/kafka/client.py`에 factory 함수 추가**

파일 상단 `_COMMENT_ANSWER_CONSUMER_GROUP_ID = "..."` 다음 줄에 추가:

```python
_REVIEW_FEEDBACK_CONSUMER_GROUP_ID = "dovi-ai-review-feedback-engine"
```

파일 끝에 추가:

```python
def create_review_feedback_consumer(settings: Settings) -> AIOKafkaConsumer:
    logger.info(
        "creating kafka review-feedback consumer bootstrap_servers=%s topic=%s group_id=%s",
        settings.kafka_bootstrap_servers,
        settings.kafka_review_feedback_topic,
        _REVIEW_FEEDBACK_CONSUMER_GROUP_ID,
    )
    return AIOKafkaConsumer(
        settings.kafka_review_feedback_topic,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=_REVIEW_FEEDBACK_CONSUMER_GROUP_ID,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
```

(이 함수 자체는 새 테스트가 필요 없다 — `create_consumer`/
`create_comment_answer_consumer`도 별도 유닛 테스트가 없고, `app/main.py`
배선 테스트로 간접 검증된다. 기존 관례를 따른다.)

- [ ] **Step 2: 실패하는 테스트 작성**

`tests/test_main.py`에 `FakeQdrantClient` 클래스 다음, 기존
`test_lifespan_wires_dependency_resolver_when_enabled` 근처에 추가:

```python
class FakeEvaluationRepository:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def save_completed(self, event: object) -> None:
        pass

    async def save_failed(self, event: object) -> None:
        pass

    async def upsert_feedback(self, feedback: object) -> None:
        pass


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


async def test_lifespan_wires_evaluation_repository_and_feedback_consumer_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("EVALUATION_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    fake_feedback_consumer = FakeConsumerSource()
    fake_engine = FakeEngine()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )
    monkeypatch.setattr(
        "app.main.create_review_feedback_consumer",
        lambda settings: fake_feedback_consumer,
    )
    monkeypatch.setattr("app.evaluation.db.create_engine", lambda settings: fake_engine)
    monkeypatch.setattr(
        "app.evaluation.repository.SqlAlchemyEvaluationRepository",
        lambda session_factory: FakeEvaluationRepository(),
    )

    captured_kwargs: dict[str, object] = {}
    original_init = ReviewPipeline.__init__

    def capturing_init(self: ReviewPipeline, *args: object, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ReviewPipeline, "__init__", capturing_init)

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)
            assert fake_feedback_consumer.started

        assert fake_feedback_consumer.stopped
        assert fake_engine.disposed
    finally:
        get_settings.cache_clear()


async def test_lifespan_leaves_evaluation_repository_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")
    # EVALUATION_ENABLED를 아예 설정하지 않는다 (기본 False 확인)

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `uv run pytest tests/test_main.py -v -k evaluation`
Expected: FAIL — `ImportError: cannot import name 'create_review_feedback_consumer' from 'app.main'`

- [ ] **Step 4: `app/main.py` 수정**

`from app.kafka.client import (` import 블록에 `create_review_feedback_consumer`
추가:

```python
from app.kafka.client import (
    create_comment_answer_consumer,
    create_consumer,
    create_producer,
    create_review_feedback_consumer,
)
```

`dependency_resolver = None` ... 블록(`if settings.dependency_check_enabled:`)
바로 다음에 추가:

```python
    evaluation_repository = None
    evaluation_engine = None
    if settings.evaluation_enabled:
        from app.evaluation.db import create_engine, create_session_factory
        from app.evaluation.repository import SqlAlchemyEvaluationRepository

        evaluation_engine = create_engine(settings)
        session_factory = create_session_factory(evaluation_engine)
        evaluation_repository = SqlAlchemyEvaluationRepository(session_factory)
```

`pipeline = ReviewPipeline(...)` 다음 줄, `review_consumer = ReviewRequestConsumer(...)`
호출을 다음으로 교체:

```python
    review_consumer = ReviewRequestConsumer(
        kafka_consumer,
        pipeline,
        event_producer,
        dedup_store,
        evaluation_repository=evaluation_repository,
    )
```

`await comment_answer_kafka_consumer.start()` 다음 줄에 추가:

```python
    review_feedback_kafka_consumer = None
    if settings.evaluation_enabled:
        review_feedback_kafka_consumer = create_review_feedback_consumer(settings)
        await review_feedback_kafka_consumer.start()
```

`comment_answer_task = asyncio.create_task(...)` 다음, `tasks = (consumer_task, comment_answer_task)`
줄을 다음으로 교체:

```python
    tasks: list[asyncio.Task[None]] = [consumer_task, comment_answer_task]
    if review_feedback_kafka_consumer is not None:
        from app.evaluation.feedback_consumer import ReviewFeedbackConsumer

        review_feedback_consumer = ReviewFeedbackConsumer(
            review_feedback_kafka_consumer, evaluation_repository
        )
        review_feedback_task = asyncio.create_task(
            _run_consumer_forever(
                review_feedback_consumer, shutdown_event, name="review-feedback"
            )
        )
        tasks.append(review_feedback_task)
    tasks = tuple(tasks)
```

`finally:` 블록의 `await kafka_consumer.stop()` / `await comment_answer_kafka_consumer.stop()`
다음 줄에 추가:

```python
        if review_feedback_kafka_consumer is not None:
            await review_feedback_kafka_consumer.stop()
```

`if npm_registry_client is not None: await npm_registry_client.aclose()` 다음
줄에 추가:

```python
        if evaluation_engine is not None:
            await evaluation_engine.dispose()
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/test_main.py -v`
Expected: PASS (신규 2개 포함 전부)

- [ ] **Step 6: qdrant_client 회귀 방지 확인**

Run: `uv run python -c "import app.main; import sys; assert 'qdrant_client' not in sys.modules"`
Expected: 에러 없이 종료 (평가 루프 기능이 무거운 의존성을 끌어오지 않는지
확인 — 기존 관례)

- [ ] **Step 7: 전체 테스트 스위트 + 린트 + 타입체크**

Run: `uv run pytest && uv run ruff check . && uv run mypy .`
Expected: 전부 PASS/clean

- [ ] **Step 8: Commit**

```bash
git add app/kafka/client.py app/main.py tests/test_main.py
git commit -m "feat :: evaluation_enabled 배선 - repository/feedback consumer lifespan 연결"
```

---

### Task 7: `scripts/evaluate_reviews.py`

**Files:**
- Create: `scripts/evaluate_reviews.py`
- Test: `tests/test_evaluate_reviews.py`

**Interfaces:**
- Consumes: `app.evaluation.models.Base/ReviewJobRow/ReviewRecordRow/ReviewFeedbackRow` (Task 2),
  `app.core.config.get_settings` (기존)
- Produces: `scripts.evaluate_reviews.build_report(session) -> dict`,
  `collect_report() -> dict`, `main()` (CLI 엔트리포인트, 이후 태스크 없음)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_evaluate_reviews.py`:

```python
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.evaluation.models import Base, ReviewFeedbackRow, ReviewJobRow, ReviewRecordRow
from scripts.evaluate_reviews import build_report


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as s:
        yield s
    await engine.dispose()


async def _seed(
    session,
    *,
    job_id: str,
    severity: str,
    reflected: bool,
    model_version: str = "m1",
    prompt_version: str = "p1",
) -> None:
    now = datetime.now(UTC)
    session.add(
        ReviewJobRow(
            review_job_id=job_id,
            repository_id=1,
            pr_number=1,
            head_sha="sha",
            status="completed",
            fail_reason=None,
            created_at=now,
        )
    )
    session.add(
        ReviewRecordRow(
            review_job_id=job_id,
            reviews=[{"severity": severity}],
            summary="s",
            model_version=model_version,
            prompt_version=prompt_version,
        )
    )
    session.add(
        ReviewFeedbackRow(
            review_job_id=job_id,
            finding_index=0,
            reflected=reflected,
            reason=None,
            updated_at=now,
        )
    )
    await session.commit()


async def test_build_report_computes_acceptance_rates(session) -> None:
    await _seed(session, job_id="1", severity="critical", reflected=True)
    await _seed(session, job_id="2", severity="critical", reflected=False)
    await _seed(session, job_id="3", severity="minor", reflected=True)

    report = await build_report(session)

    assert report["total_feedback_count"] == 3
    assert report["overall_acceptance_rate"] == pytest.approx(2 / 3)
    assert report["by_severity"]["critical"] == pytest.approx(0.5)
    assert report["by_severity"]["minor"] == pytest.approx(1.0)
    assert report["by_model_prompt_version"]["m1::p1"] == pytest.approx(2 / 3)


async def test_build_report_empty_db_returns_none_rate(session) -> None:
    report = await build_report(session)

    assert report["total_feedback_count"] == 0
    assert report["overall_acceptance_rate"] is None
    assert report["by_severity"] == {}
    assert report["by_model_prompt_version"] == {}
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_evaluate_reviews.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.evaluate_reviews'`

- [ ] **Step 3: `scripts/evaluate_reviews.py` 작성**

```python
"""review_jobs/review_records/review_feedback을 집계해 리뷰 품질 지표를 낸다.

사용법:
    uv run python -m scripts.evaluate_reviews
    uv run python -m scripts.evaluate_reviews --json
"""

import argparse
import asyncio
import json
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.evaluation.models import ReviewFeedbackRow, ReviewRecordRow

_Row = tuple[str, bool, str, str]  # (severity, reflected, model_version, prompt_version)


async def _fetch_rows(session: AsyncSession) -> list[_Row]:
    result = await session.execute(
        select(
            ReviewRecordRow.reviews,
            ReviewFeedbackRow.finding_index,
            ReviewFeedbackRow.reflected,
            ReviewRecordRow.model_version,
            ReviewRecordRow.prompt_version,
        ).join(
            ReviewFeedbackRow,
            ReviewFeedbackRow.review_job_id == ReviewRecordRow.review_job_id,
        )
    )
    rows: list[_Row] = []
    for reviews, finding_index, reflected, model_version, prompt_version in result.all():
        if finding_index >= len(reviews):
            continue
        severity = reviews[finding_index]["severity"]
        rows.append((severity, reflected, model_version, prompt_version))
    return rows


def _acceptance_rate(flags: Sequence[bool]) -> float | None:
    if not flags:
        return None
    return sum(1 for flag in flags if flag) / len(flags)


def _by_severity(rows: list[_Row]) -> dict[str, float | None]:
    grouped: dict[str, list[bool]] = defaultdict(list)
    for severity, reflected, _, _ in rows:
        grouped[severity].append(reflected)
    return {severity: _acceptance_rate(flags) for severity, flags in grouped.items()}


def _by_version(rows: list[_Row]) -> dict[str, float | None]:
    grouped: dict[str, list[bool]] = defaultdict(list)
    for _, reflected, model_version, prompt_version in rows:
        grouped[f"{model_version}::{prompt_version}"].append(reflected)
    return {key: _acceptance_rate(flags) for key, flags in grouped.items()}


async def build_report(session: AsyncSession) -> dict[str, object]:
    rows = await _fetch_rows(session)
    return {
        "total_feedback_count": len(rows),
        "overall_acceptance_rate": _acceptance_rate([reflected for _, reflected, _, _ in rows]),
        "by_severity": _by_severity(rows),
        "by_model_prompt_version": _by_version(rows),
    }


async def collect_report() -> dict[str, object]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        report = await build_report(session)
    await engine.dispose()
    return report


def _format_rate(rate: float | None) -> str:
    return f"{rate:.1%}" if rate is not None else "N/A"


def _print_report(report: dict[str, object]) -> None:
    print(f"전체 feedback 수: {report['total_feedback_count']}")
    print(f"전체 acceptance rate: {_format_rate(report['overall_acceptance_rate'])}")  # type: ignore[arg-type]
    print()
    print("severity별 반영률:")
    for severity, rate in report["by_severity"].items():  # type: ignore[union-attr]
        print(f"  {severity.ljust(12)} {_format_rate(rate)}")
    print()
    print("모델/프롬프트 버전별 반영률:")
    for key, rate in report["by_model_prompt_version"].items():  # type: ignore[union-attr]
        print(f"  {key.ljust(40)} {_format_rate(rate)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="리뷰 평가 지표 집계")
    parser.add_argument("--json", action="store_true", help="JSON으로 출력")
    args = parser.parse_args()

    report = asyncio.run(collect_report())

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_evaluate_reviews.py -v`
Expected: PASS

- [ ] **Step 5: CLI 수동 스모크 테스트 (로컬 Postgres, 빈 DB)**

Run: `docker compose up -d postgres && DATABASE_URL=postgresql+asyncpg://dovi:dovi@localhost:5432/dovi uv run alembic upgrade head && DATABASE_URL=postgresql+asyncpg://dovi:dovi@localhost:5432/dovi uv run python -m scripts.evaluate_reviews`
Expected: `전체 feedback 수: 0` / `전체 acceptance rate: N/A` 출력, 에러 없음

- [ ] **Step 6: 전체 테스트 스위트 + 린트 + 타입체크**

Run: `uv run pytest && uv run ruff check . && uv run mypy .`
Expected: 전부 PASS/clean

- [ ] **Step 7: Commit**

```bash
git add scripts/evaluate_reviews.py tests/test_evaluate_reviews.py
git commit -m "feat :: evaluate_reviews.py로 acceptance rate 집계 스크립트 추가"
```
