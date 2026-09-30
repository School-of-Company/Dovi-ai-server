import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.sandbox_probe.docker import ContainerSpec, DockerRunner, ExecResult
from app.sandbox_probe.schema import Finding
from app.sandbox_probe.verdict import ProbeOutcome

_SLEEP = Callable[[float], Awaitable[None]]

_MOCK_PORT = 9000
_MOCK_RECEIVED_SCRIPT = (
    "import urllib.request,sys;"
    f"sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:{_MOCK_PORT}/_received')"
    ".read().decode())"
)


@dataclass(frozen=True)
class ImportFailure:
    file: str
    error: str


def parse_init_order_output(output: str) -> tuple[int, list[ImportFailure]] | None:
    for line in reversed(output.strip().splitlines()):
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            failures = [ImportFailure(f["file"], f["error"]) for f in data["failures"]]
            return int(data["checked"]), failures
        except (ValueError, KeyError, TypeError):
            return None
    return None


def _guess_source_path(dist_relative_file: str) -> str:
    stem = dist_relative_file.rsplit(".", 1)[0]
    return f"src/{stem}.ts"


def init_order_outcome(result: ExecResult) -> ProbeOutcome:
    parsed = parse_init_order_output(result.output)
    if result.timed_out or result.exit_code != 0 or parsed is None:
        return ProbeOutcome("init_order", "inconclusive", evidence=result.output[-2000:])
    checked, failures = parsed
    if not failures:
        return ProbeOutcome("init_order", "passed", evidence=f"{checked}개 파일 import 확인")
    findings = tuple(
        Finding(
            probe="init_order",
            title="초기화 순서 오류 (순환 참조 의심)",
            message=(
                f"빌드 산출물 `{failure.file}`을 단독으로 import하면 초기화 에러가 발생합니다. "
                "순환 import와 데코레이터 메타데이터가 함께 있을 때 특정 import 순서에서만 "
                "나타나는 문제일 수 있습니다."
            ),
            file_path=_guess_source_path(failure.file),
            evidence=failure.error,
        )
        for failure in failures
    )
    return ProbeOutcome(
        "init_order",
        "found_issue",
        findings=findings,
        evidence=f"{checked}개 중 {len(failures)}개 파일에서 초기화 에러",
    )


async def mock_received(docker: DockerRunner, mock_container: str) -> list[dict[str, str]]:
    result = await docker.exec(
        mock_container, ["python", "-c", _MOCK_RECEIVED_SCRIPT], timeout=10
    )
    if result.exit_code != 0:
        return []
    try:
        received = json.loads(result.output)
    except ValueError:
        return []
    return received if isinstance(received, list) else []


async def _tcp_open(docker: DockerRunner, container: str, port: int) -> bool:
    script = (
        f"require('net').connect({port},'127.0.0.1')"
        ".on('connect',()=>process.exit(0)).on('error',()=>process.exit(1))"
    )
    result = await docker.exec(container, ["node", "-e", script], timeout=10)
    return result.exit_code == 0


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]],
    timeout: float,
    poll: float,
    sleep: _SLEEP,
) -> bool:
    waited = 0.0
    while True:
        if await predicate():
            return True
        if waited >= timeout:
            return False
        await sleep(poll)
        waited += poll


async def run_lifecycle_probe(
    docker: DockerRunner,
    job_id: str,
    app_spec: ContainerSpec,
    mock_container: str,
    port: int,
    *,
    startup_timeout: float = 30,
    notify_timeout: float = 5,
    shutdown_timeout: float = 5,
    poll: float = 0.5,
    sleep: _SLEEP = asyncio.sleep,
) -> ProbeOutcome:
    await docker.start(app_spec, job_id)
    app = app_spec.name

    async def started() -> bool:
        if not await docker.is_running(app):
            return True
        return await _tcp_open(docker, app, port) or bool(
            await mock_received(docker, mock_container)
        )

    await _wait_until(started, startup_timeout, poll, sleep)
    if not await docker.is_running(app) or not await _tcp_open(docker, app, port):
        return ProbeOutcome(
            "lifecycle",
            "inconclusive",
            evidence="앱이 기동하지 못했습니다.\n" + (await docker.logs(app))[-2000:],
        )

    async def start_notification_arrived() -> bool:
        return bool(await mock_received(docker, mock_container))

    if not await _wait_until(start_notification_arrived, notify_timeout, poll, sleep):
        return ProbeOutcome(
            "lifecycle",
            "skip",
            evidence="기동 시 알림이 오지 않아 종료 알림 검증을 건너뜁니다.",
        )

    before = len(await mock_received(docker, mock_container))
    await docker.kill(app, "SIGTERM")

    async def shutdown_notification_arrived() -> bool:
        return len(await mock_received(docker, mock_container)) > before

    if await _wait_until(shutdown_notification_arrived, shutdown_timeout, poll, sleep):
        return ProbeOutcome("lifecycle", "passed", evidence="SIGTERM 후 종료 알림 확인")

    logs = (await docker.logs(app))[-1500:]
    finding = Finding(
        probe="lifecycle",
        title="SIGTERM 시 종료 알림 미발송",
        message=(
            "기동 알림은 도착했지만 SIGTERM 후 종료 알림이 오지 않았습니다. "
            "shutdown hook이 등록되지 않았을 수 있습니다(예: `app.enableShutdownHooks()` 누락)."
        ),
        evidence=f"SIGTERM 후 {shutdown_timeout:g}초 동안 알림 없음\n{logs}",
    )
    return ProbeOutcome(
        "lifecycle",
        "found_issue",
        findings=(finding,),
        evidence="SIGTERM 후 종료 알림 미도착",
    )
