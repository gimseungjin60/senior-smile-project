"""
[격리 모듈] /ws/voice 전송 계층. (docs/voice-cloud-refactor-design.md §6)

Railway 프록시가 WebSocket binary 프레임을 못 넘기는 것으로 밝혀지면(프로브 FAIL),
폴백(base64-over-text)으로 전환할 때 **이 파일 하나만** 고치면 되도록 격리한다.

방향별 위험도:
- 인바운드(브라우저 마이크 → 서버): 발화 오디오를 binary 프레임으로 업로드 → **위험 지점**(여기 격리)
- 아웃바운드(서버 → 브라우저): TTS/효과음은 HTTP FileResponse(main.py)로 서빙하고 WS로는
  {type:'speak', url} JSON만 보냄 → 바이너리 다운로드 없음, 격리 불필요

전환 방법: TRANSPORT 만 "base64"로 바꾸면 인바운드가 {type:'audio', data:<base64>} text 프레임을 받음.
voice_agent / main.py 의 나머지 코드는 전송 방식을 모른다(transport-agnostic).
"""
from __future__ import annotations

import base64
import json

# "binary"(기본, Railway 프로브 PASS 시) | "base64"(프록시가 binary 차단 시 폴백)
TRANSPORT = "binary"


def extract_audio(message: dict) -> bytes | None:
    """
    Starlette `websocket.receive()` 결과 dict에서 발화 오디오 bytes를 추출.
    오디오 프레임이 아니면 None.

    - binary 모드: 바이너리 프레임의 raw bytes.
    - base64 모드: text 프레임 {"type":"audio","data":"<base64 webm>"}.
    """
    if TRANSPORT == "binary":
        return message.get("bytes")

    text = message.get("text")
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if obj.get("type") == "audio" and isinstance(obj.get("data"), str):
        try:
            return base64.b64decode(obj["data"])
        except (ValueError, TypeError):
            return None
    return None


def parse_control(message: dict) -> dict | None:
    """
    제어 메시지(text JSON) 파싱: {"type":"control","action":...}.
    오디오 프레임이거나 제어가 아니면 None. (binary/base64 모드 공통)
    """
    text = message.get("text")
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if obj.get("type") == "control" else None
