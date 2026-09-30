import httpx


class TokenFetchError(RuntimeError):
    pass


class GithubAppTokenClient:
    """github-app 내부 API에서 `contents:read`로 스코프를 좁힌 installation token을 받는다.

    App private key는 github-app에만 있고 이 VM에는 두지 않는다.
    """

    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/internal/sandbox-probe/token"
        self._secret = secret
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def fetch_token(self, installation_id: int, repository_id: int) -> str:
        try:
            response = await self._client.post(
                self._url,
                json={"installationId": installation_id, "repositoryId": repository_id},
                headers={"X-Dovi-Internal-Secret": self._secret},
            )
            response.raise_for_status()
            token = response.json()["token"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            # 응답 본문에 토큰이 섞일 수 있어 예외 메시지에는 상태만 남긴다.
            raise TokenFetchError(f"token request failed: {type(exc).__name__}") from None
        if not isinstance(token, str) or not token:
            raise TokenFetchError("token response is empty")
        return token

    async def aclose(self) -> None:
        await self._client.aclose()
