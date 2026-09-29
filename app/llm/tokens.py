import math
import re

# /tokenize 호출이 실패했을 때만 쓰는 폴백 추정기다. 실제 Qwen2.5-Coder 토크나이저로
# 직접 작성한(운영 데이터 아님) 합성 샘플들을 측정해서 비율을 정했다(이슈 #98) —
# 단일 상수(예: 2.0)로는 한글에서 토큰 수를 과소평가하고 영문 코드에서는 과대평가해
# 두 문제가 동시에 생긴다는 지적을 반영해, 한글/비한글을 나눠 센다.
#
# 실측 결과(transformers.AutoTokenizer.from_pretrained("Qwen/Qwen2.5-Coder-32B-Instruct"),
# 2026-09-29, 합성 샘플 7개):
#   - 한글 주석이 섞인 코드에서 한글 chars/token 최악 0.83까지 관측 (평문 한글은 1.3~1.6)
#   - 연산자/심볼이 밀집한 코드에서 비한글 chars/token 최악 2.98까지 관측
#     (자연어에 가까운 영문 코드는 5.5 안팎)
# 두 값 다 관측된 최악치보다 낮게(=토큰 수를 더 많이 잡게) 잡아 항상 과소추정하지
# 않도록 한다.
_HANGUL_CHARS_PER_TOKEN = 0.7
_OTHER_CHARS_PER_TOKEN = 2.5

_HANGUL_RE = re.compile(r"[가-힣ㄱ-ㆎ]")


def estimate_tokens(text: str) -> int:
    """실제 토크나이저(/tokenize) 호출이 실패했을 때만 쓰는 보수적 토큰 수 추정.

    한글과 그 외 문자를 나눠서 각각의 chars/token 비율을 적용한다 — 절대 실제
    토큰 수보다 적게 추정하지 않도록(과소추정 시 컨텍스트 초과로 이어짐) 관측된
    최악 비율보다도 낮은 값을 쓴다.
    """
    if not text:
        return 0
    hangul_chars = len(_HANGUL_RE.findall(text))
    other_chars = len(text) - hangul_chars
    return math.ceil(hangul_chars / _HANGUL_CHARS_PER_TOKEN) + math.ceil(
        other_chars / _OTHER_CHARS_PER_TOKEN
    )
