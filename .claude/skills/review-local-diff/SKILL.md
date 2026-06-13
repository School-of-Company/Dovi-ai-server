# Review Local Diff

## Steps

1. `git diff` — see current changes.
2. `git diff --name-only` — list changed files.
3. Read each file — understand the context before and after.
4. Review against criteria below.
5. Report findings.

## General Review Criteria

**Bugs (must check)**
- Missing `await` on async calls
- Possible `None` dereference
- Missing exception handling
- Incorrect HTTP status codes

**Security**
- Missing or bypassable webhook signature verification
- Hardcoded secrets
- Sensitive data (token, key, secret) in logs

**Missing tests**
- New endpoint without tests
- Failure cases not tested

**Performance**
- Blocking I/O inside async context
- Repeated API calls inside a loop

## Dovi-Specific Review Criteria

**GitHub installation token**
- Installation tokens expire based on `expires_at` (typically ~1 hour) — check for expiry-based refresh logic
- If cached, verify the refresh is triggered before `expires_at` (stale token returns 401)
- Confirm tokens are not exposed in logs or response bodies

**GitHub API rate limit**
- Primary rate limit: 5000 req/hr. Check handling of `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers
- Secondary rate limit: triggered by bursts or concurrent requests. Check for `Retry-After` header-based backoff
- Check retry logic for 429, 403 (rate limit), and 503 responses
- Check for unnecessary repeated API calls

**PR diff size**
- Check for size limits or file filters for large diffs (thousands of lines)
- Check for potential LLM context window overflow
- Check that binary files and generated files are excluded

**Webhook idempotency / duplicate delivery**
- GitHub may redeliver the same event up to 3 times
- Check for deduplication logic using the `X-GitHub-Delivery` header
- Check for duplicate review comment prevention

**LLM API failures / timeouts**
- Check handling of Anthropic API timeouts, 503, and 529 responses
- If retry logic exists, check for exponential backoff
- Check for notification or logging when LLM fails

**Duplicate review comment posting**
- Check whether Dovi looks for existing bot comments before posting
- Check for logic to update or skip if a bot comment already exists

## Output Format

```
[HIGH] file.py:line — description
[MED]  file.py:line — description
[LOW]  file.py:line — description
Dovi-specific issues: [if any]
Missing tests: yes/no
```

Style issues are handled by ruff. Do not duplicate.
