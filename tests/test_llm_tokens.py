from app.llm.tokens import estimate_tokens


def test_estimate_tokens_empty_string() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_pure_ascii() -> None:
    # 비한글만 있는 텍스트도 0보다 큰 추정치를 낸다.
    assert estimate_tokens("def foo(): return 1") > 0


def test_estimate_tokens_counts_hangul_and_other_separately() -> None:
    # 한글 10자 + 비한글 10자를 섞은 텍스트가, 같은 길이의 순수 비한글 텍스트보다
    # 더 높게(또는 같게) 추정돼야 한다 — 한글 비율(0.7)이 비한글 비율(2.5)보다
    # 낮게(=토큰을 더 많이) 잡혀 있으므로.
    hangul_text = "가" * 10
    other_text = "a" * 10
    assert estimate_tokens(hangul_text) >= estimate_tokens(other_text)


def test_estimate_tokens_is_conservative_upper_bound_on_real_tokenizer() -> None:
    """추정 함수는 /tokenize가 실패했을 때만 쓰는 폴백이라, 실제 토큰 수보다
    적게 잡으면 안 된다(컨텍스트 초과로 이어짐) — `>= 실제값 * 0.9` 같은 완화
    계수 없이 엄격한 상한을 보장해야 한다.

    골든 샘플은 운영 데이터가 아니라 이 테스트를 위해 직접 작성한 합성
    텍스트다. 실제 토큰 수는 2026-09-29에
    `transformers.AutoTokenizer.from_pretrained("Qwen/Qwen2.5-Coder-32B-Instruct")`
    로 1회 측정해 고정값으로 박아뒀다(추정 함수만으로 스스로를 검증하는
    순환 테스트가 되지 않도록).
    """
    sample = '''
def 사용자_인증_처리(요청):
    # 사용자 인증 정보를 검증하고 토큰을 발급한다
    if not 요청.토큰:
        raise 인증오류("토큰이 없습니다")
    return 토큰_발급(요청.사용자)
'''
    actual_tokens_measured_2026_09_29 = 76

    assert estimate_tokens(sample) >= actual_tokens_measured_2026_09_29
