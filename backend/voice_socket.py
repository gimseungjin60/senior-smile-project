"""
[스레드↔async 경계 격리 모듈] voice_agent(스레드 루프) ↔ /ws/voice(async) 브리지.
(docs/voice-cloud-refactor-design.md §2.5)

P2에서 가장 깨지기 쉬운 지점이라 main.py에서 분리해 단위 테스트 가능하게 격리한다.
- 인바운드: /ws/voice가 받은 오디오 bytes → voice_agent.audio_in 큐 (feed_audio)
- 아웃바운드: voice_agent **스레드**가 audio_out(payload) 호출 → run_coroutine_threadsafe로
  이벤트 루프에 넘겨 ws.send_json. (스레드에서 직접 await 불가 — 이 배선이 어긋나면
  "메시지 안 감 / event loop 에러"로 터짐)
- 단일 연결(§2.5 동시성): attach 시 이전 연결 닫고 큐·상태 리셋. 시니어 기기 1대 = ws 1.
"""
from __future__ import annotations

import asyncio
import queue
import logging

logger = logging.getLogger(__name__)


class VoiceSocketBridge:
    def __init__(self):
        self.ws = None            # 현재 활성 /ws/voice 연결 (단일)
        self.loop = None          # 그 연결이 속한 asyncio 이벤트 루프
        self.agent = None         # VoiceAgent (audio_in/playback_done/audio_out 보유)

    def bind_agent(self, agent):
        """voice_agent 생성/교체 시 호출 — audio_out을 이 브리지의 send로 연결."""
        self.agent = agent
        if agent is not None:
            agent.audio_out = self.send

    def send(self, payload: dict):
        """voice_agent **스레드**에서 호출됨. 이벤트 루프로 넘겨 ws.send_json 실행.
        전송 완료까지 블록(스레드라 안전) → 실패 시 즉시 알 수 있어 half-duplex 무한대기 방지."""
        ws, loop = self.ws, self.loop
        if ws is None or loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)
            fut.result(timeout=5)
        except Exception as e:
            logger.warning(f"[voice] audio_out 송신 실패: {e}")

    async def attach(self, ws, loop):
        """새 /ws/voice 연결. 단일 연결 가드: 이전 연결 닫고 큐/상태 리셋."""
        old = self.ws
        self.ws = ws
        self.loop = loop
        self._drain()
        if self.agent is not None:
            self.agent.audio_out = self.send
            self.agent.playback_done.set()  # 이전 재생 대기가 있었다면 깨움
        if old is not None and old is not ws:
            try:
                await old.close()
            except Exception:
                pass

    def detach(self, ws):
        """현재 연결이 끊겼을 때. 큐 비우고 재생 대기 해제(루프 멈춤 방지)."""
        if self.ws is ws:
            self.ws = None
            self.loop = None
            if self.agent is not None:
                self.agent.playback_done.set()
            self._drain()

    def feed_audio(self, audio_bytes: bytes):
        """수신한 발화 오디오를 voice_agent 입력 큐로."""
        if self.agent is not None and audio_bytes:
            self.agent.audio_in.put(audio_bytes)

    def signal_playback_done(self):
        """클라가 재생 완료(playback_done) → half-duplex 대기 해제."""
        if self.agent is not None:
            self.agent.playback_done.set()

    def _drain(self):
        """입력 큐의 stale 프레임 제거 (재연결 시 이전 연결 오디오가 안 섞이게)."""
        if self.agent is None:
            return
        try:
            while True:
                self.agent.audio_in.get_nowait()
        except queue.Empty:
            pass
