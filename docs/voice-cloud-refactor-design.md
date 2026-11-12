# 음성 파이프라인 클라우드 재설계 설계안

> 목적: `voice_agent.py`의 **로컬 마이크/스피커 의존**을 제거하고, 오디오 I/O를 갤탭 브라우저로 옮겨 **클라우드(Railway) 배포 가능**하게 만든다.
> 원칙: STT(Whisper API)·GPT·TTS 생성·호출어 매칭 등 **서버 로직은 최대한 보존**, 양 끝단(입력 마이크 / 출력 스피커)만 브라우저로 이전. (비전 이전과 동일 원칙)

## 1. 현재 구조 (RPi 가정)

```
voice_agent._run_loop (서버 스레드)
  with sr.Microphone():                 # ← 서버 로컬 마이크
    recognizer.listen(source)           # VAD로 발화 녹음
    _transcribe_audio(audio)            # OpenAI Whisper API  ✅ 클라우드 OK
    _is_wake_word / GPT / 감정분석       #                     ✅ 클라우드 OK
    speak() → pygame.mixer.music.play() # ← 서버 로컬 스피커
상태(자막/듣기중 등)는 /ws 가 voice_agent 속성을 폴링해 프론트로 일방향 브로드캐스트
```

**클라우드 불가 지점:** `sr.Microphone()`(입력), `pygame.mixer.music`(출력) — 서버에 장치 없음.

## 2. 목표 구조 (클라우드)

```
[갤탭 브라우저]                         [클라우드 서버]
 mic getUserMedia + VAD ──(binary audio)──▶ 오디오 큐 → _transcribe_audio(Whisper)
                                              → 호출어/GPT/감정분석 (기존 로직 그대로)
                                              → TTS mp3 생성 (기존)
 <audio> 재생  ◀──({type:'speak',url})──────  speak()가 재생 대신 url 송신
 재생완료 ─────({action:'playback_done'})──▶  서버 "재생중" 해제 → 다음 입력 수신
 자막/상태   ◀────────(/ws 기존 그대로)──────  voice_agent.current_subtitle 등
```

## 2.5 상태 머신 & 동시성 (골격 — 코딩 전 확정)

> 에코 방지와 동시성 매핑은 "나중에 붙이는 기능"이 아니라 **서버 루프 상태 설계 그 자체**다.
> P1(서버 송신 구조)와 P3(프론트 VAD 제어)가 여기서 맞물리므로 코딩 전에 골격으로 확정한다.

### 에코 방지 = half-duplex (확정)
재생(TTS) 중에는 입력(VAD/마이크)을 정지한다. AEC(에코 캔슬)는 쓰지 않는다.
**근거:** 노인 대상·단일 기기·동시 발화가 거의 없음 → 풀듀플렉스/AEC 복잡도 불필요.

voice_agent는 명시적 3-상태로 동작:

```
        utterance(wake/conv)         response ready → {type:'speak'}
LISTENING ─────────────────▶ THINKING ─────────────────────────▶ SPEAKING
   ▲                                                                 │
   └──────────────────── playback_done ◀─────────────────────────────┘
   (재생 끝나야 입력 재개)
```

- **LISTENING**: 오디오 큐 소비 → Whisper → 호출어/대화 처리.
- **THINKING**: GPT/감정분석 중. 입력 무시(큐 유입분 드롭).
- **SPEAKING**: 클라이언트가 TTS 재생 중. 서버 `is_speaking=True` → **큐 유입 오디오 전량 드롭**, `playback_done` 수신까지 대기(threading.Event).

**이중 방어(둘 다 필수):**
1. *클라이언트* — `{type:'speak'}`/`{type:'beep'}` 수신 시 VAD/마이크 캡처 **즉시 정지**, 재생 `ended`에서 `playback_done` 송신 후 캡처 재개.
2. *서버* — `is_speaking` 동안 큐 입력 무시. 클라가 정지 못 해 새어 들어온 프레임도 서버에서 차단.

