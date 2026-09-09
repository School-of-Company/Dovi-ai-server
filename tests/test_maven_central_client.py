from collections.abc import Callable

import httpx

from app.context.maven_central_client import MavenCentralClient, RelocationLookupResult

_RELOCATED_POM = """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>commons-logging</groupId>
  <artifactId>commons-logging</artifactId>
  <version>1.2</version>
  <distributionManagement>
    <relocation>
      <groupId>org.apache.commons</groupId>
      <artifactId>commons-logging</artifactId>
      <message>moved to org.apache.commons</message>
    </relocation>
  </distributionManagement>
</project>
"""

_NON_RELOCATED_POM = """\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.google.code.gson</groupId>
  <artifactId>gson</artifactId>
  <version>2.13.1</version>
</project>
"""

_NON_RELOCATED_POM_NO_NAMESPACE = """\
<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.google.code.gson</groupId>
  <artifactId>gson</artifactId>
  <version>2.13.1</version>
</project>
"""


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> MavenCentralClient:
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(
        transport=transport, base_url="https://repo1.maven.org/maven2"
    )
    return MavenCentralClient(client=async_client)


async def test_returns_relocation_target_when_pom_has_relocation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            request.url.path
            == "/maven2/commons-logging/commons-logging/1.2/commons-logging-1.2.pom"
        )
        return httpx.Response(200, content=_RELOCATED_POM.encode())

    client = _client(handler)
    result = await client.check_relocation("commons-logging:commons-logging", "1.2")
    assert result == RelocationLookupResult(
        ok=True, relocated_to="org.apache.commons:commons-logging"
    )


async def test_returns_none_when_no_relocation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_NON_RELOCATED_POM.encode())

    client = _client(handler)
    result = await client.check_relocation("com.google.code.gson:gson", "2.13.1")
    assert result == RelocationLookupResult(ok=True, relocated_to=None)


async def test_handles_pom_without_namespace() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_NON_RELOCATED_POM_NO_NAMESPACE.encode())

    client = _client(handler)
    result = await client.check_relocation("com.google.code.gson:gson", "2.13.1")
    assert result == RelocationLookupResult(ok=True, relocated_to=None)


async def test_builds_group_path_from_dotted_group_id() -> None:
    captured_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.path)
        return httpx.Response(200, content=_NON_RELOCATED_POM.encode())

    client = _client(handler)
    await client.check_relocation("org.springframework.boot:spring-boot-starter-web", "3.2.10")

    assert captured_paths == [
        "/maven2/org/springframework/boot/spring-boot-starter-web/3.2.10/"
        "spring-boot-starter-web-3.2.10.pom"
    ]


async def test_returns_not_ok_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _client(handler)
    result = await client.check_relocation("nonexistent:artifact", "1.0.0")
    assert result == RelocationLookupResult(ok=False, relocated_to=None)


async def test_returns_not_ok_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    client = _client(handler)
    result = await client.check_relocation("com.google.code.gson:gson", "2.13.1")
    assert result == RelocationLookupResult(ok=False, relocated_to=None)


async def test_returns_not_ok_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    result = await client.check_relocation("com.google.code.gson:gson", "2.13.1")
    assert result == RelocationLookupResult(ok=False, relocated_to=None)


async def test_returns_not_ok_on_malformed_xml() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not valid xml <<<")

    client = _client(handler)
    result = await client.check_relocation("com.google.code.gson:gson", "2.13.1")
    assert result == RelocationLookupResult(ok=False, relocated_to=None)


async def test_returns_not_ok_when_name_has_no_group_artifact_separator() -> None:
    client = _client(lambda request: httpx.Response(200))
    result = await client.check_relocation("just-a-name-no-colon", "1.0.0")
    assert result == RelocationLookupResult(ok=False, relocated_to=None)
