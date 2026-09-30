import asyncio
import json
import logging
import os
import re
import secrets
import shlex
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.sandbox_probe import checkout as checkout_module
from app.sandbox_probe.docker import ContainerSpec, DockerRunner, Mount
from app.sandbox_probe.probes import init_order_outcome, run_lifecycle_probe
from app.sandbox_probe.repo_inspect import (
    Toolchain,
    build_probe_env,
    classify_env,
    detect_toolchain,
    extract_compose_services,
    parse_env_example,
    scan_source_env_names,
)
from app.sandbox_probe.schema import (
    ProbeStatus,
    SandboxProbeCompletedEvent,
    SandboxProbeRequestedEvent,
    cap_completed_event,
)
from app.sandbox_probe.sidecars import Sidecar, plan_sidecars, sidecar_resolver
from app.sandbox_probe.verdict import StageResult, aggregate

logger = logging.getLogger(__name__)

_CONTAINERS_DIR = Path(__file__).parent / "containers"
_PROXY_HOSTS = ("registry.npmjs.org", "registry.yarnpkg.com")
_PROXY_PORT = 3128
_MOCK_PORT = 9000
_LOCKFILES = ("pnpm-lock.yaml", "yarn.lock", "package-lock.json")
_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
_MAX_SOURCE_FILES = 500
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_SIDECAR_CAPS = ("CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER")


class TokenSource(Protocol):
    async def fetch_token(self, installation_id: int, repository_id: int) -> str: ...


CheckoutFn = Callable[..., Awaitable[None]]


class _Inconclusive(Exception):
    pass


@dataclass(frozen=True)
class RepoFacts:
    package_json: str
    lockfiles: list[str]
    workflows: list[str]
    node_version_files: list[str]
    env_example: str
    compose: str
    sources: list[str]


def _read(path: Path, root: Path) -> str:
    # 대상 레포는 신뢰할 수 없는 입력이라, 심볼릭 링크로 워커 호스트의 파일(.env 등)을
    # 읽어 컨테이너 env나 결과 코멘트로 흘려보내지 못하게 레포 밖 경로는 읽지 않는다.
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            return ""
        return path.read_text(errors="replace")
    except OSError:
        return ""


def read_repo_facts(repo: Path) -> RepoFacts:
    sources: list[str] = []
    total = 0
    for path in sorted((repo / "src").rglob("*.ts")) if (repo / "src").is_dir() else []:
        if len(sources) >= _MAX_SOURCE_FILES or total >= _MAX_SOURCE_BYTES:
            break
        text = _read(path, repo)
        if not text:
            continue
        total += len(text)
        sources.append(text)
    workflows_dir = repo / ".github" / "workflows"
    workflows = (
        [_read(p, repo) for p in sorted(workflows_dir.glob("*.y*ml"))]
        if workflows_dir.is_dir()
        else []
    )
    compose = next((_read(repo / n, repo) for n in _COMPOSE_FILES if (repo / n).is_file()), "")
    return RepoFacts(
        package_json=_read(repo / "package.json", repo),
        lockfiles=[n for n in _LOCKFILES if (repo / n).is_file()],
        workflows=workflows,
        node_version_files=[
            _read(repo / n, repo) for n in (".nvmrc", ".node-version") if (repo / n).is_file()
        ],
        env_example=_read(repo / ".env.example", repo),
        compose=compose,
        sources=sources,
    )


_SAFE_VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[\w.]+)?")


def install_command(toolchain: Toolchain) -> list[str]:
    if toolchain.package_manager == "npm":
        return ["sh", "-c", "npm ci"]
    # Node 25+ 이미지에는 corepack이 없어 npm으로 패키지 매니저를 받는다. 버전 문자열은
    # PR이 바꿀 수 있는 입력이라 셸에 넣기 전에 형식을 검증한다.
    manager = toolchain.package_manager
    version = toolchain.manager_version
    pinned = version if version and _SAFE_VERSION.fullmatch(version) else "latest"
    script = (
        f"npm install --global --prefix /tmp/tool {manager}@{pinned} && "
        f"PATH=/tmp/tool/bin:$PATH {manager} install --frozen-lockfile"
    )
    return ["sh", "-c", script]


