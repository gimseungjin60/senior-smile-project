"""
ws_binary_probe 로컬 검증 (네트워크 없이 in-process ASGI).
우리 FastAPI/Starlette 스택이 binary 프레임을 변형 없이 다루는지 증명.
(Railway 프록시 통과 여부는 배포 후 / 페이지로만 확인 가능 — 이 테스트 범위 밖)

실행: cd backend && python scripts/test_ws_probe_local.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ws_binary_probe import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SIZE = 65536


def main() -> int:
    client = TestClient(app)
    with client.websocket_connect("/ws/probe") as ws:
        payload = bytes((i * 7 + 13) % 256 for i in range(SIZE))
        ws.send_bytes(payload)
        echoed = ws.receive_bytes()
        if echoed != payload:
            print(f"FAIL: binary {len(echoed)}B != {SIZE}B 또는 내용 불일치")
            return 1
        print(f"PASS: binary roundtrip {len(echoed)}B 완전 일치")

        ws.send_text("hello-probe")
        t = ws.receive_text()
        if t != "echo:hello-probe":
            print(f"FAIL: text echo 불일치: {t!r}")
            return 1
        print(f"PASS: text roundtrip {t!r}")
    print("로컬 스택 binary/text WS 처리 OK. (프록시 검증은 Railway 배포 후 / 페이지)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
