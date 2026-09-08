from collections.abc import Callable

import httpx

from app.context.github_release_client import GithubReleaseClient, ReleaseNotesResult


def _client(
    api_handler: Callable[[httpx.Request], httpx.Response],
    raw_handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> GithubReleaseClient:
    api_transport = httpx.MockTransport(api_handler)
    api_client = httpx.AsyncClient(transport=api_transport, base_url="https://api.github.com")
    raw_transport = httpx.MockTransport(raw_handler or (lambda r: httpx.Response(404)))
    raw_client = httpx.AsyncClient(
        transport=raw_transport, base_url="https://raw.githubusercontent.com"
    )
    return GithubReleaseClient(client=api_client, raw_client=raw_client)


async def test_finds_release_by_v_prefixed_tag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/axios/axios/releases/tags/v1.20.0"
        return httpx.Response(200, json={"body": "release notes for 1.20.0"})

    client = _client(handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result == ReleaseNotesResult(ok=True, notes="release notes for 1.20.0")


async def test_falls_back_to_name_at_version_tag() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("query-core@5.102.8"):
            return httpx.Response(200, json={"body": "monorepo release notes"})
        return httpx.Response(404)

    client = _client(handler)
    result = await client.find_release_notes("TanStack/query", "query-core", "5.102.8")

    assert result == ReleaseNotesResult(ok=True, notes="monorepo release notes")
    assert calls == [
        "/repos/TanStack/query/releases/tags/v5.102.8",
        "/repos/TanStack/query/releases/tags/query-core@5.102.8",
    ]


async def test_falls_back_to_bare_version_tag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/1.20.0"):
            return httpx.Response(200, json={"body": "bare version notes"})
        return httpx.Response(404)

    client = _client(handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result == ReleaseNotesResult(ok=True, notes="bare version notes")


async def test_falls_back_to_changelog_when_no_tag_matches() -> None:
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def raw_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/axios/axios/main/CHANGELOG.md"
        return httpx.Response(
            200,
            text=(
                "# Changelog\n\n"
                "## 1.20.0\n\n"
                "- Fixed a bug\n\n"
                "## 1.19.0\n\n"
                "- Old release\n"
            ),
        )

    client = _client(api_handler, raw_handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result == ReleaseNotesResult(ok=True, notes="- Fixed a bug")


async def test_tries_master_branch_when_main_missing() -> None:
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def raw_handler(request: httpx.Request) -> httpx.Response:
        if "/main/" in request.url.path:
            return httpx.Response(404)
        assert request.url.path == "/axios/axios/master/CHANGELOG.md"
        return httpx.Response(200, text="## 1.20.0\n\nmaster branch notes\n")

    client = _client(api_handler, raw_handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result == ReleaseNotesResult(ok=True, notes="master branch notes")


async def test_returns_ok_with_none_notes_when_nothing_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _client(handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result == ReleaseNotesResult(ok=True, notes=None)


async def test_returns_not_ok_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result.ok is False
    assert result.notes is None


async def test_changelog_section_preserves_h3_subheadings() -> None:
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def raw_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "# Changelog\n\n"
                "## 1.20.0\n\n"
                "### Features\n\n"
                "- Added new option\n\n"
                "### Bug Fixes\n\n"
                "- Fixed a bug\n\n"
                "## 1.19.0\n\n"
                "- Old release\n"
            ),
        )

    client = _client(api_handler, raw_handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result == ReleaseNotesResult(
        ok=True,
        notes=(
            "### Features\n\n"
            "- Added new option\n\n"
            "### Bug Fixes\n\n"
            "- Fixed a bug"
        ),
    )


async def test_returns_not_ok_on_rate_limited_tag_lookup() -> None:
    # 403(rate limit)은 "이 버전 릴리즈 노트가 없음을 확인했다"가 아니다 —
    # ok=True로 돌려주면 호출자가 30일간 false negative를 캐싱해버린다.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "API rate limit exceeded"})

    client = _client(handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")

    assert result.ok is False
    assert result.notes is None


async def test_returns_not_ok_on_unauthorized_tag_lookup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = _client(handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")

    assert result.ok is False
    assert result.notes is None


async def test_returns_not_ok_on_server_error_tag_lookup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _client(handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")

    assert result.ok is False
    assert result.notes is None


async def test_returns_ok_with_none_notes_on_plain_404() -> None:
    # 404는 진짜 "없음"이므로 기존대로 캐싱 가능한 확정 결과여야 한다.
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def raw_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _client(api_handler, raw_handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")

    assert result == ReleaseNotesResult(ok=True, notes=None)


async def test_returns_not_ok_when_changelog_fetch_is_rate_limited() -> None:
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def raw_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    client = _client(api_handler, raw_handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")

    assert result.ok is False
    assert result.notes is None


async def test_encodes_tag_and_owner_repo_path_segments() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.raw_path.decode())
        return httpx.Response(404)

    client = _client(handler)
    await client.find_release_notes("babel/babel", "@babel/core", "1.0.0")

    # name@version 태그의 `@`와 scope 구분자 `/`는 퍼센트 인코딩돼, 의도치 않은
    # 경로 세그먼트가 생기지 않아야 한다.
    assert "/repos/babel/babel/releases/tags/%40babel%2Fcore%401.0.0" in captured
    for path in captured:
        assert path.startswith("/repos/babel/babel/releases/tags/")


async def test_encodes_owner_repo_with_unexpected_characters() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.raw_path.decode())
        return httpx.Response(404)

    client = _client(handler)
    await client.find_release_notes("owner/repo name", "pkg", "1.0.0")

    assert captured[0] == "/repos/owner/repo%20name/releases/tags/v1.0.0"


async def test_changelog_section_uses_exact_version_not_substring() -> None:
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def raw_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "# Changelog\n\n"
                "## 12.0.0\n\n"
                "- Should not match when searching for 2.0.0\n\n"
                "## 2.0.0\n\n"
                "- Correct release notes\n"
            ),
        )

    client = _client(api_handler, raw_handler)
    result = await client.find_release_notes("axios/axios", "axios", "2.0.0")
    assert result == ReleaseNotesResult(ok=True, notes="- Correct release notes")


async def test_changelog_section_does_not_match_longer_patch_version_prefix() -> None:
    # CHANGELOG는 최신순이라 `## 1.2.30`이 `## 1.2.3`보다 먼저 나온다 —
    # 경계가 없으면 search()가 앞의 1.2.30 섹션을 먼저 잡아버린다.
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def raw_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "# Changelog\n\n"
                "## 1.2.30\n\n"
                "- Wrong section\n\n"
                "## 1.2.3\n\n"
                "- Correct section\n"
            ),
        )

    client = _client(api_handler, raw_handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.2.3")

    assert result == ReleaseNotesResult(ok=True, notes="- Correct section")


async def test_changelog_section_matches_v_prefixed_heading() -> None:
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def raw_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=("# Changelog\n\n## v1.20.0\n\n- v-prefixed notes\n\n## v1.19.0\n\n- Old\n"),
        )

    client = _client(api_handler, raw_handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")

    assert result == ReleaseNotesResult(ok=True, notes="- v-prefixed notes")
