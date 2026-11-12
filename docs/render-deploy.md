# 시니어 백엔드 — Render Docker 배포 가이드

> 시니어 서버(`backend/`, FastAPI)를 Render에 Docker로 올리는 절차.
> 프론트(`frontend/`)는 Firebase Hosting 등 정적 호스팅으로 별도 배포(이 문서 범위 밖).

## 현재 상태 (배포 전 알아둘 것)

| 기능 | 클라우드 동작 |
|------|---------------|
| 앱 부팅 / `/health` | ✅ |
| 페어링 (Firestore) | ✅ |
| 복약·일정 리마인더 (스케줄러 → 브라우저 TTS) | ✅ |
| 브라우저 마이크 → `/ws/voice` 오디오 | ✅ |
| **일반 음성 대화 자동 시작** | ❌ **아직 안 됨 (P5)** |
| 시니어 영상 LiveKit 송출(서버 카메라) | ❌ (서버에 카메라 없음) |

- **P5 미완**: `main.py` 의 세션 트리거가 아직 *서버 로컬 카메라 얼굴감지* 기반이라(`App.jsx:37` 주석),
  Render에는 카메라가 없어 `greeting→active` 전환이 일어나지 않음 → 일반 대화가 자동 시작되지 않는다.
  이 항목은 별도 작업으로 진행 예정. (배포 자체는 가능, 위 ✅ 기능들은 동작)

## 1. 서비스 생성

- Render 대시보드 → **New → Web Service** → 이 GitHub 저장소 연결
- **Runtime: Docker** (Dockerfile Path = `./Dockerfile`, Root Directory = 비움/루트)
- **Instance: 단일 인스턴스 (autoscaling OFF, instance count = 1)**
  - 인프로세스 큐 / APScheduler / `/ws/voice` 단일 활성 연결 전제(설계 §6.6). **반드시 1대.**
- **Health Check Path: `/health`**
- WebSocket(`/ws`, `/ws/voice`)은 Render에서 추가 설정 없이 통과.

## 2. 환경변수 (Environment)

`PORT` 는 Render가 자동 주입하므로 **설정하지 말 것.**

| 키 | 필수 | 설명 |
|----|------|------|
| `DEVICE_ID` | **필수** | 디바이스 고정 ID. 미설정 시 컨테이너가 매 배포마다 새 ID를 생성해 **페어링이 깨진다.** 예: `frame-prod01`. 한 번 정하면 바꾸지 말 것 |
| `SERVICE_ACCOUNT_KEY_JSON` | **필수** | Firebase 서비스계정 키(`serviceAccountKey.json`)의 **전체 JSON 내용**을 그대로 붙여넣기. 기동 시 파일로 복원됨. (대안: 아래 Secret Files) |
| `OPENAI_API_KEY` | **필수** | Whisper STT + GPT 대화 + TTS |
| `WEATHER_API_KEY` | 선택 | OpenWeatherMap. 없으면 날씨 카드 숨김 |
| `WEATHER_CITY` | 선택 | 기본 `Seoul` |
| `NIGHT_START` / `NIGHT_END` | 선택 | 야간모드, 기본 `22:00` / `07:00` |
| `AIBUM_BACKEND_URL` | 선택 | AI-bum 백엔드 이벤트 전송 대상, 기본 `http://localhost:8001` |
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | 선택 | 셋 다 있어야 LiveKit publisher 활성. (단, 서버 카메라가 없어 현재 송출할 프레임 없음 → 사실상 비활성) |

### SERVICE_ACCOUNT_KEY_JSON 대안: Render Secret Files
env에 거대 JSON을 붙이기 싫으면 Render **Secret Files** 로
`serviceAccountKey.json` 을 마운트 경로 `/app/serviceAccountKey.json` 에 등록해도 된다.
(이 경우 `SERVICE_ACCOUNT_KEY_JSON` env는 생략)

## 3. 배포 & 확인

1. env 입력 후 **Create / Deploy**.
2. 빌드 로그에서 `pip install` 통과 확인.
3. 배포 완료 후:
   - `https://<your-service>.onrender.com/health` → `{"status": "ok"|"degraded", ...}` 응답
   - 프론트의 시니어 서버 주소(`frontend/src/utils/host.js`)를 이 URL로 맞추고 페어링 테스트.

## 4. 알려진 제약 (휘발성 파일시스템)

Render 기본 인스턴스는 디스크가 휘발성이라 **로컬 파일에 쓰는 상태는 재배포 시 사라진다.**

- `DEVICE_ID` → env로 고정(위에서 해결).
- `medications.json` (REST `/api/medications` 가 파일에 기록) → 재배포 시 초기화됨.
  스케줄러는 이미 Firestore(`medications/{deviceId}/items`)도 읽으므로, **복약 등록을 Firestore 기준으로
  운용**하거나 Render **Persistent Disk** 부착을 권장. (보존이 필요하면 영구 디스크)
- `voice_messages/`, `sounds/temp_*` → 임시 파일이라 무방.

## 5. 로컬 빌드 테스트

```powershell
docker build -t senior-smile-backend:test .
docker run --rm -p 8000:8000 `
  -e DEVICE_ID=frame-local01 `
  -e OPENAI_API_KEY=sk-... `
  -e SERVICE_ACCOUNT_KEY_JSON="$(Get-Content backend\serviceAccountKey.json -Raw)" `
  senior-smile-backend:test
# → http://localhost:8000/health
```
