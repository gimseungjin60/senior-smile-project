"""
P1 격리 단위 테스트 — main.py 없이 voice_agent 한 사이클 검증.
(docs/voice-cloud-refactor-design.md §2.5 half-duplex 골격)

heavy deps(OpenAI/Firebase)는 mock으로 차단, Whisper는 instance 메서드 mock.
실행: cd backend && python scripts/test_p1_voice_cycle.py

검증:
  1) audio_in 큐 → listen()이 발화 bytes를 꺼내 STT로 전달하고 결과 반환
  2) speak()가 audio_out 콜백 호출({type:'speak', url:'/tts/latest'})
  3) playback_done.set() 이 _play_and_wait 대기를 깨워 다음으로 진행
  4) is_speaking 동안 인입 발화는 드롭(에코 방지) + Whisper 미호출
"""
import sys
import time
import threading
import pathlib
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# OpenAI/Firebase 차단하고 VoiceAgent 생성 (네트워크/스냅샷 리스너 방지)
with mock.patch("voice_agent.OpenAI"), \
     mock.patch("voice_agent.firebase_admin"), \
     mock.patch("voice_agent.credentials"), \
     mock.patch("voice_agent.firestore"):
    import voice_agent
    agent = voice_agent.VoiceAgent()

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "—", name)
    if not cond:
        failures.append(name)


agent.is_active = True
agent.is_running = True
agent.is_speaking = False

# ── 1) listen()이 큐를 소비하고 STT(mock)로 bytes 전달 ──
seen = {}


def fake_stt(b, fmt="webm"):
    seen["bytes"] = b
    return "안녕하세요"


agent._transcribe_audio = fake_stt
agent.audio_in.put(b"FAKE_AUDIO_1")
txt = agent.listen()
check("listen()가 큐 발화를 꺼내 STT 결과 반환", txt == "안녕하세요")
check("listen()이 큐의 실제 bytes를 STT로 전달", seen.get("bytes") == b"FAKE_AUDIO_1")

# ── 2) speak()가 audio_out 호출 (클라 재생완료 즉시 시뮬레이션) ──
emitted = []


def out_autodone(payload):
    emitted.append(payload)
    agent.playback_done.set()  # 클라가 재생 끝내고 playback_done 보낸 것으로 시뮬


agent.audio_out = out_autodone
agent.speak("테스트 발화")
check("speak()가 audio_out 콜백 호출", len(emitted) >= 1)
check(
    "speak() payload가 type=speak, url=/tts/latest",
    bool(emitted) and emitted[-1].get("type") == "speak" and emitted[-1].get("url") == "/tts/latest",
)
check("speak() 종료 후 is_speaking 해제", agent.is_speaking is False)

# ── 3) playback_done.set()이 _play_and_wait 대기를 깨우는지 (별도 스레드) ──
agent.is_active = True
woke = {"done": False}


def out_noset(payload):
    pass  # 콜백은 set 안 함 → 외부에서 set 해야 깨어남


agent.audio_out = out_noset
agent.playback_done.clear()


def run_speak():
    agent.speak("대기 테스트")
    woke["done"] = True


t = threading.Thread(target=run_speak)
t.start()
time.sleep(0.3)
check(
    "set() 전에는 _play_and_wait 대기 중(is_speaking=True)",
    woke["done"] is False and agent.is_speaking is True,
)
agent.playback_done.set()
t.join(timeout=3.0)
check("playback_done.set() 후 _play_and_wait 진행", woke["done"] is True)

# ── 4) is_speaking 동안 인입 발화 드롭(에코 방지) ──
agent.is_active = True
agent.is_speaking = True
called = {"stt": False}


def stt_should_not_run(b, fmt="webm"):
    called["stt"] = True
    return "should-not-happen"


agent._transcribe_audio = stt_should_not_run
agent.audio_in.put(b"DURING_SPEAK")
dropped = agent.listen()
check("재생 중 인입 발화는 listen()에서 드롭(빈 문자열)", dropped == "")
check("재생 중 Whisper 미호출(에코 방지)", called["stt"] is False)

print()
if failures:
    print(f"❌ {len(failures)}건 실패: {failures}")
    raise SystemExit(1)
print("✅ P1 한 사이클 전부 통과")
raise SystemExit(0)
