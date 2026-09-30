import json

from app.sandbox_probe.repo_inspect import (
    Toolchain,
    build_probe_env,
    classify_env,
    detect_toolchain,
    extract_compose_services,
    parse_env_example,
    rewrite_db_url,
    scan_source_env_names,
)

_ENV_EXAMPLE = """
# comment
PORT=8080
NODE_ENV=production
DATABASE_URL="postgresql://app:secret@localhost:5432/app"
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/x
JWT_SECRET=change-me
export SESSION_TTL_ENABLED=true
"""

_SOURCE = """
const secret = this.configService.get<string>('JWT_SECRET');
const url = configService.getOrThrow("DISCORD_WEBHOOK_URL");
const port = process.env.PORT;
const key = process.env['ENCRYPTION_KEY'];
"""


def test_parse_env_example_strips_quotes_comments_and_export() -> None:
    values = parse_env_example(_ENV_EXAMPLE)

    assert values["DATABASE_URL"] == "postgresql://app:secret@localhost:5432/app"
    assert values["SESSION_TTL_ENABLED"] == "true"
    assert "comment" not in values


def test_scan_source_env_names_matches_method_calls_and_process_env() -> None:
    names = scan_source_env_names([_SOURCE])

    assert names == {"JWT_SECRET", "DISCORD_WEBHOOK_URL", "PORT", "ENCRYPTION_KEY"}


def test_classify_env_assigns_each_name_to_one_bucket() -> None:
    result = classify_env(
        [
            "DATABASE_URL",
            "POSTGRES_PASSWORD",
            "REDIS_HOST",
            "DISCORD_WEBHOOK_URL",
            "PORT",
            "NODE_ENV",
            "API_TIMEOUT_MS",
            "WEBHOOK_TIMEOUT_MS",
            "FEATURE_X_ENABLED",
            "JWT_SECRET",
        ]
    )

    assert result.database == {"DATABASE_URL", "POSTGRES_PASSWORD", "REDIS_HOST"}
    assert result.url == {"DISCORD_WEBHOOK_URL"}
    assert result.general == {
        "PORT",
        "NODE_ENV",
        "API_TIMEOUT_MS",
        "WEBHOOK_TIMEOUT_MS",
        "FEATURE_X_ENABLED",
    }
    assert result.secret == {"JWT_SECRET"}


def test_rewrite_db_url_points_at_sidecar_and_keeps_credentials_and_db() -> None:
    assert (
        rewrite_db_url("postgresql://app:secret@localhost:5432/app?ssl=off", "db", 5433)
        == "postgresql://app:secret@db:5433/app?ssl=off"
    )


def test_rewrite_db_url_handles_missing_credentials_and_non_urls() -> None:
    assert rewrite_db_url("redis://localhost:6379", "cache", 6380) == "redis://cache:6380"
    assert rewrite_db_url("not-a-url", "db", 1) == "not-a-url"


def _probe_env(example_text: str, source: str = "") -> dict[str, str]:
    example = parse_env_example(example_text)
    names = set(example) | scan_source_env_names([source])
    return build_probe_env(
        classify_env(names),
        example,
        resolve_sidecar=lambda name: ("cache", 6380) if name.startswith("REDIS") else ("db", 5433),
        mock_url="http://mock:9000",
        make_secret=lambda: "s" * 64,
    )


def test_build_probe_env_applies_each_bucket_policy() -> None:
    env = _probe_env(_ENV_EXAMPLE, _SOURCE)

    assert env["DATABASE_URL"] == "postgresql://app:secret@db:5433/app"
    assert env["DISCORD_WEBHOOK_URL"] == "http://mock:9000"
    assert env["PORT"] == "8080"
    assert env["NODE_ENV"] == "test"
    assert env["SESSION_TTL_ENABLED"] == "true"
    assert env["JWT_SECRET"] == "s" * 64
    assert env["ENCRYPTION_KEY"] == "s" * 64


def test_build_probe_env_never_uses_random_value_for_port() -> None:
    env = _probe_env("PORT=not-a-number\n")

    assert env["PORT"] == "3000"


def test_build_probe_env_defaults_port_when_only_found_in_source() -> None:
    env = _probe_env("", "const p = process.env.PORT;")

    assert env["PORT"] == "3000"


