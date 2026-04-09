# 작업 일지 — 2026.04.09

## 보호자 앱 (ai-bum-app) — 신규 생성

- Expo(React Native) 프로젝트 생성, 5탭 구조 (홈/리포트/갤러리/알림/설정)
- 코랄 & 아이보리 디자인 시스템, 한국어 UI, Haptic 피드백 적용
- 갤러리 사진 업로드 구현 (expo-image-picker → Firebase Storage → Firestore)
- 알림 센터 Firestore 실시간 구독 (긴급/주의/일반 필터)
- Fabric 호환성 수정 (lucide 제거 → @expo/vector-icons, LinearGradient 제거)
- 백엔드(FastAPI) 구현: 이벤트 수신, 하트비트, 알림 자동생성, 통계 집계, 사진 API
- Firebase 프로젝트 생성 및 실제 연결 완료

## 시니어 앱 (senior-smile-project) — 수정

- ai-bum 백엔드로 이벤트/하트비트 전송 코드 추가
- ActiveScreen: 백엔드에서 가족 사진 폴링 → 실제 슬라이드쇼 표시
- STT/TTS 코드 복구 (voice_agent.py, sounds/, SubtitleBar 등)
- requirements.txt 의존성 전체 추가, .gitignore 보안 파일 제외

## 확인된 연동

- 얼굴 감지 → 화면 전환 → 이벤트 Firestore 저장 ✅
- 보호자 앱 사진 업로드 → 시니어 앱 슬라이드쇼 표시 ✅
- 하트비트 60초 전송 ✅, 알림 자동생성 ✅

## 다음 작업

1. 시니어 앱 사진 닫기 UX
2. STT/TTS 실제 동작 수정
3. 마이크 이벤트형 전환 (TV 소리 방지)
