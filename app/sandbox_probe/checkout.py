import base64
from pathlib import Path

from app.sandbox_probe.docker import ExecFn, run_subprocess


class CheckoutError(RuntimeError):
    pass


async def checkout(
    repo_full_name: str,
    head_sha: str,
    token: str,
    dest: Path,
    *,
    base_url: str = "https://github.com",
    exec_fn: ExecFn = run_subprocess,
) -> None:
    """head_sha를 고정해 clone하고 인증 정보가 남지 않은 워킹트리만 만든다.

    토큰은 argv가 아니라 fetch 프로세스 하나의 환경변수로만 전달해 `ps`, `.git/config`,
    같은 프로세스에서 도는 다른 잡의 git 어디에도 남지 않는다.
    """
    dest.mkdir(parents=True, exist_ok=True)
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    auth_env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }

    async def git(*args: str, authenticated: bool = False) -> str:
        result = await exec_fn(
            ["git", "-C", str(dest), *args],
            300,
            env=auth_env if authenticated else None,
        )
        if result.exit_code != 0 or result.timed_out:
            raise CheckoutError(f"git {args[0]} failed: {_scrub(result.output, token, basic)}")
        return result.output

    await git("init", "-q")
    await git("remote", "add", "origin", f"{base_url}/{repo_full_name}.git")
    await git("fetch", "-q", "--depth", "1", "origin", head_sha, authenticated=True)
    await git("checkout", "-q", "--detach", "FETCH_HEAD")

    remote_url = (await git("config", "--get", "remote.origin.url")).strip()
    if "@" in remote_url:
        raise CheckoutError("remote url contains credentials")


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text[-500:]