def test_build_probe_env_does_not_invent_values_for_unknown_general_names() -> None:
    env = _probe_env("", "process.env.CACHE_TIMEOUT_MS")

    assert "CACHE_TIMEOUT_MS" not in env


def test_build_probe_env_uses_sidecar_for_host_and_port_style_database_vars() -> None:
    env = _probe_env("REDIS_HOST=localhost\nREDIS_PORT=6379\nPOSTGRES_USER=app\n")

    assert env["REDIS_HOST"] == "cache"
    assert env["REDIS_PORT"] == "6380"
    assert env["POSTGRES_USER"] == "app"


def _package_json(**fields: object) -> str:
    return json.dumps({"name": "x", **fields})


def test_detect_toolchain_prefers_package_manager_field_and_engines() -> None:
    result = detect_toolchain(
        _package_json(packageManager="pnpm@9.1.0+sha512.abc", engines={"node": ">=20.11"}),
        {"package-lock.json"},
        [],
    )

    assert result == Toolchain("pnpm", "20", "9.1.0")


def test_detect_toolchain_falls_back_to_lockfile_and_ci_workflow() -> None:
    workflow = "steps:\n  - uses: actions/setup-node@v4\n    with:\n      node-version: '22.x'\n"

    result = detect_toolchain(_package_json(), {"yarn.lock"}, [workflow])

    assert result == Toolchain("yarn", "22", None)


def test_detect_toolchain_reads_nvmrc_before_ci_and_default() -> None:
    result = detect_toolchain(
        _package_json(),
        {"pnpm-lock.yaml"},
        ["node-version: 18"],
        node_version_files=["v20.11.0\n"],
        default_node_major="24",
    )

    assert result == Toolchain("pnpm", "20", None)


def test_detect_toolchain_uses_configured_default_node_as_last_resort() -> None:
    result = detect_toolchain(
        _package_json(), {"pnpm-lock.yaml"}, [], default_node_major="24"
    )

    assert result == Toolchain("pnpm", "24", None)


def test_detect_toolchain_lockfile_priority_is_pnpm_yarn_npm() -> None:
    engines = {"node": "20"}

    assert detect_toolchain(
        _package_json(engines=engines), {"package-lock.json", "pnpm-lock.yaml"}, []
    ) == Toolchain("pnpm", "20", None)
    assert detect_toolchain(
        _package_json(engines=engines), {"package-lock.json"}, []
    ) == Toolchain("npm", "20", None)


def test_detect_toolchain_returns_none_when_nothing_is_determinable() -> None:
    assert detect_toolchain(_package_json(engines={"node": "20"}), set(), []) is None
    assert detect_toolchain(_package_json(), {"pnpm-lock.yaml"}, ["name: ci"]) is None
    assert detect_toolchain("{not json", {"pnpm-lock.yaml"}, []) is None
    assert detect_toolchain("[]", {"pnpm-lock.yaml"}, []) is None


_COMPOSE = """
services:
  db:
    image: postgres:16
    ports: ["5432:5432"]
    volumes: ["./data:/var/lib/postgresql/data"]
    privileged: true
    network_mode: host
    cap_add: [SYS_ADMIN]
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: secret
    command: ["postgres", "-c", "fsync=off"]
    healthcheck:
      test: ["CMD", "pg_isready"]
  redis:
    image: redis:7
    environment:
      - REDIS_ARGS=--save ""
  app:
    build: .
    ports: ["3000:3000"]
"""


def test_extract_compose_services_keeps_only_allowlisted_fields() -> None:
    services = extract_compose_services(_COMPOSE)

    assert set(services["db"]) == {"image", "environment", "command", "healthcheck"}
    assert services["db"]["image"] == "postgres:16"
    assert services["db"]["environment"] == {
        "POSTGRES_USER": "app",
        "POSTGRES_PASSWORD": "secret",
    }


def test_extract_compose_services_normalizes_list_environment() -> None:
    services = extract_compose_services(_COMPOSE)

    assert services["redis"]["environment"] == {"REDIS_ARGS": '--save ""'}


def test_extract_compose_services_drops_services_without_image() -> None:
    assert "app" not in extract_compose_services(_COMPOSE)


def test_extract_compose_services_tolerates_invalid_input() -> None:
    assert extract_compose_services("{{{") == {}
    assert extract_compose_services("just a string") == {}
    assert extract_compose_services("services: 3") == {}
