import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

_FAMILIES: dict[str, tuple[str, int, str, tuple[str, ...] | None]] = {
    # family: (image name pattern, default port, data dir, default readiness argv)
    "postgres": (r"^(?:[\w.-]+/)*postgres(?::|$)", 5432, "/var/lib/postgresql",
                 ("pg_isready", "-h", "127.0.0.1")),
    "redis": (r"^(?:[\w.-]+/)*(?:redis|valkey)(?::|$)", 6379, "/data",
              ("redis-cli", "ping")),
    "mysql": (r"^(?:[\w.-]+/)*(?:mysql|mariadb)(?::|$)", 3306, "/var/lib/mysql",
              ("mysqladmin", "ping", "-h", "127.0.0.1")),
    "mongo": (r"^(?:[\w.-]+/)*mongo(?::|$)", 27017, "/data/db", None),
}  # fmt: skip

_DEFAULT_POSTGRES_IMAGE = "postgres:17-alpine"


@dataclass(frozen=True)
class Sidecar:
    alias: str
    family: str
    image: str
    port: int
    data_dir: str
    env: Mapping[str, str]
    command: tuple[str, ...] = ()
    ready_argv: tuple[str, ...] | None = None


def image_family(image: str) -> str | None:
    for family, (pattern, *_rest) in _FAMILIES.items():
        if re.match(pattern, image):
            return family
    return None


def healthcheck_argv(healthcheck: Any) -> tuple[str, ...] | None:
    test = healthcheck.get("test") if isinstance(healthcheck, dict) else None
    if isinstance(test, str):
        return ("sh", "-c", test)
    if isinstance(test, list) and test:
        kind, *rest = (str(item) for item in test)
        if kind == "CMD-SHELL" and rest:
            return ("sh", "-c", rest[0])
        if kind == "CMD" and rest:
            return tuple(rest)
    return None


def _command_argv(command: Any) -> tuple[str, ...]:
    if isinstance(command, str):
        return tuple(shlex.split(command))
    if isinstance(command, list):
        return tuple(str(part) for part in command)
    return ()


def plan_sidecars(
    example_env: Mapping[str, str], compose_services: Mapping[str, Mapping[str, Any]]
) -> list[Sidecar]:
    sidecars: list[Sidecar] = []
    for alias, service in compose_services.items():
        family = image_family(service["image"])
        if family is None:
            continue
        _, port, data_dir, default_ready = _FAMILIES[family]
        sidecars.append(
            Sidecar(
                alias=alias,
                family=family,
                image=service["image"],
                port=port,
                data_dir=data_dir,
                env=dict(service.get("environment", {})),
                command=_command_argv(service.get("command")),
                ready_argv=healthcheck_argv(service.get("healthcheck")) or default_ready,
            )
        )
    if sidecars:
        return sidecars

    # compose에 DB가 없으면 예제 DATABASE_URL이 postgres일 때만 기본 사이드카를 만든다.
    parts = urlsplit(example_env.get("DATABASE_URL", ""))
    if parts.scheme in ("postgres", "postgresql"):
        _, port, data_dir, ready = _FAMILIES["postgres"]
        env = {
            "POSTGRES_USER": parts.username or "postgres",
            "POSTGRES_PASSWORD": parts.password or "postgres",
            "POSTGRES_DB": parts.path.lstrip("/") or "postgres",
        }
        return [
            Sidecar("db", "postgres", _DEFAULT_POSTGRES_IMAGE, port, data_dir, env,
                    ready_argv=ready)
        ]  # fmt: skip
    return []


def sidecar_resolver(sidecars: list[Sidecar]) -> Callable[[str], tuple[str, int]]:
    def resolve(name: str) -> tuple[str, int]:
        wanted = next(
            (
                family
                for prefix, family in (
                    ("REDIS", "redis"),
                    ("MYSQL", "mysql"),
                    ("MONGO", "mongo"),
                )
                if name.startswith(prefix)
            ),
            None,
        )
        for sidecar in sidecars:
            if wanted is None and sidecar.family != "redis" or sidecar.family == wanted:
                return sidecar.alias, sidecar.port
        return "db", 5432

    return resolve
