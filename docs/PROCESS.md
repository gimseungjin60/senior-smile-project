# Senior-Smile — 전체 프로세스 / 아키텍처 문서

> AI 손주(앨범이) 페르소나 기반 독거 시니어 정서 케어 시스템.
> 갤럭시탭 액자(시니어) + 아이폰 보호자 앱 + 클라우드.

---

## 1. 한 줄 소개

독거 어르신용 **AI 액자**. 어르신은 "앨범아" 하고 부르면 7살 손주 AI와 **실시간 음성 대화**를 하고,
가위바위보·스트레칭 같은 **활동**을 하며, **복약 알림**을 받는다.
보호자는 폰에서 어르신의 **활동/감정 리포트**를 보고, **사진**을 보내고, **실시간 카메라**로 안부를 확인한다.

---

## 2. 3계층 구조

```
┌─────────────────────────┐     ┌──────────────────────────┐     ┌─────────────────────────┐
│   시니어 디바이스          │     │        클라우드            │     │      보호자 앱            │
│   (갤럭시탭 / 웹브라우저)   │     │                          │     │   (아이폰 / RN·Expo)      │
│                         │     │  Render (FastAPI/Docker)  │     │                         │
│  React/Vite 디지털 액자   │◄───►│  - 토큰발급/페어링/스케줄러  │◄───►│  로그인·대시보드          │
│  - OpenAI Realtime(음성) │ WS  │  - Firestore 리스너        │ FS  │  페어링·사진·카메라·리포트 │
│  - MediaPipe(비전 게임)   │     │  - LiveKit 송출/리포트 API │     │                         │
│  - 태블릿 카메라 송신      │     │                          │     │                         │
└───────────┬─────────────┘     │  Firebase                │     └───────────┬─────────────┘
            │                   │  - Auth / Firestore       │                 │
            │ WebRTC(음성)       │  - Cloud Functions        │                 │ LiveKit(영상)
            ▼                   │    (verifyPairing,        │                 ▼
      OpenAI Realtime           │     getLiveKitToken)      │           LiveKit Cloud
                                └──────────────────────────┘
```

1. **시니어 디바이스** — 갤럭시탭 웹브라우저. React/Vite 프론트(디지털 액자 UI).
2. **클라우드** — Render(FastAPI 백엔드) + Firebase + OpenAI + LiveKit.
3. **보호자 앱** — 아이폰. React Native/Expo.

---

## 3. 구성요소 상세

### 3-1. 시니어 프론트엔드 (React + Vite)
| 기능 | 구현 |
|---|---|
| 음성 대화 | **OpenAI Realtime API** — 브라우저↔OpenAI 직접 WebRTC (`useRealtimeClient`) |
| 비전 활동 | **MediaPipe Web** — 가위바위보(HandLandmarker), 스트레칭(PoseLandmarker) (`vision.js`) |
| 캔드 음원 | 복약/인사 등 사전녹음 mp3 재생 (`useVoiceClient`, playbackOnly) |
| 태블릿 카메라 | 카메라 프레임을 `/ws/media`로 백엔드 송신 (`MediaBridge`) → 얼굴감지·LiveKit 송출 소스 |
| 화면 | idle(디지털 액자) → greeting → active 상태머신, 리마인더·사진·미디어 오버레이 |

### 3-2. 백엔드 (FastAPI · Render · Docker)
| 기능 | 구현 |
|---|---|
| Realtime 토큰 | `/api/realtime/token` — ephemeral 발급(API키 비노출). 페르소나·도구·검색 정보 세션에 포함 |
| 실데이터 도구 | `/api/realtime/weather` (OpenWeather) — Realtime function calling용 |
| 페어링 | 6자리 핀 생성 → Firestore `pairing_requests`. 검증은 Cloud Function `verifyPairing`(원자 트랜잭션) |
| 복약 스케줄러 | APScheduler 매분 → Firestore `medications` 확인 → 시간 맞으면 시니어 알림+음성, 10분 미복용 시 보호자 푸시 |
| 실시간 리스너 | Firestore 구독(`pairing_requests`/`devices`/`photos`) → 시니어에 WebSocket 푸시 |
| 얼굴감지 | 태블릿 카메라 프레임(`/ws/media`) → 서버 DNN 얼굴감지 → 세션 시작/종료 이벤트 → 리포트 |
| 실시간 영상 | **LiveKit Publisher** — 보호자 카메라 요청 시 태블릿 카메라를 LiveKit 방(`device-{id}`)에 송출 |
| 리포트 API | `/api/reports/summary` — 방문/체류시간/감정/복약 집계 |

### 3-3. 보호자 앱 (React Native / Expo · iOS)
- Firebase Auth 로그인 → 페어링(핀 입력, `verifyPairing` 호출)
- 대시보드(오늘 활동·기분·복약·주간차트), 사진 전송(`photos` 컬렉션), 복약 설정(`medications`)
- 실시간 카메라 보기(LiveKit, `getLiveKitToken` → 방 구독) — **네이티브 빌드 필요**(웹은 미지원)

### 3-4. 클라우드 서비스
- **Firebase**: Auth(보호자), Firestore(페어링/사진/복약/세션/이벤트), Cloud Functions(`verifyPairing`, `getLiveKitToken`, `unpairDevice`, `cleanupExpiredPins`)
- **OpenAI**: Realtime(`gpt-realtime`) 음성 대화
- **LiveKit Cloud**: 실시간 영상 중계(WebRTC SFU)
- **Render**: 백엔드 + 프론트 빌드(dist) 동일 출처 https 서빙

