import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from app.sandbox_probe.checkout import CheckoutError, checkout
from app.sandbox_probe.docker import ExecResult, run_subprocess

_TOKEN = "ghs_supersecrettoken123"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, str]:
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "package.json").write_text('{"name": "x"}')
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "one", cwd=work)
    first = _git("rev-parse", "HEAD", cwd=work)
    (work / "package.json").write_text('{"name": "y"}')
    _git("commit", "-q", "-am", "two", cwd=work)

    bare = tmp_path / "org" / "repo.git"
    bare.parent.mkdir()
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=bare)
    return tmp_path, first


async def test_checkout_pins_requested_sha_and_leaves_no_credentials(
    origin: tuple[Path, str], tmp_path: Path
) -> None:
    base, first_sha = origin
    dest = tmp_path / "dest" / "repo"

    await checkout("org/repo", first_sha, _TOKEN, dest, base_url=f"file://{base}")

    assert _git("rev-parse", "HEAD", cwd=dest) == first_sha
    assert (dest / "package.json").read_text() == '{"name": "x"}'
    assert _TOKEN not in (dest / ".git" / "config").read_text()
    assert "@" not in _git("config", "--get", "remote.origin.url", cwd=dest)


async def test_token_is_only_passed_via_env_of_the_fetch_process(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Mapping[str, str] | None]] = []

    async def fake_exec(
        argv: Sequence[str], timeout: float | None, *, env: Mapping[str, str] | None = None
    ) -> ExecResult:
        calls.append((list(argv), env))
        return ExecResult(0, "https://github.com/org/repo.git")

    await checkout("org/repo", "abc", _TOKEN, tmp_path / "r", exec_fn=fake_exec)

    assert all(_TOKEN not in " ".join(argv) for argv, _ in calls)
    with_env = [(argv, env) for argv, env in calls if env]
    assert len(with_env) == 1
    assert "fetch" in with_env[0][0]
    fetch_env = with_env[0][1]
    assert fetch_env is not None
    assert "Authorization: Basic" in fetch_env["GIT_CONFIG_VALUE_0"]


async def test_checkout_error_never_echoes_the_token(tmp_path: Path) -> None:
    async def failing_exec(
        argv: Sequence[str], timeout: float | None, *, env: Mapping[str, str] | None = None
    ) -> ExecResult:
        if "fetch" in argv:
            return ExecResult(128, f"fatal: unable to access with {_TOKEN}")
        return ExecResult(0, "")

    with pytest.raises(CheckoutError) as info:
        await checkout("org/repo", "abc", _TOKEN, tmp_path / "r", exec_fn=failing_exec)

    assert _TOKEN not in str(info.value)


async def test_checkout_rejects_remote_url_that_contains_credentials(tmp_path: Path) -> None:
    async def leaky_exec(
        argv: Sequence[str], timeout: float | None, *, env: Mapping[str, str] | None = None
    ) -> ExecResult:
        if "config" in argv:
            return ExecResult(0, "https://x-access-token:abc@github.com/org/repo.git")
        return ExecResult(0, "")

    with pytest.raises(CheckoutError, match="credentials"):
        await checkout("org/repo", "abc", _TOKEN, tmp_path / "r", exec_fn=leaky_exec)


async def test_run_subprocess_merges_env_and_reports_timeout() -> None:
    result = await run_subprocess(
        ["sh", "-c", "echo $SBX_TEST_VALUE"], 5, env={"SBX_TEST_VALUE": "hello"}
    )
    assert result == ExecResult(0, "hello\n")

    timed_out = await run_subprocess(["sleep", "5"], 0.1)
    assert timed_out.timed_out
