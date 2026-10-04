from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Dovi AI Server"
    debug: bool = False
    log_level: str = "INFO"

    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_review_request_topic: str = "pr.review.requested"
    kafka_review_completed_topic: str = "pr.review.completed"
    kafka_review_failed_topic: str = "pr.review.failed"
    kafka_comment_answer_request_topic: str = "pr.comment.answer.requested"
    kafka_comment_answer_completed_topic: str = "pr.comment.answer.completed"
    kafka_comment_answer_failed_topic: str = "pr.comment.answer.failed"
    # 기본 False: 테스트/CI에서 TestClient가 앱을 기동해도 실제 Kafka/LLM에
    # 연결을 시도하지 않는다. 운영 배포 시 .env에서 명시적으로 true로 켠다.
    kafka_consumer_enabled: bool = False
    # kafka_consumer_enabled가 true일 때 컨슈머별로 끌 수 있다. LLM/GPU가 없는 배포
    # (샌드박스 프로브 VM)가 review/comment-answer 컨슈머 그룹에 합류해 실제
    # 리뷰를 타임아웃시키는 사고를 막기 위한 스위치라, 기존 배포와의 하위호환을
    # 위해 기본값은 true다.
    review_consumer_enabled: bool = True
    comment_answer_consumer_enabled: bool = True
    # 같은 PR에 더 새로운 head의 리뷰 요청이 이미 큐에 있으면 오래된 요청은 LLM을 돌리지
    # 않고 건너뛴다. 도착 순서를 기록하는 수신 전용 컨슈머를 함께 띄운다.
    review_skip_superseded_enabled: bool = True
    kafka_sandbox_probe_request_topic: str = "pr.sandbox.probe.requested"
    kafka_sandbox_probe_completed_topic: str = "pr.sandbox.probe.completed"
    # 기본 False: 샌드박스 프로브는 신뢰할 수 없는 PR 코드를 실제로 실행하므로 Docker가 있는
    # 전용 VM에서만 켠다(이슈 #97). 그 VM은 review/comment-answer 컨슈머를 끄고 배포한다.
    sandbox_probe_consumer_enabled: bool = False
    sandbox_probe_concurrency: int = 4
    sandbox_probe_job_timeout_seconds: float = 900.0
    sandbox_probe_max_poll_interval_ms: int = 1_200_000
    sandbox_probe_max_attempts: int = 2
    sandbox_probe_dedup_ttl_seconds: int = 1800
    sandbox_probe_workdir: str = "/var/lib/dovi-sandbox"
    sandbox_probe_min_free_disk_gb: float = 5.0
    # 레포에서 Node 버전을 알아낼 수 없을 때 쓰는 기본 major 버전.
    sandbox_probe_default_node_major: str = "24"
    # github-app 내부 API(scoped installation token 발급). 키는 github-app에만 둔다.
    github_app_internal_url: str = ""
    github_app_internal_secret: str = ""
    # 배포로 종료 신호를 받았을 때, 처리 중인 리뷰를 강제 취소하기 전에 기다려주는
    # 최대 시간. llm_timeout_seconds보다 넉넉해야 정상 완료를 강제 취소로 놓치지 않는다.
    graceful_shutdown_seconds: float = 260.0

    llm_profile: str = "dual_gpu_32gb"
    llm_base_url: str = "http://localhost:8001/v1"
    llm_model: str = "qwen2.5-coder-32b-instruct-q4_k_m.gguf"
    llm_max_context: int = 8192
    llm_gpu_layers: int = -1
    llm_timeout_seconds: float = 120.0
    # ReviewPipeline의 기존 기본값과 동일 — 설정값으로 분리해 배포 환경별로
    # 조정 가능하게 한다(이슈 #98).
    llm_max_tokens: int = 1500
    llm_verify_max_tokens: int = 800
    # 출력이 잘려 부분 복구도 실패했을 때, 짧게 다시 요청하며 허용하는 finding
    # 최대 개수(이슈 #99).
    llm_truncation_retry_max_findings: int = 5
    # 큰 PR을 나눠 리뷰할 때 LLM을 호출하는 최대 배치 수(이슈 #108). 상한을 넘는
    # 파일은 리뷰하지 못하고 summary에 안내된다. 배치당 LLM 호출 1회라 소요
    # 시간이 비례해 늘어난다.
    review_max_batches: int = 12
    # 프롬프트의 diff 줄 앞에 새 파일 기준 줄 번호(R<n>)를 붙여, 모델이 `@@` 헤더에서
    # 줄 번호를 직접 계산하다 틀리는 것을 줄인다(이슈 #122). 기본 False: 로그
    # `finding lines checked`로 기준 수치를 모은 뒤 .env에서 켜서 전후를 비교한다.
    review_diff_line_numbers_enabled: bool = False
    # 비우면 프롬프트 내용 해시(sha-xxxxxxxx)를 평가 DB의 prompt_version으로 기록한다.
    prompt_version: str = ""

    redis_url: str = "redis://localhost:6379"
    # headSha는 불변이므로 TTL을 길게 잡아도 무방하다 (기본 24시간)
    review_dedup_ttl_seconds: int = 86400
    # 코멘트 Q&A는 리뷰보다 훨씬 가벼운 단발성 작업이라 TTL을 짧게 잡는다 (기본 1시간)
    comment_answer_dedup_ttl_seconds: int = 3600

    # 기본 False: Qdrant 인덱스가 아직 없는 레포/환경에서도 앱이 정상 기동해야 한다.
    # scripts/index_repo.py로 인덱싱을 마친 뒤 .env에서 명시적으로 켠다.
    rag_enabled: bool = False
    qdrant_url: str = "http://localhost:6333"
    rag_collection_name: str = "dovi_code_chunks"
    embedding_model: str = "nomic-ai/CodeRankEmbed"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    # rag_enabled와 함께 게이팅한다 — 인덱스를 조회할 일이 없으면 최신 상태로
    # 유지할 필요도 없다. push 시점의 증분 재인덱싱 요청(github-app 발행)을 소비한다.
    kafka_repo_index_request_topic: str = "repo.index.requested"

    # 기본 False: Notion 연동 설정이 없는 레포/환경에서도 앱이 정상 기동해야 한다.
    # DOVI.md에 Notion API 명세 DB 링크가 등록된 뒤 .env에서 명시적으로 켠다.
    notion_sync_enabled: bool = False
    notion_api_token: str = ""
    api_spec_collection_name: str = "dovi_api_spec_chunks"

    # 기본 False: registry 조회가 필요 없는 레포/환경에서도 앱이 정상 기동해야 한다.
    dependency_check_enabled: bool = False

    # 기본 False: GitHub API 호출이 필요 없는 레포/환경에서도 앱이 정상
    # 기동해야 한다. GITHUB_TOKEN 없이도 동작은 하지만(미인증 60회/시간),
    # 프로덕션에서는 GITHUB_TOKEN도 함께 설정하는 걸 권장한다.
    official_docs_workflow_enabled: bool = False
    github_token: str = ""

    # 기본 False: PostgreSQL이 없는 레포/환경에서도 앱이 정상 기동해야 한다.
    # Alembic 마이그레이션(alembic/) 적용 후 .env에서 명시적으로 켠다.
    evaluation_enabled: bool = False
    database_url: str = "postgresql+asyncpg://dovi:dovi@localhost:5432/dovi"
    kafka_review_feedback_topic: str = "pr.comment.reflected"

    # 기본 False: 자체 호스팅 Langfuse 인스턴스가 없는 레포/환경에서도 앱이 정상
    # 기동해야 한다. Langfuse 서버(web+worker+자체 DB 스택)를 별도로 띄운 뒤
    # .env에서 명시적으로 켠다 — 이 앱 코드가 그 서버를 배포하지는 않는다.
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
