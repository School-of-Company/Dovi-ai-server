from app.sandbox_probe.repo_inspect import extract_compose_services
from app.sandbox_probe.sidecars import (
    healthcheck_argv,
    image_family,
    plan_sidecars,
    sidecar_resolver,
)

_COMPOSE = """
services:
  db:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: expoform
    healthcheck:
      test: ['CMD-SHELL', 'pg_isready -U app -d expoform']
  cache:
    image: redis:7
  worker:
    image: ghcr.io/acme/worker:1
"""


def test_image_family_recognises_database_images() -> None:
    assert image_family("postgres:17-alpine") == "postgres"
    assert image_family("docker.io/library/postgres") == "postgres"
    assert image_family("redis:7") == "redis"
    assert image_family("mariadb:11") == "mysql"
    assert image_family("mongo:8") == "mongo"
    assert image_family("ghcr.io/acme/postgres-exporter:1") is None
    assert image_family("nginx") is None


def test_healthcheck_argv_supports_shell_and_exec_forms() -> None:
    assert healthcheck_argv({"test": ["CMD-SHELL", "pg_isready -U app"]}) == (
        "sh", "-c", "pg_isready -U app",
    )  # fmt: skip
    assert healthcheck_argv({"test": ["CMD", "redis-cli", "ping"]}) == ("redis-cli", "ping")
    assert healthcheck_argv({"test": "curl -f localhost"}) == ("sh", "-c", "curl -f localhost")
    assert healthcheck_argv({"test": ["NONE"]}) is None
    assert healthcheck_argv(None) is None


def test_plan_sidecars_keeps_only_database_services_from_compose() -> None:
    sidecars = plan_sidecars({}, extract_compose_services(_COMPOSE))

    assert [s.alias for s in sidecars] == ["db", "cache"]
    db, cache = sidecars
    assert db.port == 5432
    assert db.env["POSTGRES_DB"] == "expoform"
    assert db.ready_argv == ("sh", "-c", "pg_isready -U app -d expoform")
    assert cache.port == 6379
    assert cache.ready_argv == ("redis-cli", "ping")


def test_plan_sidecars_falls_back_to_postgres_derived_from_database_url() -> None:
    sidecars = plan_sidecars({"DATABASE_URL": "postgres://u:p@localhost:5432/mydb"}, {})

    assert len(sidecars) == 1
    assert sidecars[0].alias == "db"
    assert sidecars[0].env == {
        "POSTGRES_USER": "u",
        "POSTGRES_PASSWORD": "p",
        "POSTGRES_DB": "mydb",
    }


def test_plan_sidecars_is_empty_without_any_database_hint() -> None:
    assert plan_sidecars({"DATABASE_URL": "mysql://u:p@h/db"}, {}) == []
    assert plan_sidecars({}, {}) == []


def test_resolver_maps_variables_to_the_matching_sidecar() -> None:
    resolve = sidecar_resolver(plan_sidecars({}, extract_compose_services(_COMPOSE)))

    assert resolve("DATABASE_URL") == ("db", 5432)
    assert resolve("POSTGRES_HOST") == ("db", 5432)
    assert resolve("REDIS_HOST") == ("cache", 6379)


def test_resolver_falls_back_to_default_when_no_sidecar_matches() -> None:
    assert sidecar_resolver([])("DATABASE_URL") == ("db", 5432)