→ P1에서 `is_speaking` 플래그 + `playback_done` Event를 **루프 골격으로** 넣는다. P3는 이 신호에 맞춰 캡처만 토글. (나중에 들어낼 일 없음)

### 동시성/매핑 (확정)
**전제: 시니어 기기 1대 = voice_agent 인스턴스 1 = `/ws/voice` 연결 1.** 다대다(N:M)는 보호자↔시니어 관계의 얘기지, 한 시니어 기기의 음성 세션은 항상 단일이다.

- `/ws/voice`는 **단일 활성 연결**만 허용. 새 연결이 오면 이전 연결을 닫고 큐·상태를 리셋(재연결/새로고침 대비).
- 오디오 큐는 그 단일 연결에 귀속. **연결 종료 시 큐 비우고 상태를 LISTENING으로 초기화**, `is_active=False`로 루프 idle.
- → P2 큐 배선의 불변식: "큐에는 현재 활성 연결의 오디오만 들어있다." 재연결 시 stale 프레임이 남지 않음.

## 3. WebSocket 프로토콜

기존 `/ws`(앱 상태: status/pairing/reminder/subtitle/isListening...)는 **변경 없음**.
신규 **`/ws/voice`** (양방향) 추가 — 오디오 전송 전용.

### Client → Server
| 메시지 | 형식 | 의미 |
|--------|------|------|
| 오디오 발화 | **binary** (audio/webm;opus) | VAD가 끊은 발화 1건. 서버 오디오 큐로 push |
| `{type:'control', action:'mic_ready'}` | JSON | 마이크 권한 획득·캡처 시작 |
| `{type:'control', action:'playback_done'}` | JSON | 브라우저 TTS 재생 완료 → 서버 listen 재개 |

### Server → Client
| 메시지 | 형식 | 의미 |
|--------|------|------|
| `{type:'speak', url:'/voice_messages/xxx.mp3'}` | JSON | 이 오디오를 재생하라 (TTS/사진알림/보호자 음성메시지). **수신 즉시 클라 VAD 정지(half-duplex)** |
| `{type:'beep'}` | JSON | 호출어 인식 ack 효과음 (또는 url). 마찬가지로 재생 동안 VAD 정지 |
| `{type:'stt', text:'...'}` | JSON | 인식된 사용자 발화 (디버그/즉시 자막용, 선택) |

> half-duplex 규약: 클라이언트는 `speak`/`beep` 수신 → VAD/캡처 정지, 해당 오디오 `ended` → `{action:'playback_done'}` 송신 후 캡처 재개. 별도 `mic_pause` 메시지 불필요(speak/beep 자체가 정지 트리거).

> 자막(`subtitle`)·`isListening`·`isConversationActive` 등 화면 상태는 기존 `/ws`로 계속 흐름 → **App.jsx/SubtitleBar 수정 최소화**.

## 4. 서버 변경 (voice_agent.py)

- 의존성 제거: `import pygame`, `import speech_recognition as sr` 삭제. requirements에서 `PyAudio`, `pygame`, (서버 비전 제거분) `opencv-python`, `mediapipe` 정리.
- `_run_loop`: `with sr.Microphone()` 제거. 루프 구조는 유지하되 입력만 교체:
  - `audio = recognizer.listen(source)` → `audio_bytes = self._audio_queue.get(timeout=1.0)` (thread-safe queue, `/ws/voice` 수신 핸들러가 push)
  - 큐 empty 타임아웃 = 기존 `WaitTimeoutError` 자리 (continue)
- `_transcribe_audio(audio)` → `_transcribe_audio(audio_bytes, fmt='webm')`: `audio.get_wav_data()` 대신 받은 bytes를 temp 파일로 저장 후 Whisper 호출 (Whisper API가 webm/opus 직접 수용 → 변환 불필요).
- `speak()/play_sound()/_play_beep()/_play_voice_message()`: `pygame...play()` 대신 `/ws/voice` 클라이언트에 `{type:'speak', url}`/`{type:'beep'}` 송신. 재생 완료 대기(`while mixer.get_busy()`)는 **`playback_done` 신호 대기**(threading.Event)로 대체.
- 마이크 재시도/`OSError` 처리, ambient noise 보정(`adjust_for_ambient_noise`) 등 로컬 장치 코드 제거.