---

## 4. 실시간 통신 경로

| 구간 | 방식 | 용도 |
|---|---|---|
| 시니어 ↔ 백엔드 | WebSocket `/ws/{id}` | 상태(idle/active)·페어링·리마인더·사진 알림 |
| 시니어 ↔ 백엔드 | WebSocket `/ws/voice/{id}` | 캔드음원(복약/인사) 재생 지시 |
| 시니어 ↔ 백엔드 | WebSocket `/ws/media/{id}` | 태블릿 카메라 프레임 업로드 |
| 시니어 ↔ OpenAI | **WebRTC** | 실시간 음성 대화(서버 미경유) |
| 시니어 카메라 → 보호자 | **LiveKit(WebRTC)** | 실시간 영상 |
| 보호자 ↔ 백엔드 | Firestore 실시간 구독 + REST | 데이터·리포트 |

---

## 5. 기능별 데이터 흐름 (= 데모 시나리오)

### ① 페어링
```
시니어: 6자리 핀 화면 표시 (pairing_requests/{code} 생성)
보호자: 핀 입력 → Cloud Function verifyPairing (원자 트랜잭션) → devices/{id}.pairedUids 추가
→ 양쪽 실시간 리스너가 "연결됨" 수신
```

### ② 음성 대화 (메인)
```
어르신: "앨범아, 심심해"
→ 브라우저 마이크 → OpenAI Realtime(WebRTC) → 7살 손주 응답(음성)
→ "가위바위보 하자" → 모델이 start_game 도구 호출 → 게임 화면 전환
→ "트로트 틀어줘" → play_media 도구 → 유튜브 재생
→ "오늘 날씨 어때?" → get_weather 도구 → 실제 날씨로 응답
```

### ③ 복약 알림
```
보호자: 복약 시간 설정 (Firestore medications)
→ 백엔드 스케줄러가 시간 도달 감지
→ 시니어: ReminderScreen + pill_remind.mp3 (재생 중 Realtime/미디어는 자동 정지·덕킹)
→ 어르신: "약 먹었어" → 칭찬 음성 + Firestore 기록 → 보호자 대시보드 반영
→ 10분 미복용 시 보호자에게 푸시
```

### ④ 사진 전송
```
보호자: 사진 업로드 (Firestore photos/{...}, deviceId)
→ 백엔드 PhotosListener 감지 → 해당 시니어에 WebSocket 푸시
→ 시니어: "가족이 사진을 보냈어요" 풀스크린 표시
```

### ⑤ 실시간 영상
```
보호자: 카메라 보기 토글 (devices/{id}.cameraRequested=true)
→ 백엔드 DevicesListener → LiveKit Publisher.enable() → 태블릿 카메라를 방에 송출
→ 보호자: getLiveKitToken → 같은 방 구독 → 실시간 영상
```

### ⑥ 활동/감정 리포트
```
얼굴감지 → 세션 시작/종료 → 체류시간·방문 기록 (detection_events)
대화 종료 → 감정 분석 → sessions 저장
→ 보호자: /api/reports/summary → 대시보드(방문/기분/감정분포/주간차트)
```

---

## 6. 기술 선택 이유

- **OpenAI Realtime API**: 기존 STT(Whisper)→LLM→TTS 직렬 구조는 턴당 3~7초 지연. speech-to-speech로 **자연스러운 실시간 대화** 실현. 오디오가 브라우저↔OpenAI 직접이라 **Render 서버 부하 거의 없음**.
- **브라우저 비전/음성**: 클라우드(Render)엔 카메라/마이크가 없으므로, 디바이스(브라우저)에서 직접 처리 → **클라우드 배포 대응**.
- **Render 단일 URL**: 프론트 빌드(dist)와 백엔드를 동일 출처로 https 서빙 → 갤탭에서 한 주소로 접속, mixed-content 회피.
- **Firebase**: 페어링·실시간 데이터·인증을 서버리스로. 보호자/시니어가 다른 네트워크여도 Firestore로 연결.

---

## 7. 기술 스택

| 영역 | 기술 |
|---|---|
| 시니어 프론트 | React 19, Vite, @ricky0123/vad-web, @mediapipe/tasks-vision |
| 음성 | OpenAI Realtime API (gpt-realtime), WebRTC |
| 백엔드 | Python, FastAPI, APScheduler, OpenCV, firebase-admin, livekit |
| 보호자 앱 | React Native, Expo, @livekit/react-native, Firebase JS SDK |
| 클라우드 | Render(Docker), Firebase(Auth/Firestore/Functions), OpenAI, LiveKit Cloud, OpenWeather |

---

## 8. 알려진 한계 / 비고 (발표 시 참고)

- **리포트 감정/대화 데이터**: 대화를 Realtime로 옮기면서 voice_agent를 안 거쳐, 감정분석·대화수 집계가 비어있을 수 있음(개선 예정).
- **실시간 영상**: LiveKit 키(백엔드/Cloud Function 양쪽) + 보호자 네이티브 빌드 + 같은 device_id가 모두 맞아야 동작.
- **호출어 "앨범아"**: Realtime instructions 기반(소프트 게이트). 하드 키워드 스포팅 아님.
- **음성 비용**: Realtime은 분당 과금 → OpenAI 크레딧 관리 필요.
