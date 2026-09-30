"""스펙 수용 기준: Expo-Form-Server의 실제 PR #5/#12 수정 전·후 커밋 4개를 재생한다.

실제 clone + Docker 빌드/기동이라 몇 분씩 걸려 기본 실행에서는 제외한다
(`pytest -m fixture_replay`로 실행, CI는 .github/workflows/sandbox-fixtures.yml).
"""

import os
from pathlib import Path

import pytest

from app.sandbox_probe.docker import SubprocessDockerRunner
from app.sandbox_probe.runner import SandboxJobRunner
from app.sandbox_probe.schema import SandboxProbeRequestedEvent

pytestmark = pytest.mark.fixture_replay

_REPO = "School-of-Company/Expo-Form-Server"

_FIXTURES = [
    pytest.param(
        "07045bcb0d9a6ffdf501f200036a5daa20f052a9", "found_issue", {"init_order"}, id="pr5-buggy"
    ),
    pytest.param("b13836e9136de4954f8f09ddfdde4f0ca8ab360c", "passed", set(), id="pr5-fixed"),
    pytest.param(
        "8a4f5f9a2dc7bae7e7985d48d437ed584678a71b", "found_issue", {"lifecycle"}, id="pr12-buggy"
    ),
    pytest.param("482db26e39930305bdf24154ea927352f9dab6ed", "passed", set(), id="pr12-fixed"),
]


class StaticTokenSource:
    def __init__(self, token: str) -> None:
        self._token = token

    async def fetch_token(self, installation_id: int, repository_id: int) -> str:
        return self._token


@pytest.mark.parametrize(("sha", "expected_status", "expected_probes"), _FIXTURES)
async def test_fixture_replay(
    sha: str, expected_status: str, expected_probes: set[str], tmp_path: Path
) -> None:
    token = os.environ.get("FIXTURE_GITHUB_TOKEN")
    if not token:
        if os.environ.get("REQUIRE_FIXTURES"):
            pytest.fail("FIXTURE_GITHUB_TOKEN이 없어 fixture를 재생할 수 없습니다.")
        pytest.skip("FIXTURE_GITHUB_TOKEN이 없습니다.")

    runner = SandboxJobRunner(
        SubprocessDockerRunner(),
        StaticTokenSource(token),
        workdir=tmp_path / "work",
        min_free_disk_gb=1,
    )
    event = SandboxProbeRequestedEvent(
        review_job_id=f"1:1:{sha}",
        repository_id=1,
        installation_id=1,
        repo_full_name=_REPO,
        pr_number=1,
        head_sha=sha,
        base_sha=sha,
    )

    result = await runner.run(event)

    assert result.status == expected_status, result.evidence
    assert {finding.probe for finding in result.findings} == expected_probes
