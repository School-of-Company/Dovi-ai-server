from collections.abc import Mapping, Sequence

from app.sandbox_probe.docker import (
    ContainerSpec,
    ExecResult,
    Mount,
    SubprocessDockerRunner,
    build_container_args,
)


def _spec(**overrides: object) -> ContainerSpec:
    data: dict[str, object] = {"name": "c1", "image": "node:24-slim", "network": "net1"}
    data.update(overrides)
    return ContainerSpec(**data)  # type: ignore[arg-type]


def test_container_args_always_apply_hardening_and_job_label() -> None:
    args = build_container_args(_spec(), "job-1")

    assert "--cap-drop=ALL" in args
    assert "--security-opt=no-new-privileges" in args
    assert "--init" in args
    assert "--read-only" in args
    assert args[args.index("--label") + 1] == "dovi.sandbox.job=job-1"
    assert args[args.index("--network") + 1] == "net1"
    assert args[args.index("--user") + 1] == "1000:1000"
    for flag in ("--pids-limit", "--memory", "--cpus"):
        assert flag in args


def test_container_args_never_mount_the_docker_socket_or_use_privileged() -> None:
    args = " ".join(build_container_args(_spec(), "job-1"))

    assert "docker.sock" not in args
    assert "--privileged" not in args


def test_container_args_place_image_then_command_last() -> None:
    args = build_container_args(_spec(command=("sh", "-c", "echo hi; rm -rf /")), "j")

    assert args[-4:] == ["node:24-slim", "sh", "-c", "echo hi; rm -rf /"]


def test_container_args_pass_command_words_as_separate_argv_items() -> None:
    args = build_container_args(_spec(command=("node", "a b", "$(whoami)")), "j")

    assert args[-3:] == ["node", "a b", "$(whoami)"]


def test_container_args_render_env_mounts_aliases_and_tmpfs() -> None:
    spec = _spec(
        env={"PORT": "3000"},
        mounts=(Mount("/host/repo", "/work", read_only=False), Mount("/host/s.py", "/opt/s.py")),
        aliases=("db",),
        tmpfs=("/tmp:rw,exec",),
        workdir="/work",
        cap_add=("CHOWN",),
        user=None,
        read_only_root=False,
    )

    args = build_container_args(spec, "j")

    assert "PORT=3000" in args
    assert "type=bind,source=/host/repo,target=/work" in args
    assert "type=bind,source=/host/s.py,target=/opt/s.py,readonly" in args
    assert args[args.index("--network-alias") + 1] == "db"
    assert args[args.index("--tmpfs") + 1] == "/tmp:rw,exec"
    assert args[args.index("--workdir") + 1] == "/work"
    assert "--cap-add=CHOWN" in args
    assert "--user" not in args
    assert "--read-only" not in args


class RecordingExec:
    def __init__(self, results: Sequence[ExecResult] = ()) -> None:
        self.calls: list[list[str]] = []
        self._results = list(results)

    async def __call__(
        self,
        argv: Sequence[str],
        timeout: float | None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        self.calls.append(list(argv))
        return self._results.pop(0) if self._results else ExecResult(0, "")


async def test_start_creates_connects_extra_networks_with_aliases_then_starts() -> None:
    exec_fn = RecordingExec()
    runner = SubprocessDockerRunner(exec_fn)

    await runner.start(_spec(extra_networks=("internal",), aliases=("egress-proxy",)), "j")

    verbs = [call[1] for call in exec_fn.calls]
    assert verbs == ["create", "network", "start"]
    assert exec_fn.calls[1] == [
        "docker", "network", "connect", "--alias", "egress-proxy", "internal", "c1"
    ]  # fmt: skip


async def test_run_removes_container_when_it_times_out() -> None:
    exec_fn = RecordingExec([ExecResult(-1, "partial", timed_out=True)])
    runner = SubprocessDockerRunner(exec_fn)

    result = await runner.run(_spec(), "j", timeout=1)

    assert result.timed_out
    assert exec_fn.calls[-1][:3] == ["docker", "rm", "-fv"]


async def test_run_uses_rm_flag_and_returns_exit_code() -> None:
    exec_fn = RecordingExec([ExecResult(3, "boom")])
    runner = SubprocessDockerRunner(exec_fn)

    result = await runner.run(_spec(), "j", timeout=5)

    assert exec_fn.calls[0][:3] == ["docker", "run", "--rm"]
    assert result == ExecResult(3, "boom")


async def test_cleanup_job_removes_labelled_containers_and_networks() -> None:
    exec_fn = RecordingExec(
        [ExecResult(0, "c1\nc2\n"), ExecResult(0, ""), ExecResult(0, "n1\n"), ExecResult(0, "")]
    )
    runner = SubprocessDockerRunner(exec_fn)

    await runner.cleanup_job("job-9")

    assert exec_fn.calls[0] == ["docker", "ps", "-aq", "--filter", "label=dovi.sandbox.job=job-9"]
    assert ["docker", "rm", "-fv", "c1", "c2"] in exec_fn.calls
    assert ["docker", "network", "rm", "n1"] in exec_fn.calls


async def test_reap_orphans_filters_by_label_key_only() -> None:
    exec_fn = RecordingExec()
    runner = SubprocessDockerRunner(exec_fn)

    await runner.reap_orphans()

    assert exec_fn.calls[0] == ["docker", "ps", "-aq", "--filter", "label=dovi.sandbox.job"]


async def test_is_running_reads_inspect_output() -> None:
    runner = SubprocessDockerRunner(RecordingExec([ExecResult(0, "true\n")]))
    assert await runner.is_running("c1") is True

    runner = SubprocessDockerRunner(RecordingExec([ExecResult(1, "No such container")]))
    assert await runner.is_running("c1") is False


async def test_failed_create_network_raises() -> None:
    import pytest

    runner = SubprocessDockerRunner(RecordingExec([ExecResult(1, "denied")]))

    with pytest.raises(RuntimeError, match="network failed"):
        await runner.create_network("n", "j", internal=True)
