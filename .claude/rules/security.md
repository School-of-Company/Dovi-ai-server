# Dovi Security Rules

## Strictly Forbidden

- Reading or printing `.env`, `.env.*` files
- Printing GitHub App private key (`.pem`, `.p8`, `private-key.*`)
- Hardcoding webhook secret in code
- Logging installation access tokens
- Hardcoding LLM API keys in code
- Hardcoding GitHub tokens, DB passwords, or connection strings
- Reading files under `secrets/`

## Environment Variable Management

All configuration values must be accessed exclusively through the `Settings` class in `app/core/config.py`.

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    github_app_id: int
    github_private_key: str
    webhook_secret: str
    anthropic_api_key: str

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
```

## GitHub App Authentication

- Use the private key in memory only when generating JWTs
- Installation tokens expire based on `expires_at` (typically ~1 hour) — implement expiry-based refresh logic
- Never include tokens in logs, response bodies, or PR comments

## Webhook Security

- All webhook requests must have `X-Hub-Signature-256` signature verification
- Return 403 on signature verification failure
- Never introduce conditional branches that can bypass signature verification

## PR Diff Handling

- Do not pass sensitive data found in diffs (keys, tokens, passwords) directly into LLM prompts
- Apply expiry policy if diffs are cached externally

## Logging

- Never include tokens, API keys, signatures, or personal data in logs
- Do not include full request bodies in error logs
- Use structured logging (JSON format recommended)
