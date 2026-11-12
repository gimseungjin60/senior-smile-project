"""
P2 격리 단위 테스트 — voice_socket.VoiceSocketBridge (스레드↔async 경계).
main.py(cv2/firebase) 없이 브리지만 검증.
실행: cd backend && python scripts/test_p2_voice_socket.py

핵심 검증(가장 깨지기 쉬운 지점):
  1) send()를 **voice_agent 스레드(이벤트 루프와 다른 스레드)**에서 불러도
     run_coroutine_threadsafe로 실제 ws.send_json이 실행되는가
  2) bind_agent가 agent.audio_out을 브리지 send로 연결하는가 (스레드→WS 일관)
  3) feed_audio → agent.audio_in 큐 적재 / signal_playback_done → Event set
  4) 단일 연결 가드: 새 연결 attach 시 이전 ws.close() + 입력 큐 리셋 + ws 교체
"""
import sys
import queue
import asyncio
import threading
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from voice_socket import VoiceSocketBridge  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "—", name)
    if not cond:
        failures.append(name)


class FakeWS:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self.closed = False

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


class FakeAgent:
    """브리지가 건드리는 최소 인터페이스만."""
    def __init__(self):
        self.audio_in = queue.Queue()
        self.playback_done = threading.Event()
        self.audio_out = None


# 이벤트 루프를 별도 스레드에서 가동 (= /ws/voice가 사는 async 루프 시뮬레이션)
loop = asyncio.new_event_loop()
loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
loop_thread.start()


def run_on_loop(coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=5)


bridge = VoiceSocketBridge()
agent = FakeAgent()
bridge.bind_agent(agent)

# ── 2) bind_agent가 audio_out 연결 ──
# 바운드 메서드는 접근마다 새 객체 → is 대신 ==(=__self__·__func__ 비교)
check("bind_agent가 agent.audio_out을 브리지 send로 연결", agent.audio_out == bridge.send)

# ── 1) 스레드→async 경계: 다른 스레드에서 send() 호출 → 실제 ws.send_json 실행 ──
ws1 = FakeWS("ws1")
run_on_loop(bridge.attach(ws1, loop))  # attach는 async (이벤트 루프에서 실행)
check("attach 후 bridge.ws == ws1", bridge.ws is ws1)

sent_from_thread = {"ok": False}


def worker_thread_emit():
    # voice_agent 스레드가 audio_out(payload) 부르는 상황 재현 (루프 스레드와 다른 스레드)
    agent.audio_out({"type": "speak", "url": "/tts/latest"})
    sent_from_thread["ok"] = True


th = threading.Thread(target=worker_thread_emit)
th.start()
th.join(timeout=5)
check("다른 스레드의 send() 호출이 블록 없이 반환", sent_from_thread["ok"] is True)
check("스레드→async 브리지로 ws.send_json 실제 실행됨", len(ws1.sent) == 1)
check(
    "전송된 payload 내용 일치",
    bool(ws1.sent) and ws1.sent[0] == {"type": "speak", "url": "/tts/latest"},
)
check(
    "루프 스레드와 워커 스레드가 실제로 다름(경계 검증 유효)",
    loop_thread.ident != th.ident,
)

# ── 3) feed_audio / signal_playback_done ──
bridge.feed_audio(b"UTTERANCE_1")
got = None
try:
    got = agent.audio_in.get_nowait()
except queue.Empty:
    pass
check("feed_audio가 agent.audio_in 큐에 적재", got == b"UTTERANCE_1")

agent.playback_done.clear()
bridge.signal_playback_done()
check("signal_playback_done가 playback_done Event set", agent.playback_done.is_set())

# ── 4) 단일 연결 가드: 새 연결 attach → 이전 ws close + 큐 리셋 + ws 교체 ──
agent.audio_in.put(b"STALE_1")
agent.audio_in.put(b"STALE_2")
ws2 = FakeWS("ws2")
run_on_loop(bridge.attach(ws2, loop))
check("새 연결 후 bridge.ws == ws2", bridge.ws is ws2)
check("이전 연결 ws1.close() 호출됨", ws1.closed is True)
check("attach가 이전 연결의 stale 입력 큐를 비움", agent.audio_in.empty())

# 새 연결로 send 되는지(다른 스레드)
ws2_done = {"ok": False}


def worker2():
    agent.audio_out({"type": "beep", "url": "/sounds/beep.wav"})
    ws2_done["ok"] = True


th2 = threading.Thread(target=worker2)
th2.start()
th2.join(timeout=5)
check("교체 후 새 ws2로 송신", ws2_done["ok"] and len(ws2.sent) == 1 and ws2.sent[0]["url"] == "/sounds/beep.wav")

# ── detach: 끊긴 뒤 ws None + 재생 대기 해제 ──
agent.playback_done.clear()
bridge.detach(ws2)
check("detach 후 bridge.ws is None", bridge.ws is None)
check("detach가 playback_done set(루프 멈춤 방지)", agent.playback_done.is_set())

loop.call_soon_threadsafe(loop.stop)

print()
if failures:
    print(f"❌ {len(failures)}건 실패: {failures}")
    raise SystemExit(1)
print("✅ P2 브리지(스레드↔async + 단일연결) 전부 통과")
raise SystemExit(0)
