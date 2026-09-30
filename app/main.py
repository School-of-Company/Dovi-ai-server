import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI

from app.api.health import router as health_router
from app.comment_answer.consumer import CommentAnswerConsumer
from app.comment_answer.pipeline import CommentAnswerPipeline
from app.common.logger import configure_logging
from app.core.config import get_settings
from app.kafka.client import (
    create_comment_answer_consumer,
    create_consumer,
    create_producer,
    create_repo_index_consumer,
    create_review_feedback_consumer,
    create_sandbox_probe_consumer,
)
from app.kafka.consumer import ReviewRequestConsumer
from app.kafka.producer import CommentAnswerEventProducer, ReviewEventProducer
from app.llm.openai_compatible_client import OpenAICompatibleLLMClient
from app.review.dedup import (
    create_comment_answer_dedup_store,
    create_dedup_store,
    create_redis_client,
)
from app.review.pipeline import ReviewPipeline

logger = logging.getLogger(__name__)


class _RunnableConsumer(Protocol):
    async def run(self, shutdown: asyncio.Event | None = None) -> None: ...


async def _run_consumer_forever(
    consumer: _RunnableConsumer, shutdown: asyncio.Event, *, name: str
) -> None:
    while not shutdown.is_set():
        try:
            await consumer.run(shutdown)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s consumer loop crashed, restarting in 5s", name)
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    if not settings.kafka_consumer_enabled:
        yield
        return

    review_on = settings.review_consumer_enabled
    comment_on = settings.comment_answer_consumer_enabled

    langfuse_client = None
    if settings.langfuse_enabled:
        from langfuse import Langfuse

        langfuse_client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )

    # LLM/GPU가 없는 배포(샌드박스 VM)는 review/comment-answer 컨슈머를 꺼서
    # llama-server·Qdrant 등에 연결을 시도하지 않게 한다.
    llm_client = None
    if review_on or comment_on:
        llm_client = OpenAICompatibleLLMClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )

    qdrant_client = None
    npm_registry_client = None
    retriever = None
    api_spec_retriever = None
    rag_on = review_on and settings.rag_enabled
    if rag_on:
        # qdrant-client는 numpy를 끌어오는데, 이를 지원 안 하는 CPU에서는 import만
        # 해도 죽는다(RAG를 안 켜는 배포에까지 그 위험을 지우지 않도록 지연 import).
        from qdrant_client import QdrantClient

        from app.rag.embeddings import CodeRankEmbedClient
        from app.rag.reranker import CrossEncoderReranker
        from app.rag.retriever import ProjectContextRetriever
        from app.rag.vector_store import QdrantVectorStore

        embedder = CodeRankEmbedClient(settings.embedding_model)
        reranker = CrossEncoderReranker(settings.reranker_model)
        qdrant_client = QdrantClient(url=settings.qdrant_url)
        vector_store = QdrantVectorStore(
            qdrant_client, settings.rag_collection_name, vector_size=embedder.dimension
        )
        retriever = ProjectContextRetriever(embedder, vector_store, reranker=reranker)

        # API 명세 검색은 RAG 인프라(embedder/qdrant_client)가 이미 있어야 의미가
        # 있으므로 rag_enabled 조건 안에서만 notion_sync_enabled를 추가로 확인한다.
        if settings.notion_sync_enabled:
            from app.context.api_spec_retriever import ApiSpecRetriever
            from app.rag.api_spec_vector_store import ApiSpecVectorStore

            api_spec_vector_store = ApiSpecVectorStore(
                qdrant_client, settings.api_spec_collection_name, vector_size=embedder.dimension
            )
            api_spec_retriever = ApiSpecRetriever(embedder, api_spec_vector_store)

    # notion_link_store가 redis_client를 필요로 하므로 pipeline 생성보다 먼저 만든다.
    redis_client = create_redis_client(settings)

    notion_link_store = None
    if review_on and settings.notion_sync_enabled:
        from app.context.api_spec_link_store import RedisNotionLinkStore

        # redis.asyncio.Redis의 실제 타입 스텁이 RedisLike보다 훨씬 넓어 구조적으로
        # 완전히 일치하지 않지만, set/get/keys를 문자열 인자로만 호출하므로 런타임에는 호환된다.
        notion_link_store = RedisNotionLinkStore(redis_client)  # type: ignore[arg-type]

    if review_on and (
        settings.dependency_check_enabled or settings.official_docs_workflow_enabled
    ):
        from app.context.npm_registry_client import NpmRegistryClient

        npm_registry_client = NpmRegistryClient()

    maven_central_client = None
    dependency_resolver = None
    if review_on and settings.dependency_check_enabled:
        from app.context.dependency_resolver import DependencyResolver
        from app.context.maven_central_client import MavenCentralClient
        from app.context.npm_deprecation_cache import RedisNpmDeprecationCache

        # redis.asyncio.Redis의 실제 타입 스텁이 RedisLike보다 훨씬 넓어 구조적으로
        # 완전히 일치하지 않지만, set/get을 문자열 인자로만 호출하므로 런타임에는 호환된다.
        npm_deprecation_cache = RedisNpmDeprecationCache(redis_client)  # type: ignore[arg-type]
        # RedisNpmDeprecationCache는 이름은 npm 전용이지만 동작은 범용
        # (name, version) -> CachedResult 캐시라, Maven relocation 결과도 별도
        # key prefix로 재사용한다.
        maven_relocation_cache = RedisNpmDeprecationCache(
            redis_client,  # type: ignore[arg-type]
            key_prefix="ai-review:maven-relocation:",
        )
        maven_central_client = MavenCentralClient()
        assert npm_registry_client is not None
        dependency_resolver = DependencyResolver(
            npm_registry_client,
            npm_deprecation_cache,
            maven_client=maven_central_client,
            maven_cache=maven_relocation_cache,
        )

    github_release_client = None
    official_docs_workflow = None
    if review_on and settings.official_docs_workflow_enabled:
        from app.context.github_release_client import GithubReleaseClient
        from app.context.official_docs_workflow import OfficialDocsWorkflow
        from app.context.release_notes_cache import RedisReleaseNotesCache

        github_release_client = GithubReleaseClient(token=settings.github_token)
        release_notes_cache = RedisReleaseNotesCache(redis_client)
        assert npm_registry_client is not None
        official_docs_workflow = OfficialDocsWorkflow(
            npm_registry_client, github_release_client, release_notes_cache
        )

    evaluation_repository = None
    evaluation_engine = None
    if review_on and settings.evaluation_enabled:
        from app.evaluation.db import create_engine, create_session_factory
        from app.evaluation.repository import SqlAlchemyEvaluationRepository

        evaluation_engine = create_engine(settings)
        session_factory = create_session_factory(evaluation_engine)
        evaluation_repository = SqlAlchemyEvaluationRepository(session_factory)

        # 스펙 판정: evaluation_enabled=true인데 DB/스키마가 없으면 다른 필수
        # 인프라와 동급으로 기동 실패시킨다(조용한 무동작 대신) — create_async_engine()은
        # lazy connect라 여기서 명시적으로 확인해야 한다. review_jobs를 직접
        # 건드리는 쿼리라 "연결은 되지만 마이그레이션 미적용" 케이스도 같이 잡는다.
        from sqlalchemy import select

        from app.evaluation.models import ReviewJobRow

        async with evaluation_engine.connect() as conn:
            await conn.execute(select(ReviewJobRow.review_job_id).limit(1))

    pipeline = None
    if review_on:
        assert llm_client is not None
        pipeline = ReviewPipeline(
            llm_client,
            model_version=settings.llm_model,
            prompt_version="v1",
            llm_max_context=settings.llm_max_context,
            max_tokens=settings.llm_max_tokens,
            verify_max_tokens=settings.llm_verify_max_tokens,
            truncation_retry_max_findings=settings.llm_truncation_retry_max_findings,
            max_review_batches=settings.review_max_batches,
            retriever=retriever,
            api_spec_retriever=api_spec_retriever,
            notion_link_store=notion_link_store,
            dependency_resolver=dependency_resolver,
            official_docs_workflow=official_docs_workflow,
        )

    comment_answer_pipeline = None
    if comment_on:
        assert llm_client is not None
        comment_answer_pipeline = CommentAnswerPipeline(
            llm_client, llm_max_context=settings.llm_max_context
        )

    kafka_producer = create_producer(settings)
    await kafka_producer.start()

    kafka_consumer = None
    if review_on:
        kafka_consumer = create_consumer(settings)
        await kafka_consumer.start()

    comment_answer_kafka_consumer = None
    if comment_on:
        comment_answer_kafka_consumer = create_comment_answer_consumer(settings)
        await comment_answer_kafka_consumer.start()

    review_feedback_kafka_consumer = None
    if evaluation_repository is not None:
        review_feedback_kafka_consumer = create_review_feedback_consumer(settings)
        await review_feedback_kafka_consumer.start()

    repo_index_kafka_consumer = None
    if rag_on:
        repo_index_kafka_consumer = create_repo_index_consumer(settings)
        await repo_index_kafka_consumer.start()

    # redis.asyncio.Redis의 실제 타입 스텁이 RedisLike보다 훨씬 넓어 구조적으로
    # 완전히 일치하지 않지만, set/get/delete를 문자열 인자로만 호출하므로 런타임에는 호환된다.
    dedup_store = create_dedup_store(settings, redis_client)  # type: ignore[arg-type]
    comment_answer_dedup_store = create_comment_answer_dedup_store(
        settings, redis_client  # type: ignore[arg-type]
    )

    shutdown_event = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    if kafka_consumer is not None:
        assert pipeline is not None
        event_producer = ReviewEventProducer(
            kafka_producer,
            completed_topic=settings.kafka_review_completed_topic,
            failed_topic=settings.kafka_review_failed_topic,
        )
        review_consumer = ReviewRequestConsumer(
            kafka_consumer,
            pipeline,
            event_producer,
            dedup_store,
            evaluation_repository=evaluation_repository,
        )
        tasks.append(
            asyncio.create_task(
                _run_consumer_forever(review_consumer, shutdown_event, name="review")
            )
        )
    if comment_answer_kafka_consumer is not None:
        assert comment_answer_pipeline is not None
        comment_answer_event_producer = CommentAnswerEventProducer(
            kafka_producer,
            completed_topic=settings.kafka_comment_answer_completed_topic,
            failed_topic=settings.kafka_comment_answer_failed_topic,
        )
        comment_answer_consumer = CommentAnswerConsumer(
            comment_answer_kafka_consumer,
            comment_answer_pipeline,
            comment_answer_event_producer,
            comment_answer_dedup_store,
        )
        tasks.append(
            asyncio.create_task(
                _run_consumer_forever(
                    comment_answer_consumer, shutdown_event, name="comment-answer"
                )
            )
        )
    if review_feedback_kafka_consumer is not None:
        from app.evaluation.feedback_consumer import ReviewFeedbackConsumer

        assert evaluation_repository is not None
        review_feedback_consumer = ReviewFeedbackConsumer(
            review_feedback_kafka_consumer, evaluation_repository
        )
        review_feedback_task = asyncio.create_task(
            _run_consumer_forever(
                review_feedback_consumer, shutdown_event, name="review-feedback"
            )
        )
        tasks.append(review_feedback_task)
    if repo_index_kafka_consumer is not None:
        from app.rag.index_consumer import RepoIndexConsumer

        repo_index_consumer = RepoIndexConsumer(
            repo_index_kafka_consumer, embedder, vector_store
        )
        repo_index_task = asyncio.create_task(
            _run_consumer_forever(repo_index_consumer, shutdown_event, name="repo-index")
        )
        tasks.append(repo_index_task)

    sandbox_probe_kafka_consumers: list[Any] = []
    sandbox_token_client = None
    if settings.sandbox_probe_consumer_enabled:
        from app.kafka.producer import SandboxProbeEventProducer
        from app.review.dedup import RedisDedupStore
        from app.sandbox_probe.consumer import SandboxProbeConsumer
        from app.sandbox_probe.docker import SubprocessDockerRunner
        from app.sandbox_probe.runner import SandboxJobRunner
        from app.sandbox_probe.token_client import GithubAppTokenClient

        sandbox_token_client = GithubAppTokenClient(
            settings.github_app_internal_url, settings.github_app_internal_secret
        )
        docker_runner = SubprocessDockerRunner()
        # 이전 프로세스가 배포 도중 죽으며 남긴 잡 리소스를 먼저 수거한다.
        await docker_runner.reap_orphans()
        sandbox_runner = SandboxJobRunner(
            docker_runner,
            sandbox_token_client,
            workdir=Path(settings.sandbox_probe_workdir),
            job_timeout_seconds=settings.sandbox_probe_job_timeout_seconds,
            min_free_disk_gb=settings.sandbox_probe_min_free_disk_gb,
            default_node_major=settings.sandbox_probe_default_node_major,
        )
        sandbox_probe_dedup = RedisDedupStore(
            redis_client,  # type: ignore[arg-type]
            key_prefix="ai-review:sandbox-probe-dedup:",
            ttl_seconds=settings.sandbox_probe_dedup_ttl_seconds,
        )
        sandbox_probe_producer = SandboxProbeEventProducer(
            kafka_producer,
            completed_topic=settings.kafka_sandbox_probe_completed_topic,
        )
        # 기존 컨슈머는 엄격히 순차 처리라, 동시성은 같은 그룹의 컨슈머를 여러 개
        # 띄워서 얻는다(토픽 파티션 수 이상으로 늘려도 이득이 없다).
        for index in range(settings.sandbox_probe_concurrency):
            sandbox_kafka_consumer = create_sandbox_probe_consumer(settings)
            await sandbox_kafka_consumer.start()
            sandbox_probe_kafka_consumers.append(sandbox_kafka_consumer)
            sandbox_consumer = SandboxProbeConsumer(
                sandbox_kafka_consumer,
                sandbox_runner,
                sandbox_probe_producer,
                sandbox_probe_dedup,
                redis_client,
                max_attempts=settings.sandbox_probe_max_attempts,
            )
            tasks.append(
                asyncio.create_task(
                    _run_consumer_forever(
                        sandbox_consumer, shutdown_event, name=f"sandbox-probe-{index}"
                    )
                )
            )

    try:
        yield
    finally:
        # 처리 중인 메시지가 있으면 커밋까지 마무리할 시간을 준다. 유예시간을
        # 넘기면 강제 취소하되, 락 해제는 각 consumer.handle()이 책임진다.
        shutdown_event.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks), timeout=settings.graceful_shutdown_seconds
            )
        except (TimeoutError, asyncio.CancelledError):
            pass
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if kafka_consumer is not None:
            await kafka_consumer.stop()
        if comment_answer_kafka_consumer is not None:
            await comment_answer_kafka_consumer.stop()
        if review_feedback_kafka_consumer is not None:
            await review_feedback_kafka_consumer.stop()
        if repo_index_kafka_consumer is not None:
            await repo_index_kafka_consumer.stop()
        for sandbox_kafka_consumer in sandbox_probe_kafka_consumers:
            await sandbox_kafka_consumer.stop()
        if sandbox_token_client is not None:
            await sandbox_token_client.aclose()
        await kafka_producer.stop()
        await redis_client.aclose()
        if llm_client is not None:
            await llm_client.aclose()
        if qdrant_client is not None:
            qdrant_client.close()
        if npm_registry_client is not None:
            await npm_registry_client.aclose()
        if maven_central_client is not None:
            await maven_central_client.aclose()
        if github_release_client is not None:
            await github_release_client.aclose()
        if evaluation_engine is not None:
            await evaluation_engine.dispose()
        if langfuse_client is not None:
            langfuse_client.shutdown()


settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

app.include_router(health_router)
