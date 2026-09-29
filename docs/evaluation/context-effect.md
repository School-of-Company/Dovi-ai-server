# RAG·릴리즈노트 근거 효과 오프라인 평가

## 목적

이슈 #104. 현재 `ReviewPipeline`은 RAG(관련 프로젝트 코드 검색)와 공식
릴리즈 노트 근거(`OfficialDocsWorkflow`)를 프롬프트에 추가할 수 있지만,
이 두 근거가 실제로 리뷰 품질(오탐 감소, 재현율 향상)을 높이는지 측정한
데이터가 없다. 이 문서는 사람이 라벨링한 PR 평가셋으로 조건별 리뷰
품질을 비교하는 오프라인 평가 하네스의 사용법을 설명한다.

스테이징 환경에서의 실제 실행과 사람 라벨링 작업 자체는 이 하네스의
범위 밖이다 — 하네스는 "실행하고 지표를 계산하는 도구"만 제공한다.

## 조건 정의

- **조건 A (순수 diff)**: `context_files`를 비우고 `changed_files`의
  `content`/`previousContent`를 제거한 이벤트로 실행한다. `prTitle`/
  `prBody`는 diff 이해에 필요한 최소 배경 정보로 보고 그대로 남긴다.
- **조건 B (+RAG)**: 조건 A에 `ContextRetriever`(프로젝트 기존 코드
  검색)를 추가한다.
- **조건 C (+RAG+릴리즈노트)**: 조건 B에 `OfficialDocsWorkflow`(npm
  registry → GitHub 릴리즈 노트/CHANGELOG 조회)를 추가한다.

## 평가셋 구성

### 포맷

평가셋 디렉터리 하나에 케이스별로 두 파일을 짝지어 둔다.

```
<dataset>/<case_id>.event.json    # ReviewRequestedEvent
<dataset>/<case_id>.labels.json   # CaseLabels
```

`*.event.json`은 `sample_events/pr_review_requested.json`과 같은
`ReviewRequestedEvent` 포맷이다. B/C 조건까지 의미 있게 평가하려면
`changedFiles[].content`/`previousContent`가 채워져 있어야 한다(조건
A는 하네스가 자동으로 이를 제거하므로 원본 이벤트에는 그대로 둬도 된다).

### dependency 카테고리 케이스 주의사항

`OfficialDocsWorkflow`는 `ReviewPipeline.run()`에서 `package-lock.json`
같은 lockfile 변경분만으로는 호출되지 않는다 — lockfile 외의 리뷰 대상
파일 변경이 하나도 없으면(`analyze()` 결과 `targets`가 비면)
official docs 조회 자체가 early return으로 건너뛰어진다. 따라서
`category: "dependency"` 케이스는 **lockfile 변경과 함께 다른 코드
변경도 반드시 포함**해야 한다 — 그렇지 않으면 조건 C에서 근거가 전혀
프롬프트에 붙지 않아 A/B/C 비교 자체가 무의미해진다.

### B/C 사전 인덱싱

조건 B/C는 RAG 검색 결과에 의존하므로, 평가 대상 레포를 미리 인덱싱해야
한다.

```bash
uv run python scripts/index_repo.py --path /path/to/cloned/repo --repository-id <id>
```

`--repository-id`는 각 이벤트의 `repositoryId`와 반드시 같은 값이어야
검색이 스코핑된다.

### 스테이징 LLM 버전

비교가 의미 있으려면 스테이징 LLM이 운영과 동일한 모델
(Qwen2.5-Coder-32B Q4_K_M)이어야 한다. 다른 모델/양자화로 실행하려면
`.env` 대신 `LLM_BASE_URL`/`LLM_MODEL` 환경변수로 override해서 실행
결과를 별도 `--out` 디렉터리에 남기고, 리포트에도 어떤 모델로 돌렸는지
(`meta.json`)를 함께 남긴다.

### 비공개 레포 코드 커밋 금지

평가셋에는 실제 PR diff/파일 내용이 그대로 들어가므로, 비공개 레포
코드가 포함된 평가셋은 절대 이 레포에 커밋하지 않는다. `eval-data/`와
실행 결과가 쌓이는 `eval-results/`는 이미 `.gitignore`에 포함돼 있다.

## 라벨 포맷 (`CaseLabels`)

```jsonc
{
  "caseId": "pr-123",
  "category": "dependency",           // "dependency" | "repo_specific"
  "expected": [
    {
      "id": "e1",
      "filePath": "app/service/payment.py",
      "lineStart": 12,
      "lineEnd": 14,
      "severity": "major",
      "description": "잔액 부족 체크 누락"
    }
  ],
  "judgments": {
    // 키: "{filePath}:{line}:{title}" — filter_reviews()의 dedup 키
    // (file_path, line, title)과 대응한다. expected와 매칭되지 않은
    // finding 중, 사람이 직접 참/오탐을 판정한 것만 여기 채운다.
    "app/service/payment.py:20:불필요한 로그": "false_positive"
  }
}
```

