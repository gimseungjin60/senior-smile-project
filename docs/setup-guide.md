# AI-bum & Senior Smile 프로젝트 환경 세팅 가이드

## 사전 준비

### 필수 설치 프로그램
- **Python 3.11+**: https://www.python.org/downloads/
- **Node.js 18+**: https://nodejs.org/
- **Git**: https://git-scm.com/
- **Expo Go** (아이폰 App Store에서 설치)

---

## 1단계: 코드 클론

```
git clone https://github.com/gimseungjin60/ai-bum-app.git
git clone https://github.com/gimseungjin60/senior-smile-project.git
```

---

## 2단계: 보안 파일 수동 복사

> ⚠️ 이 파일들은 GitHub에 올라가지 않습니다. USB, 카카오톡, 에어드롭 등으로 직접 복사하세요.

### 복사할 파일 3개

| 파일명 | 복사할 위치 | 설명 |
|--------|-----------|------|
| `serviceAccountKey.json` | `ai-bum-app/backend/` | Firebase 백엔드 인증키 |
| `serviceAccountKey.json` | `senior-smile-project/backend/` | Firebase 시니어앱 인증키 |
| `.env` | `senior-smile-project/backend/` | OpenAI API 키 + 카메라 설정 |

### .env 파일 내용 (직접 생성해도 됨)

```
OPENAI_API_KEY=sk-여기에_본인_OpenAI_키_입력
CAMERA_INDEX=0
```

> 카메라가 안 잡히면 `CAMERA_INDEX=1`로 변경

---

## 3단계: 패키지 설치

### ai-bum-app (보호자 앱)

```
cd ai-bum-app
npm install
cd backend
pip install -r requirements.txt
cd ../..
```

### senior-smile-project (시니어 앱)

```
cd senior-smile-project
cd backend
pip install -r requirements.txt
cd ../frontend
npm install
cd ../..
```

---

## 4단계: 서버 실행

> 터미널 4개를 각각 열어서 실행합니다.

### 터미널 1: ai-bum 백엔드 (포트 8001)

```
cd ai-bum-app/backend
uvicorn main:app --host 0.0.0.0 --port 8001
```

### 터미널 2: 시니어 백엔드 (포트 8000)

```
cd senior-smile-project/backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 터미널 3: 시니어 프론트엔드

```
cd senior-smile-project/frontend
npm run dev
```

### 터미널 4: 보호자 모바일 앱

```
cd ai-bum-app
npx expo start
```

---

## 5단계: 접속 확인

| 서비스 | URL | 설명 |
|--------|-----|------|
| 시니어 앱 화면 | http://localhost:5173 | 브라우저에서 열기 |
| 시니어 카메라 피드 | http://localhost:8000/video | 얼굴 감지 확인용 |
| 시니어 상태 확인 | http://localhost:8000/status | `{"status": "idle"}` |
| ai-bum 백엔드 | http://localhost:8001 | API 엔드포인트 목록 |
| ai-bum 헬스체크 | http://localhost:8001/health | `{"status": "healthy"}` |
| 보호자 앱 | Expo Go QR 스캔 | 아이폰에서 테스트 |

---

## 6단계: 테스트 체크리스트

### 얼굴 감지
- [ ] `localhost:5173` 열기
- [ ] 웹캠에 얼굴 비추기
- [ ] idle → greeting(안녕하세요!) → active(슬라이드) 전환 확인
- [ ] 얼굴 치우고 30초 → idle 복귀 확인

### 이벤트 연동
- [ ] 터미널 2에 `이벤트 전송: session_start` 로그 확인
- [ ] 터미널 1에 `200 OK` 응답 확인

### 사진 업로드
- [ ] 아이폰 보호자 앱 → 갤러리 탭 → "사진 올리기"
- [ ] 사진 선택 → "업로드 완료" 알림 확인
- [ ] 시니어 앱 active 화면에서 60초 내 사진 표시 확인

### 알림
- [ ] 보호자 앱 알림 탭에서 "새로운 사진이 등록되었습니다" 확인
- [ ] Firebase 콘솔에서 notifications 컬렉션 확인

---

## 트러블슈팅

### 카메라가 안 잡힐 때
`.env`에서 `CAMERA_INDEX=1`로 변경 후 서버 재시작

### Expo Go에서 앱이 안 열릴 때
PC와 아이폰이 **같은 Wi-Fi**에 연결되어 있는지 확인

### WebSocket 연결 실패 (프론트엔드 화면 전환 안 됨)
`pip install "uvicorn[standard]"` 실행 후 서버 재시작

### Firebase 연결 실패
`serviceAccountKey.json` 파일이 올바른 위치에 있는지 확인

---

## 프로젝트 구조

```
ai-bum-app/                    ← 보호자용 모바일 앱
├── App.js                     (Expo 진입점)
├── src/
│   ├── screens/               (홈/리포트/갤러리/알림/설정)
│   ├── components/            (Icon, HapticButton, Card)
│   ├── config/firebase.js     (Firebase 연결)
│   ├── hooks/useFirestore.js  (실시간 데이터 구독)
│   └── services/photoService.js (사진 업로드)
└── backend/
    ├── main.py                (FastAPI 서버)
    ├── config.py              (Firebase Admin SDK)
    ├── scheduler.py           (알림 자동생성)
    ├── routers/               (events, reports, photos API)
    └── services/              (firestore, notification, stats)

senior-smile-project/          ← 시니어용 스마트 프레임
├── backend/
│   ├── main.py                (얼굴 감지 + 상태머신)
│   ├── config.py              (설정값)
│   ├── voice_agent.py         (STT/TTS 음성 대화)
│   ├── models/                (OpenCV DNN 모델)
│   └── sounds/                (인사/알림 음성 파일)
└── frontend/
    └── src/
        ├── App.jsx            (WebSocket 상태 관리)
        └── components/        (Idle/Greet/Active/SubtitleBar)
```

---

## 사용 포트 정리

| 포트 | 서비스 | 용도 |
|------|--------|------|
| 5173 | Vite (시니어 프론트) | 시니어 앱 화면 |
| 8000 | FastAPI (시니어 백엔드) | 얼굴 감지 + WebSocket |
| 8001 | FastAPI (ai-bum 백엔드) | 이벤트 저장 + 알림 + 리포트 |
| 8081 | Expo (보호자 앱) | 모바일 앱 번들링 |
