import asyncio
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)

JOB_LABEL = "dovi.sandbox.job"


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str
    timed_out: bool = False


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    image: str
    network: str
    command: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    mounts: tuple[Mount, ...] = ()
    aliases: tuple[str, ...] = ()
    extra_networks: tuple[str, ...] = ()
    workdir: str | None = None
    user: str | None = "1000:1000"
    memory: str = "1g"
    cpus: str = "1"
    pids_limit: int = 512
    read_only_root: bool = True
    tmpfs: tuple[str, ...] = ("/tmp",)
    cap_add: tuple[str, ...] = ()


class DockerRunner(Protocol):
    async def create_network(self, name: str, job_id: str, *, internal: bool) -> None: ...

    async def run(self, spec: ContainerSpec, job_id: str, *, timeout: float) -> ExecResult: ...

    async def start(self, spec: ContainerSpec, job_id: str) -> None: ...

    async def exec(
        self, container: str, argv: Sequence[str], *, timeout: float
    ) -> ExecResult: ...

    async def kill(self, container: str, signal: str) -> None: ...

    async def remove(self, container: str) -> None: ...

    async def is_running(self, container: str) -> bool: ...

    async def logs(self, container: str) -> str: ...

    async def cleanup_job(self, job_id: str) -> None: ...

    async def reap_orphans(self) -> None: ...


def build_container_args(spec: ContainerSpec, job_id: str) -> list[str]:
    """`docker run`/`create` 뒤에 붙는 공통 인자. 셸 문자열이 아니라 argv 배열로만 조립한다."""
    args = [
        "--name", spec.name,
        "--label", f"{JOB_LABEL}={job_id}",
        "--network", spec.network,
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit", str(spec.pids_limit),
        "--memory", spec.memory,
        "--cpus", spec.cpus,
        "--init",
    ]  # fmt: skip
    for alias in spec.aliases:
        args += ["--network-alias", alias]
    for cap in spec.cap_add:
        args.append(f"--cap-add={cap}")
    if spec.user is not None:
        args += ["--user", spec.user]
    if spec.read_only_root:
        args.append("--read-only")
    for path in spec.tmpfs:
        args += ["--tmpfs", path]
    if spec.workdir is not None:
        args += ["--workdir", spec.workdir]
    for key, value in spec.env.items():
        args += ["--env", f"{key}={value}"]
    for mount in spec.mounts:
        option = f"type=bind,source={mount.source},target={mount.target}"
        args += ["--mount", option + (",readonly" if mount.read_only else "")]
    args.append(spec.image)
    args += spec.command
    return args


class ExecFn(Protocol):
    async def __call__(
        self,
        argv: Sequence[str],
        timeout: float | None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult: ...


async def run_subprocess(
    argv: Sequence[str],
    timeout: float | None,
    *,
    env: Mapping[str, str] | None = None,
) -> ExecResult:
    process = await asyncio.create_subprocess_exec(
        *argv,
        env={**os.environ, **env} if env else None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        stdout, _ = await process.communicate()
        return ExecResult(-1, stdout.decode(errors="replace"), timed_out=True)
    return ExecResult(process.returncode or 0, stdout.decode(errors="replace"))


class SubprocessDockerRunner:
    def __init__(self, exec_fn: ExecFn = run_subprocess, docker: str = "docker") -> None:
        self._exec = exec_fn
        self._docker = docker

    async def _docker_cmd(
        self, *args: str, timeout: float | None = 60, check: bool = True
    ) -> ExecResult:
        result = await self._exec([self._docker, *args], timeout)
        if check and (result.exit_code != 0 or result.timed_out):
            raise RuntimeError(f"docker {args[0]} failed: {result.output[-500:]}")
        return result

    async def create_network(self, name: str, job_id: str, *, internal: bool) -> None:
        args = ["network", "create", "--label", f"{JOB_LABEL}={job_id}"]
        if internal:
            args.append("--internal")
        await self._docker_cmd(*args, name)

    async def run(self, spec: ContainerSpec, job_id: str, *, timeout: float) -> ExecResult:
        result = await self._docker_cmd(
            "run", "--rm", *build_container_args(spec, job_id),
            timeout=timeout, check=False,
        )  # fmt: skip
        if result.timed_out:
            await self.remove(spec.name)
        return result

    async def start(self, spec: ContainerSpec, job_id: str) -> None:
        await self._docker_cmd("create", *build_container_args(spec, job_id))
        for network in spec.extra_networks:
            aliases = [arg for alias in spec.aliases for arg in ("--alias", alias)]
            await self._docker_cmd("network", "connect", *aliases, network, spec.name)
        await self._docker_cmd("start", spec.name)

    async def exec(
        self, container: str, argv: Sequence[str], *, timeout: float
    ) -> ExecResult:
        return await self._docker_cmd("exec", container, *argv, timeout=timeout, check=False)

    async def kill(self, container: str, signal: str) -> None:
        await self._docker_cmd("kill", "--signal", signal, container)

    async def remove(self, container: str) -> None:
        await self._docker_cmd("rm", "-fv", container, check=False)

    async def is_running(self, container: str) -> bool:
        result = await self._docker_cmd(
            "inspect", "--format", "{{.State.Running}}", container, check=False
        )
        return result.exit_code == 0 and result.output.strip() == "true"

    async def logs(self, container: str) -> str:
        result = await self._docker_cmd("logs", container, check=False)
        return result.output

    async def _ids(self, kind: str, label_filter: str) -> list[str]:
        if kind == "container":
            args = ["ps", "-aq", "--filter", label_filter]
        else:
            args = [kind, "ls", "-q", "--filter", label_filter]
        result = await self._docker_cmd(*args, check=False)
        return result.output.split() if result.exit_code == 0 else []

    async def _remove_matching(self, label_filter: str) -> None:
        containers = await self._ids("container", label_filter)
        if containers:
            await self._docker_cmd("rm", "-fv", *containers, check=False)
        networks = await self._ids("network", label_filter)
        if networks:
            await self._docker_cmd("network", "rm", *networks, check=False)

    async def cleanup_job(self, job_id: str) -> None:
        await self._remove_matching(f"label={JOB_LABEL}={job_id}")

    async def reap_orphans(self) -> None:
        await self._remove_matching(f"label={JOB_LABEL}")