- `expected`: 이 PR에서 리뷰가 반드시 잡아야 하는 이슈 목록(재현율 계산 기준).
- `judgments`: `expected`와 매칭되지 않은 finding에 대한 사람의 참/오탐
  판정. 아직 판정하지 않은 finding은 `report` 실행 후
  `pending_judgments.json`에서 확인해 채워 넣는다.

## 실행 명령 예시

```bash
# 스모크 테스트 — fake LLM, 네트워크 불필요
uv run python -m scripts.evaluate_context_effect run \
    --dataset eval-data/my-set --out eval-results/smoke --fake-llm

# 조건 A/B/C 전체를 실제 스테이징 LLM으로 실행
uv run python -m scripts.evaluate_context_effect run \
    --dataset eval-data/my-set --out eval-results/run1 --repository-id 12345

# 조건 B/C만 실행
uv run python -m scripts.evaluate_context_effect run \
    --dataset eval-data/my-set --out eval-results/run1 --conditions B,C

# 저장된 실행 결과 + 라벨로 지표 리포트 생성 (report.md + pending_judgments.json)
uv run python -m scripts.evaluate_context_effect report \
    --dataset eval-data/my-set --out eval-results/run1

# JSON으로 stdout에만 출력 (report.md는 만들지 않음)
uv run python -m scripts.evaluate_context_effect report \
    --dataset eval-data/my-set --out eval-results/run1 --json
```

## 지표 정의

모든 비율 지표는 분모가 0이면 계산하지 않고 `null`(마크다운에서는 `-`)로
표시한다.

| 지표 | 분자 | 분모 |
|---|---|---|
| `false_positive_rate` (오탐률) | 오탐(`false_positive`) + 미판정(`unjudged`) finding 수 | 전체 finding 수 |
| `recall` (재현율) | 실제로 매칭된 `expected` 고유 id 수 (케이스 전체 합산) | 전체 `expected` 수 (케이스 전체 합산) |
| `critical_major_fp_count` | severity가 critical/major이면서 오탐 또는 미판정인 finding 수 | (절대값) |
| `evidence_prompt_rate` (근거 프롬프트 반영률, 조건 C 전용) | 근거 텍스트가 실제 LLM 프롬프트에 포함된 케이스 수 | `changedPackages`(변경된 npm 패키지)가 비어있지 않은 케이스 수 |
| `evidence_linked_finding_rate` (근거 연관 finding 비율, 조건 C 전용) | title/message/evidence에 변경된 패키지명이 하나라도 부분 문자열로 등장하는 finding 수 | 전체 finding 수 |
| `evidence_latency_p50_ms` / `p95_ms` (조건 C 전용) | `OfficialDocsWorkflow.build_evidence()` 호출 지연(ms)의 50/95 백분위수 | - |
| `cache_hit_rate` (조건 C 전용) | 릴리즈 노트 캐시 hit 수 | 캐시 조회(hit+miss) 총 수 |

`cache_hit_rate`는 운영 Redis가 아니라 `run` 실행 1회 범위의 인메모리 캐시
기준이다 — dataset에 같은 패키지@버전이 여러 케이스에 걸쳐 등장할 때만
hit가 생기고, 실행을 재시작하면 캐시는 항상 비어서 시작한다. 운영 캐시
히트율(#100에서 확인할 지표)과는 별개의 수치이므로 그대로 비교하지 않는다.

`false_positive_rate`는 미판정 finding을 보수적으로 오탐 취급해 분자에
포함하지만, `unjudged_count`로 별도 노출해 "진짜 오탐"과 "아직 라벨링
안 됨"을 구분할 수 있게 한다. `expected`와 위치(파일 + 라인 ±3 tolerance)가
일치하는 finding은 `judgments`에 뭐라 적혀 있든 항상 true positive로
취급한다(`expected` 매칭이 `judgments`보다 우선).

**주의(방법론적 한계)**: `expected` 매칭은 파일 경로와 라인 위치(±3
tolerance)만 보고, finding의 제목/설명 내용은 검증하지 않는다. 같은 파일의
같은 라인 근방에서 LLM이 `expected`가 의도한 것과 무관한 다른 문제를
지적해도 true positive로 집계된다. 같은 위치에 `expected`가 여러 개
겹치면 그중 처음 매칭되는 것과만 대응한다. 라벨링할 때 한 PR 안에서
`expected` 위치가 서로 3줄 이내로 가깝게 겹치지 않도록 하고, 재현율·오탐률
수치를 해석할 때도 이 한계를 감안한다.

## 결과

측정 전. 조건 A/B/C 실행 결과가 아직 쌓이지 않았다. 근거(RAG/릴리즈
노트)가 오탐률·재현율에 유의미한 효과가 있다고 확인되면, 이슈 #100에서
운영 환경 활성화(`rag_enabled`/`official_docs_workflow_enabled`)를
검토한다.
