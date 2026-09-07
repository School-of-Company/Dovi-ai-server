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
