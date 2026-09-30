import json

from app.sandbox_probe.docker import ContainerSpec, ExecResult
from app.sandbox_probe.probes import (
    init_order_outcome,
    parse_init_order_output,
    run_lifecycle_probe,
)
from tests.sandbox_fakes import FakeDocker, no_sleep

_FAILURE = {
    "file": "form/entities/form.entity.js",
    "error": "ReferenceError: Cannot access 'FormEntity' before initialization",
}


def _output(checked: int = 18, failures: list[dict[str, str]] | None = None) -> str:
    return "noise line\n" + json.dumps({"checked": checked, "failures": failures or []})


def test_parse_init_order_output_reads_the_last_json_line() -> None:
    parsed = parse_init_order_output(_output(3, [_FAILURE]))

    assert parsed is not None
    checked, failures = parsed
    assert checked == 3
    assert failures[0].file == "form/entities/form.entity.js"


def test_parse_init_order_output_returns_none_for_garbage() -> None:
    assert parse_init_order_output("") is None
    assert parse_init_order_output("plain text only") is None
    assert parse_init_order_output('{"checked": 1}') is None
    assert parse_init_order_output("{broken") is None


def test_init_order_passes_when_every_file_imports_cleanly() -> None:
    outcome = init_order_outcome(ExecResult(0, _output(18)))

    assert outcome.status == "passed"
    assert outcome.findings == ()


def test_init_order_reports_one_finding_per_failing_file_with_source_path() -> None:
    outcome = init_order_outcome(ExecResult(0, _output(18, [_FAILURE])))

    assert outcome.status == "found_issue"
    finding = outcome.findings[0]
    assert finding.probe == "init_order"
    assert finding.file_path == "src/form/entities/form.entity.ts"
    assert "before initialization" in finding.evidence


def test_init_order_is_inconclusive_when_the_probe_itself_broke() -> None:
    assert init_order_outcome(ExecResult(1, "node: not found")).status == "inconclusive"
    assert init_order_outcome(ExecResult(0, "no json")).status == "inconclusive"
    assert init_order_outcome(ExecResult(-1, "", timed_out=True)).status == "inconclusive"


def _app_spec() -> ContainerSpec:
    return ContainerSpec(name="sbx-1-app", image="node:24-slim", network="net")


async def _lifecycle(docker: FakeDocker) -> object:
    return await run_lifecycle_probe(
        docker, "job", _app_spec(), "sbx-1-mock", 3000, sleep=no_sleep, poll=1
    )


async def test_lifecycle_passes_when_shutdown_notification_arrives_after_sigterm() -> None:
    docker = FakeDocker()

    outcome = await _lifecycle(docker)

    assert outcome.status == "passed"  # type: ignore[attr-defined]
    assert docker.killed == [("sbx-1-app", "SIGTERM")]


async def test_lifecycle_reports_issue_when_no_shutdown_notification_arrives() -> None:
    docker = FakeDocker()
    docker.send_shutdown_notification = False

    outcome = await _lifecycle(docker)

    assert outcome.status == "found_issue"  # type: ignore[attr-defined]
    finding = outcome.findings[0]  # type: ignore[attr-defined]
    assert finding.probe == "lifecycle"
    assert "app logs" in finding.evidence


async def test_lifecycle_skips_when_app_never_sends_a_start_notification() -> None:
    docker = FakeDocker()
    docker.send_start_notification = False

    outcome = await _lifecycle(docker)

    assert outcome.status == "skip"  # type: ignore[attr-defined]
    assert docker.killed == []


async def test_lifecycle_is_inconclusive_when_the_app_exits_during_startup() -> None:
    docker = FakeDocker()
    docker.running = False

    outcome = await _lifecycle(docker)

    assert outcome.status == "inconclusive"  # type: ignore[attr-defined]
    assert "app logs" in outcome.evidence  # type: ignore[attr-defined]


async def test_lifecycle_is_inconclusive_when_the_port_never_opens() -> None:
    docker = FakeDocker()
    docker.tcp_open = False
    docker.send_start_notification = False

    outcome = await run_lifecycle_probe(
        docker, "job", _app_spec(), "sbx-1-mock", 3000,
        startup_timeout=3, sleep=no_sleep, poll=1,
    )  # fmt: skip

    assert outcome.status == "inconclusive"
