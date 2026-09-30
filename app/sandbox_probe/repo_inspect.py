import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import yaml

PackageManager = Literal["pnpm", "npm", "yarn"]

_ENV_EXAMPLE_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$")
_SOURCE_ENV_PATTERNS = (
    re.compile(r"""\.(?:get|getOrThrow)(?:<[^>]*>)?\(\s*['"`]([A-Z0-9_]+)['"`]"""),
    re.compile(r"""process\.env(?:\.|\[['"])([A-Z0-9_]+)"""),
)

_DATABASE = re.compile(
    r"^(DATABASE_|DB_|POSTGRES|MYSQL|REDIS|MONGO|PG(HOST|PORT|USER|PASSWORD|DATABASE)$)"
)
_GENERAL = re.compile(
    r"^(PORT|NODE_ENV|TZ|LOG_LEVEL|HOST)$|_PORT$|_TIMEOUT|_ENABLED$|_REJECT_"
)
_URL = re.compile(r"_URL$|_WEBHOOK|^WEBHOOK")

_GENERAL_DEFAULTS = {
    "PORT": "3000",
    "NODE_ENV": "test",
    "TZ": "UTC",
    "LOG_LEVEL": "info",
    "HOST": "0.0.0.0",
}

_COMPOSE_ALLOWED_FIELDS = ("image", "environment", "command", "healthcheck")


@dataclass(frozen=True)
class EnvClassification:
    database: frozenset[str]
    url: frozenset[str]
    general: frozenset[str]
    secret: frozenset[str]


@dataclass(frozen=True)
class Toolchain:
    package_manager: PackageManager
    node_major: str
    manager_version: str | None = None


def parse_env_example(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_EXAMPLE_LINE.match(line)
        if match is None:
            continue
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[match.group(1)] = value
    return values


def scan_source_env_names(source_texts: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for text in source_texts:
        for pattern in _SOURCE_ENV_PATTERNS:
            names.update(pattern.findall(text))
    return names


def classify_env(names: Iterable[str]) -> EnvClassification:
    database: set[str] = set()
    url: set[str] = set()
    general: set[str] = set()
    secret: set[str] = set()
    for name in names:
        if _DATABASE.search(name):
            database.add(name)
        # 일반 설정을 URL보다 먼저 본다: WEBHOOK_TIMEOUT_MS 같은 값에 URL을 넣으면 안 된다.
        elif _GENERAL.search(name):
            general.add(name)
        elif _URL.search(name):
            url.add(name)
        else:
            secret.add(name)
    return EnvClassification(
        frozenset(database), frozenset(url), frozenset(general), frozenset(secret)
    )


def rewrite_db_url(url: str, host: str, port: int) -> str:
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return url
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = f"{userinfo}@{host}:{port}" if userinfo else f"{host}:{port}"
    return urlunsplit(parts._replace(netloc=netloc))


def build_probe_env(
    classification: EnvClassification,
    example: Mapping[str, str],
    *,
    resolve_sidecar: Callable[[str], tuple[str, int]],
    mock_url: str,
    make_secret: Callable[[], str],
) -> dict[str, str]:
    env: dict[str, str] = {}

    for name in classification.database:
        value = example.get(name, "")
        sidecar_host, sidecar_port = resolve_sidecar(name)
        if name.endswith(("_URL", "_URI")) and "://" in value:
            env[name] = rewrite_db_url(value, sidecar_host, sidecar_port)
        elif name.endswith("_HOST"):
            env[name] = sidecar_host
        elif name.endswith("_PORT"):
            env[name] = str(sidecar_port)
        elif value:
            env[name] = value

    for name in classification.url:
        env[name] = mock_url

    for name in classification.general:
        value = example.get(name, "")
        if name == "NODE_ENV":
            env[name] = "test"
        elif name == "PORT":
            env[name] = value if value.isdigit() else _GENERAL_DEFAULTS["PORT"]
        elif value:
            env[name] = value
        elif name in _GENERAL_DEFAULTS:
            env[name] = _GENERAL_DEFAULTS[name]

    for name in classification.secret:
        env[name] = make_secret()

    return env


def _first_int(text: str) -> str | None:
    match = re.search(r"\d+", text)
    return match.group(0) if match else None


def detect_toolchain(
    package_json: str,
    lockfiles: Collection[str],
    ci_workflows: Iterable[str],
    *,
    node_version_files: Iterable[str] = (),
    default_node_major: str | None = None,
) -> Toolchain | None:
    try:
        pkg = json.loads(package_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(pkg, dict):
        return None

    manager: PackageManager | None = None
    manager_version: str | None = None
    declared = pkg.get("packageManager")
    if isinstance(declared, str):
        name, _, version = declared.partition("@")
        if name in ("pnpm", "npm", "yarn"):
            manager = name  # type: ignore[assignment]
            manager_version = version.split("+")[0] or None
    if manager is None:
        if "pnpm-lock.yaml" in lockfiles:
            manager = "pnpm"
        elif "yarn.lock" in lockfiles:
            manager = "yarn"
        elif "package-lock.json" in lockfiles:
            manager = "npm"
    if manager is None:
        return None

    node_major: str | None = None
    engines = pkg.get("engines")
    if isinstance(engines, dict) and isinstance(engines.get("node"), str):
        node_major = _first_int(engines["node"])
    if node_major is None:
        node_major = next(
            (major for text in node_version_files if (major := _first_int(text))), None
        )
    if node_major is None:
        for workflow in ci_workflows:
            match = re.search(r"node-version:\s*\[?\s*['\"]?(\d+)", workflow)
            if match:
                node_major = match.group(1)
                break
    if node_major is None:
        node_major = default_node_major
    if node_major is None:
        return None

    return Toolchain(manager, node_major, manager_version)


def _normalize_environment(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        env: dict[str, str] = {}
        for item in raw:
            key, _, value = str(item).partition("=")
            env[key] = value
        return env
    return {}


def extract_compose_services(compose_yaml: str) -> dict[str, dict[str, Any]]:
    # compose 파일은 PR이 수정할 수 있는 입력이라 그대로 실행하지 않고,
    # 안전한 필드만 추려 docker run argv를 직접 조립한다.
    try:
        data = yaml.safe_load(compose_yaml)
    except yaml.YAMLError:
        return {}
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for name, definition in services.items():
        if not isinstance(definition, dict) or not isinstance(definition.get("image"), str):
            continue
        kept = {k: definition[k] for k in _COMPOSE_ALLOWED_FIELDS if k in definition}
        if "environment" in kept:
            kept["environment"] = _normalize_environment(kept["environment"])
        result[str(name)] = kept
    return result