def start_command(package_json: str) -> list[str]:
    try:
        script = json.loads(package_json).get("scripts", {}).get("start:prod", "")
    except (ValueError, AttributeError):
        script = ""
    match = re.fullmatch(r"node\s+(\S.*)", script if isinstance(script, str) else "")
    return ["node", *shlex.split(match.group(1))] if match else ["node", "dist/main"]


def build_script(package_json: str) -> str | None:
    try:
        script = json.loads(package_json).get("scripts", {}).get("build")
    except (ValueError, AttributeError):
        return None
    return script if isinstance(script, str) and script.strip() else None


def _tail(text: str, limit: int = 2000) -> str:
    return text[-limit:]


class SandboxJobRunner:
    def __init__(
        self,
        docker: DockerRunner,
        token_source: TokenSource,
        *,
        workdir: Path,
        job_timeout_seconds: float = 900,
        min_free_disk_gb: float = 5,
        container_user: str | None = None,
        default_node_major: str | None = "24",
        python_image: str = "python:3.13-slim",
        node_image: Callable[[str], str] = lambda major: f"node:{major}-slim",
        checkout_fn: CheckoutFn = checkout_module.checkout,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._docker = docker
        self._token_source = token_source
        self._workdir = workdir
        self._job_timeout = job_timeout_seconds
        self._min_free_bytes = int(min_free_disk_gb * 1024**3)
        self._user = container_user or f"{os.getuid()}:{os.getgid()}"
        self._default_node_major = default_node_major
        self._python_image = python_image
        self._node_image = node_image
        self._checkout = checkout_fn
        self._sleep = sleep

    async def run(self, event: SandboxProbeRequestedEvent) -> SandboxProbeCompletedEvent:
        job_id = re.sub(r"[^a-zA-Z0-9]", "-", event.review_job_id)[-40:].strip("-")
        job_dir = self._workdir / job_id
        try:
            async with asyncio.timeout(self._job_timeout):
                status, evidence, findings = await self._run_stages(event, job_id, job_dir)
        except _Inconclusive as exc:
            status, evidence, findings = "inconclusive", str(exc), ()
        except TimeoutError:
            status, evidence, findings = "inconclusive", "잡 제한 시간을 초과했습니다.", ()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("sandbox probe job crashed reviewJobId=%s", event.review_job_id)
            status, evidence, findings = "inconclusive", f"내부 오류: {type(exc).__name__}", ()
        finally:
            await self._docker.cleanup_job(job_id)
            shutil.rmtree(job_dir, ignore_errors=True)

        return cap_completed_event(
            SandboxProbeCompletedEvent(
                review_job_id=event.review_job_id,
                repository_id=event.repository_id,
                pr_number=event.pr_number,
                head_sha=event.head_sha,
                status=status,
                evidence=evidence,
                findings=list(findings),
            )
        )

    async def _run_stages(
        self, event: SandboxProbeRequestedEvent, job_id: str, job_dir: Path
    ) -> tuple[ProbeStatus, str, tuple[Any, ...]]:
        self._workdir.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self._workdir).free < self._min_free_bytes:
            raise _Inconclusive("샌드박스 VM의 디스크 여유가 부족합니다.")

        repo = job_dir / "repo"
        token = await self._token_source.fetch_token(event.installation_id, event.repository_id)
        await self._checkout(event.repo_full_name, event.head_sha, token, repo)
        del token

        facts = read_repo_facts(repo)
        toolchain = detect_toolchain(
            facts.package_json,
            facts.lockfiles,
            facts.workflows,
            node_version_files=facts.node_version_files,
            default_node_major=self._default_node_major,
        )
        if toolchain is None:
            raise _Inconclusive("패키지 매니저 또는 Node 버전을 결정할 수 없습니다.")
        build_cmd = build_script(facts.package_json)
        if build_cmd is None:
            raise _Inconclusive("package.json에 build 스크립트가 없어 검증하지 않았습니다.")

        example = parse_env_example(facts.env_example)
        sidecars = plan_sidecars(example, extract_compose_services(facts.compose))
        classification = classify_env(set(example) | scan_source_env_names(facts.sources))
        probe_env = build_probe_env(
            classification,
            example,
            resolve_sidecar=sidecar_resolver(sidecars),
            mock_url=f"http://mock:{_MOCK_PORT}",
            make_secret=lambda: secrets.token_hex(32),
        )
        probe_env.setdefault("PORT", "3000")
        probe_env.setdefault("NODE_ENV", "test")

        network = f"sbx-{job_id}"
        egress_network = f"sbx-{job_id}-egress"
        await self._docker.create_network(network, job_id, internal=True)
        await self._docker.create_network(egress_network, job_id, internal=False)

        node_image = self._node_image(toolchain.node_major)
        work_mount = Mount(str(repo), "/work", read_only=False)

        install = await self._install(
            job_id, network, egress_network, node_image, work_mount, toolchain
        )
        if not install.ok:
            return aggregate(install, None, None, None).status, install.evidence, ()

        build = await self._docker.run(
            self._node_spec(
                f"sbx-{job_id}-build", node_image, network, work_mount,
                env={"PATH": "/work/node_modules/.bin:/usr/local/bin:/usr/bin:/bin"},
                command=("sh", "-c", build_cmd),
            ),
            job_id,
            timeout=300,
        )  # fmt: skip
        if build.timed_out:
            raise _Inconclusive("빌드가 제한 시간을 초과했습니다.")
        build_stage = StageResult(build.exit_code == 0, _tail(build.output))

        init_outcome = None
        lifecycle_outcome = None
        if build_stage.ok:
            ro_mount = Mount(str(repo), "/work", read_only=True)
            init_result = await self._docker.run(
                self._node_spec(
                    f"sbx-{job_id}-init", node_image, network, ro_mount,
                    env=probe_env,
                    command=("node", "/opt/init_order.mjs", "dist"),
                    extra_mounts=(_script_mount("init_order.mjs"),),
                ),
                job_id,
                timeout=180,
            )  # fmt: skip
            init_outcome = init_order_outcome(init_result)

            mock_name = f"sbx-{job_id}-mock"
            await self._start_mock(job_id, network, mock_name)
            await self._start_sidecars(job_id, network, sidecars)
            app_spec = self._node_spec(
                f"sbx-{job_id}-app", node_image, network, ro_mount,
                env=probe_env,
                command=tuple(start_command(facts.package_json)),
                aliases=("app",),
            )  # fmt: skip
            lifecycle_outcome = await run_lifecycle_probe(
                self._docker,
                job_id,
                app_spec,
                mock_name,
                int(probe_env["PORT"]) if probe_env["PORT"].isdigit() else 3000,
                sleep=self._sleep,
            )

        result = aggregate(install, build_stage, init_outcome, lifecycle_outcome)
        return result.status, result.evidence, result.findings

    def _node_spec(
        self,
        name: str,
        image: str,
        network: str,
        mount: Mount,
        *,
        env: dict[str, str],
        command: tuple[str, ...],
        aliases: tuple[str, ...] = (),
        extra_mounts: tuple[Mount, ...] = (),
        extra_networks: tuple[str, ...] = (),
        tmpfs: tuple[str, ...] = ("/tmp",),
    ) -> ContainerSpec:
        return ContainerSpec(
            name=name,
            image=image,
            network=network,
            command=command,
            env={"HOME": "/tmp/home", **env},
            mounts=(mount, *extra_mounts),
            aliases=aliases,
            extra_networks=extra_networks,
            tmpfs=tmpfs,
            workdir="/work",
            user=self._user,
            memory="2g",
            cpus="1",
        )

    async def _install(
        self,
        job_id: str,
        network: str,
        egress_network: str,
        node_image: str,
        work_mount: Mount,
        toolchain: Toolchain,
    ) -> StageResult:
        proxy_name = f"sbx-{job_id}-proxy"
        proxy_spec = ContainerSpec(
            name=proxy_name,
            image=self._python_image,
            network=egress_network,
            extra_networks=(network,),
            aliases=("egress-proxy",),
            command=("python", "/opt/egress_proxy.py", str(_PROXY_PORT), *_PROXY_HOSTS),
            mounts=(_script_mount("egress_proxy.py"),),
            user=self._user,
            memory="128m",
            cpus="0.5",
        )
        await self._docker.start(proxy_spec, job_id)
        await self._wait_ready(
            proxy_name,
            _connect_check(_PROXY_PORT),
        )
        proxy_url = f"http://egress-proxy:{_PROXY_PORT}"
        env = {
            "HOME": "/tmp/home",
            "COREPACK_HOME": "/tmp/corepack",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "NODE_USE_ENV_PROXY": "1",
            "HTTPS_PROXY": proxy_url,
            "HTTP_PROXY": proxy_url,
            "https_proxy": proxy_url,
            "http_proxy": proxy_url,
        }
        result = await self._docker.run(
            self._node_spec(
                f"sbx-{job_id}-install", node_image, network, work_mount,
                env=env, command=tuple(install_command(toolchain)),
                # corepack이 내려받은 패키지 매니저 바이너리를 /tmp에서 실행해야 한다.
                tmpfs=("/tmp:rw,exec,nosuid,size=1g",),
            ),
            job_id,
            timeout=420,
        )  # fmt: skip
        await self._docker.remove(proxy_name)
        if result.timed_out:
            return StageResult(False, "의존성 설치가 제한 시간을 초과했습니다.")
        return StageResult(result.exit_code == 0, _tail(result.output))

    async def _wait_ready(
        self, container: str, argv: list[str] | tuple[str, ...], attempts: int = 30
    ) -> bool:
        for _ in range(attempts):
            result = await self._docker.exec(container, list(argv), timeout=10)
            if result.exit_code == 0:
                return True
            await self._sleep(1)
        return False

    async def _start_mock(self, job_id: str, network: str, name: str) -> None:
        spec = ContainerSpec(
            name=name,
            image=self._python_image,
            network=network,
            aliases=("mock",),
            command=("python", "/opt/mock_server.py", str(_MOCK_PORT)),
            mounts=(_script_mount("mock_server.py"),),
            user=self._user,
            memory="128m",
            cpus="0.5",
        )
        await self._docker.start(spec, job_id)
        await self._wait_ready(
            name,
            _connect_check(_MOCK_PORT),
        )

    async def _start_sidecars(self, job_id: str, network: str, sidecars: list[Sidecar]) -> None:
        for sidecar in sidecars:
            name = f"sbx-{job_id}-{sidecar.alias}"
            spec = ContainerSpec(
                name=name,
                image=sidecar.image,
                network=network,
                aliases=(sidecar.alias,),
                command=sidecar.command,
                env=sidecar.env,
                user=None,
                read_only_root=False,
                tmpfs=(sidecar.data_dir,),
                cap_add=_SIDECAR_CAPS,
                memory="512m",
                cpus="0.5",
            )
            await self._docker.start(spec, job_id)
            if sidecar.ready_argv is not None:
                await self._wait_ready(name, sidecar.ready_argv)
            else:
                await self._sleep(5)


def _connect_check(port: int) -> list[str]:
    code = f"import socket;socket.create_connection(('127.0.0.1',{port}))"
    return ["python", "-c", code]


def _script_mount(filename: str) -> Mount:
    return Mount(str(_CONTAINERS_DIR / filename), f"/opt/{filename}", read_only=True)
