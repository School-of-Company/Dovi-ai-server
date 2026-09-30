import json
from collections.abc import Callable, Sequence

from app.sandbox_probe.docker import ContainerSpec, ExecResult


class FakeDocker:
    """DockerRunner의 fake. 실제 docker 없이 러너/프로브의 흐름과 컨테이너 스펙을 검증한다."""

    def __init__(self) -> None:
        self.networks: list[tuple[str, bool]] = []
        self.ran: list[ContainerSpec] = []
        self.started: list[ContainerSpec] = []
        self.removed: list[str] = []
        self.killed: list[tuple[str, str]] = []
        self.cleaned: list[str] = []
        self.reaped = False
        self.run_handler: Callable[[ContainerSpec], ExecResult] = lambda spec: ExecResult(0, "")
        self.running = True
        self.tcp_open = True
        self.received: list[dict[str, str]] = []
        self.send_start_notification = True
        self.send_shutdown_notification = True
        self.logs_text = "app logs"

    async def create_network(self, name: str, job_id: str, *, internal: bool) -> None:
        self.networks.append((name, internal))

    async def run(self, spec: ContainerSpec, job_id: str, *, timeout: float) -> ExecResult:
        self.ran.append(spec)
        return self.run_handler(spec)

    async def start(self, spec: ContainerSpec, job_id: str) -> None:
        self.started.append(spec)
        if spec.name.endswith("-app") and self.send_start_notification:
            self.received.append({"method": "POST", "path": "/start", "body": ""})

    async def exec(
        self, container: str, argv: Sequence[str], *, timeout: float
    ) -> ExecResult:
        if argv[0] == "python" and "_received" in argv[-1]:
            return ExecResult(0, json.dumps(self.received))
        if argv[0] == "node":
            return ExecResult(0 if self.tcp_open else 1, "")
        return ExecResult(0, "")

    async def kill(self, container: str, signal: str) -> None:
        self.killed.append((container, signal))
        if self.send_shutdown_notification:
            self.received.append({"method": "POST", "path": "/stop", "body": ""})

    async def remove(self, container: str) -> None:
        self.removed.append(container)

    async def is_running(self, container: str) -> bool:
        return self.running

    async def logs(self, container: str) -> str:
        return self.logs_text

    async def cleanup_job(self, job_id: str) -> None:
        self.cleaned.append(job_id)

    async def reap_orphans(self) -> None:
        self.reaped = True


async def no_sleep(seconds: float) -> None:
    return None
