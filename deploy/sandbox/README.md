# 샌드박스 프로브 VM 배포

PR 코드를 실제로 빌드/기동하는 워커(이슈 #97)를 프로덕션 GPU 박스와 분리된 전용 VM에서 돌린다.
설계는 `docs/superpowers/specs/2026-09-23-build-verify-agent-design.md`를 본다.

## 사전 조건 (VM에서 한 번)

1. Docker Engine 설치, 배포 사용자(`ubuntu`)를 `docker` 그룹에 추가
2. [uv](https://docs.astral.sh/uv/) 설치 (`~/.local/bin/uv`)
3. 배포 사용자의 비밀번호 없는 `sudo` (systemd 유닛 설치용)
4. 작업 디렉터리: `sudo mkdir -p /var/lib/dovi-sandbox && sudo chown ubuntu: /var/lib/dovi-sandbox`
5. `~/Dovi-ai-server/.env`를 `.env.sandbox.example`을 보고 작성 (git에 올리지 않는다)
6. VM에서 Kafka와 Redis, github-app 내부 API에 닿는 경로를 정해 `.env`에 반영한다.
   프로덕션 박스는 현재 방화벽 없이 `127.0.0.1` 바인딩과 SSH 터널을 쓰므로, 터널로 갈지 허용 규칙을 둘지 먼저 결정해야 한다.

## CD

`.github/workflows/cd-sandbox.yml`이 `main`에 머지될 때 VM에 배포한다.
아래를 준비한 뒤 저장소 변수 `SANDBOX_CD_ENABLED=true`를 설정하면 켜진다.

- 시크릿: `SANDBOX_SSH_HOST`, `SANDBOX_SSH_PORT`, `SANDBOX_SSH_USER`, `SANDBOX_SSH_PRIVATE_KEY`
- VM이 현재 비밀번호 인증만 쓴다면 CD용 공개키를 `~/.ssh/authorized_keys`에 먼저 등록한다.

배포 순서: 이 워커를 먼저 배포하고, 그 다음 github-app 발행(킬스위치 끈 상태)을 배포한다.

## 확인

```bash
systemctl status dovi-sandbox-probe
journalctl -u dovi-sandbox-probe -f
docker ps -a --filter label=dovi.sandbox.job   # 진행 중이 아닐 때는 비어 있어야 한다
```

## 참고

- 이 VM에는 torch 계열 패키지를 설치하지 않는다(RAG를 쓰지 않는다).
- 워커는 기동할 때 `dovi.sandbox.job` 라벨이 붙은 고아 컨테이너와 네트워크를 먼저 정리한다.
- fixture 재생 검증: `FIXTURE_GITHUB_TOKEN=$(gh auth token) uv run pytest -m fixture_replay -v`
