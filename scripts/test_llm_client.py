"""
llama-server (OpenAI-compatible) 연결 테스트 스크립트.

사용법:
  uv run python scripts/test_llm_client.py
  uv run python scripts/test_llm_client.py --base-url http://localhost:8001/v1
"""

import argparse
import json
import sys

import httpx


def test_health(base_url: str) -> bool:
    try:
        r = httpx.get(f"{base_url}/models", timeout=5)
        r.raise_for_status()
        models = r.json()
        print(f"[OK] /models → {json.dumps(models, ensure_ascii=False)}")
        return True
    except httpx.ConnectError:
        print(f"[FAIL] {base_url} 에 연결할 수 없습니다. llama-server가 실행 중인지 확인하세요.")
        return False
    except Exception as e:
        print(f"[FAIL] {e}")
        return False


def test_completion(base_url: str, model: str) -> bool:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "def add(a, b): 이 함수를 한 줄로 완성해줘."}],
        "max_tokens": 64,
        "temperature": 0,
    }
    try:
        r = httpx.post(f"{base_url}/chat/completions", json=payload, timeout=60)
        r.raise_for_status()
        result = r.json()
        choices = result.get("choices", [])
        if not choices:
            print("[FAIL] 응답에 'choices' 결과가 없습니다.")
            return False
        content = choices[0].get("message", {}).get("content", "")
        print(f"[OK] chat/completions → {content[:120]}")
        return True
    except httpx.ConnectError:
        print(f"[FAIL] {base_url} 에 연결할 수 없습니다.")
        return False
    except Exception as e:
        print(f"[FAIL] {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8001/v1")
    parser.add_argument("--model", default="local-model")
    args = parser.parse_args()

    print(f"LLM 서버: {args.base_url}\n")

    ok = test_health(args.base_url)
    if not ok:
        sys.exit(1)

    test_completion(args.base_url, args.model)


if __name__ == "__main__":
    main()