## 5. 프론트 변경 (senior-client)

신규 `frontend/src/audio/useVoiceClient.js` (훅):
- `/ws/voice` 연결.
- 마이크: **Silero VAD(@ricky0123/vad-web)**가 마이크 캡처+발화 구간 분할을 담당 → `onSpeechEnd(audio)`에서 `utils.encodeWAV(audio)`로 16kHz mono WAV 인코딩해 binary 전송. (MediaRecorder 타이밍 조율 불필요, 발화 경계 정확)
- 수신 `{type:'speak',url}` → `new Audio(url)` 재생, **재생 중 마이크 캡처 일시중지(에코 방지)**, `ended`에서 `{action:'playback_done'}` 송신.
- `{type:'beep'}` → 효과음 재생.
- App.jsx: 페어링 완료 시 훅 마운트. 자막/상태는 기존 `/ws` 구독 그대로.

## 6. 결정 사항 (확정 2026-06-02)

1. **VAD 방식** — ✅ **`@ricky0123/vad-web`(Silero VAD wasm)** 사용. 노인 발화·소음 환경 견고성 우선.
2. **호출어 감지 위치** — ✅ **서버 유지**. 브라우저는 VAD로 끊은 발화를 모두 서버로 전송 → 서버 Whisper STT 후 기존 텍스트 매칭 로직(`_is_wake_word`) 그대로. (대기 중 Whisper 비용은 감수, 추후 Web Speech 1차 필터 최적화 여지)
3. **얼굴 게이팅** — ✅ **일단 제거**. 대화 화면 활성 시 항상 listen. `voice_agent.face_detected` 게이팅 코드 및 main.py 카메라→마이크 연동 제거. 필요 시 추후 브라우저 FaceDetector로 재도입.
4. **오디오 포맷** — ✅ **WAV 16kHz mono** (vad-web `utils.encodeWAV`). webm/opus(MediaRecorder) 대신 채택 — VAD가 발화를 Float32로 끊어주므로 encodeWAV가 가장 단순·정확, Whisper API가 wav 직접 수용. 서버 `_transcribe_audio(fmt="wav")`.
5. **에코 방지** — ✅ **half-duplex** (재생 중 VAD 정지). AEC 미사용. → §2.5 상태 머신. *P1 골격에 포함.*
6. **동시성/매핑** — ✅ **시니어 기기 1대 = voice_agent 1 = /ws/voice 1**, 단일 활성 연결. → §2.5. *P2 큐 불변식.*
7. **발화 끊김(무음) 타임아웃** — 구조 아닌 튜닝값. 기본 **무음 1.2~1.5초 = 발화 종료**로 두고 P1 진행, 실측(노인 음성)으로 조정. (vad-web `redemptionFrames`/`minSpeechFrames`로 매핑)

### Railway 사전 확인 (P1과 독립, 아무 때나 ~30분)
- WebSocket **binary 프레임** 정상 통과(프록시가 안 끊는지)
- **단일 인스턴스**(min=max=1) 구성 — 인프로세스 큐/스케줄러/단일연결 전제와 일치
- 위 2개만 확인되면 P4 배포 리스크 제거.

## 7. 영향 파일 체크리스트
- `backend/voice_agent.py` (대수술), `backend/main.py`(`/ws/voice` 핸들러 + 오디오 큐 배선)
- `backend/requirements.txt`(불필요 의존성 제거)
- `frontend/src/audio/useVoiceClient.js`(신규), `frontend/src/App.jsx`(훅 마운트)
- 배포: `Dockerfile` 또는 Railway Nixpacks 설정(오디오 시스템 라이브러리 불필요해짐 → 이미지 경량화)
