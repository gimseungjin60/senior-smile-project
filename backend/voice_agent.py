import os
import time
import threading
import collections
from pathlib import Path
import speech_recognition as sr
from openai import OpenAI
import pygame
import config
import firebase_admin
from firebase_admin import credentials, firestore

class VoiceAgent:
    def __init__(self):
        # OpenAI API 키 검증
        if not config.OPENAI_API_KEY or config.OPENAI_API_KEY.startswith("여기에"):
            print("[VoiceAgent] ⚠️ OpenAI API 키가 설정되지 않았습니다. .env 파일을 확인하세요.")
        self.openai_client = OpenAI(api_key=config.OPENAI_API_KEY)
        self.recognizer = sr.Recognizer()
        self.is_pill_taken = False
        self.is_running = False
        self.is_listening = False
        # 외부 모듈(예: 게임/스트레칭 WebSocket)이 마이크/스피커 자원을 점유할 때
        # voice_agent를 일시 정지시키기 위한 플래그. False면 listen() 루프가 즉시 빠져나감.
        self.is_active = True
        self.chat_history = collections.deque(maxlen=5)
        self.current_subtitle = ""
        self.current_user_text = ""

        # 새 사진 표시 관련 상태 변수
        self.pending_photo_url = None
        self.is_asking_photo = False
        self.new_photo_url = None

        # 얼굴 감지 기반 마이크 게이팅
        self.face_detected = False

        # 호출어 기반 대화 활성화
        # Whisper STT가 노인 발음을 다양한 변형으로 인식할 수 있어 합리적 변형 폭넓게 등록.
        # "앨범" 단독은 TV/광고 오인식 위험으로 제외.
        self._wake_words = [
            "앨범아", "앨범 아",
            "앨버마", "앨버 마",
            "앨봄아", "앨봄 아",
            "앰범아", "앰버마",
            "엘범아", "엘버마",
            "앨범야", "앨범 야",
            "앨범 아이", "앨버마이",
        ]
        self.is_conversation_active = False  # 호출어 감지 후 대화 모드
        self._conversation_timeout = 10  # 무응답 시 대기 모드 복귀 (초)
        self._last_interaction_time = 0

        # 감정 리포트 (세션 종료 시 main.py에서 읽어감)
        self.last_emotion_report = ""

        # 긴급 상황 상태
        self.is_emergency = False
        # 노인이 응급 시 쓸 수 있는 표현 폭넓게 커버. 명확한 응급 표현만 추가해 오인 알림 방지.
        self._emergency_keywords = [
            # 통증/불편
            "아파", "아프", "어지러", "어지럽", "토할", "메스꺼",
            # 자세/움직임
            "쓰러", "쓰러졌", "넘어졌", "넘어 졌", "못 움직", "못움직", "못 일어",
            # 호흡/심혈관
            "숨이 차", "숨이 안", "가슴이 답답", "가슴이 아", "심장이",
            # 도움 요청
            "살려", "도와줘", "도와주", "사람 좀",
            # 명시적 호출
            "119", "응급", "구급",
            # 상해
            "다쳤", "피가 나", "피가 안", "기절",
        ]

        # 푸시 알림 매니저 (main.py에서 주입)
        self.notifier = None

        # 보호자 음성 메시지 대기열
        self.pending_voice_msg = None

        # 활동(게임/스트레칭) 요청 — 프론트가 다음 broadcast에서 받아 화면 전환
        # 'cognitive_game' | 'stretching' | None
        self.requested_activity = None

        # TTS 임시 파일 경로 (절대경로)
        self.temp_voice_path = str(config.SOUNDS_DIR / "temp_voice.mp3")

        # 음성 인식 에너지 임계값
        # 노인 음성 + 일반 USB 마이크 게인 조합에서 1500은 너무 높아 트리거 실패함.
        # 기본값으로 시작하고 dynamic이 환경에 맞춰 자동 조정하도록 위임.
        self.recognizer.energy_threshold = 300
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.dynamic_energy_adjustment_damping = 0.15
        self.recognizer.dynamic_energy_ratio = 1.5  # 배경 대비 1.5배 이상이면 음성으로 인식
        self.recognizer.pause_threshold = 0.8  # 발화 종료 판단 (말 사이 공백)
        self.recognizer.phrase_threshold = 0.3  # 최소 발화 길이

        # Pygame 믹서 초기화 (음성 재생용)
        pygame.mixer.init()
        
        # Firebase 연동
        self.db = None
        try:
            key_path = Path(__file__).parent / "serviceAccountKey.json"
            if key_path.exists():
                cred = credentials.Certificate(str(key_path))
                if not firebase_admin._apps:
                    firebase_admin.initialize_app(cred)
                self.db = firestore.client()
                self.db.collection('photo_notifs').on_snapshot(self._on_firebase_snapshot)
                print("[VoiceAgent] Firebase 실시간 감시 시작 완료")
            else:
                print("[VoiceAgent] serviceAccountKey.json 없음. Firebase 사진 알림 비활성화.")
        except Exception as e:
            print(f"[VoiceAgent] Firebase 초기화 에러 (사진 알림 비활성화): {e}")
            self.db = None
        
        # 시스템 프롬프트 (페르소나)
        self.system_prompt = (
            "당신은 70~80대 한국 어르신(할머니/할아버지)을 돌보는 7살짜리 손주 AI입니다. "
            "거실 액자 형태로 설치되어 어르신의 말동무, 복약 안내, 정서 지원을 담당합니다.\n"
            "\n"
            "[말투]\n"
            "- 어린 손주처럼 애교 섞인 다정한 말투. '웅!', '헤헤~', '으응~' 같은 표현을 자연스럽게 섞으세요.\n"
            "- 반드시 2문장 이내, 짧고 또렷하게.\n"
            "- 어려운 단어/외래어/약자(AI, 시스템, 데이터, 앱 등)는 절대 사용하지 마세요. 쉬운 우리말로만.\n"
            "\n"
            "[입력 판별 — 매우 중요]\n"
            "- 사용자 발화는 음성 인식으로 들어오므로 TV 뉴스, 광고, 드라마 대사, 노래 가사가 잘못 들어올 수 있습니다.\n"
            "- 입력이 다음 같으면 절대 그 내용에 답하지 말고 '할머니, 잘 못 들었어요. 다시 한 번 말씀해주실래요? 헤헤~' 식으로 부드럽게 되묻기만 하세요:\n"
            "  · 시사/정치/경제/사건사고 보도 같은 뉴스 어조\n"
            "  · 광고 문구, 상품 홍보\n"
            "  · 노래 가사, 드라마 대사처럼 어르신이 직접 말했을 가능성이 낮은 문장\n"
            "  · 어르신과 대화 맥락이 전혀 안 맞는 긴 문장\n"
            "\n"
            "[금지 사항]\n"
            "- 의학적 진단이나 약 처방 조언 금지. 몸이 아프다고 하시면 '보호자께 알려드릴게요'로 안내.\n"
            "- 부정적·위협적 표현, 죽음/사고 농담, 정치/종교 주제 금지.\n"
            "- 길게 설명하지 않기. 모르는 건 '잘 모르겠어요, 헤헤~'로 솔직하게.\n"
            "\n"
            "[감정 케어]\n"
            "- 어르신이 외롭거나 슬퍼하시면 공감을 먼저: '많이 적적하셨구나, 제가 옆에 있어요!'\n"
            "- 항상 안심시키고 따뜻한 분위기를 유지하세요."
        )
        
    def start_conversation(self):
        """대화 세션을 시작합니다."""
        if self.is_running:
            return
        self.is_running = True
        self.chat_history.clear()
        
        # 비전 루프를 방해하지 않도록 별도 스레드에서 대화 진행
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def stop_conversation(self):
        """대화 세션을 종료합니다."""
        if self.chat_history:
            chat_log = list(self.chat_history)
            print("[VoiceAgent] 대화 요약을 시작합니다...")
            summary_prompt = (
                "다음 대화 내역을 보고 어르신의 기분과 상태를 1줄 요약해 주세요. "
                "그리고 마지막 줄에 두 가지를 함께 적어 주세요:\n"
                "[기분 점수: NN/100점]\n"
                "[감정: KEY] (KEY는 happiness, neutral, surprise, sadness, anger, fear, disgust, contempt 중 하나)\n\n"
                "대화 내역:\n" + "\n".join(chat_log)
            )
            report = ""
            try:
                response = self.openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": summary_prompt}],
                    max_tokens=100
                )
                report = response.choices[0].message.content.strip()
                self.last_emotion_report = report
                print(f"\n==================================\n[AI 감정 리포트]: {report}\n==================================\n")
            except Exception as e:
                print(f"[요약 에러] {e}")

            # Firestore에 세션 기록 저장
            self._save_session_to_db(chat_log, report)
            self.chat_history.clear()

        self.is_running = False

    def _save_session_to_db(self, chat_log: list, emotion_report: str):
        """대화 세션을 Firestore에 저장합니다."""
        if not self.db:
            return
        try:
            import datetime
            self.db.collection("sessions").add({
                "device_id": config.DEVICE_ID,
                "messages": chat_log,
                "emotion_report": emotion_report,
                "pill_taken": self.is_pill_taken,
                "is_emergency": self.is_emergency,
                "created_at": datetime.datetime.now().isoformat(),
                "message_count": len(chat_log),
            })
            print(f"[VoiceAgent] 세션 기록 저장 완료 ({len(chat_log)}건)")
        except Exception as e:
            print(f"[VoiceAgent] 세션 저장 실패: {e}")

    def _match_and_log_medication(self):
        """현재 시각과 가장 가까운 처방(±2시간)에 매핑하여 medication_logs에 기록합니다.

        실패해도 기존 pill_taken 흐름은 유지되므로 try/except로 감쌉니다.
        """
        try:
            import json, datetime
            from pathlib import Path
            now = datetime.datetime.now()
            now_min = now.hour * 60 + now.minute

            meds_file = Path(__file__).parent / "medications.json"
            meds = []
            if meds_file.exists():
                meds = json.loads(meds_file.read_text(encoding="utf-8"))

            # ±2시간 (120분) 이내에서 가장 가까운 enabled 처방 찾기
            best = None
            best_gap = 121
            for m in meds:
                if not m.get("enabled", True):
                    continue
                t = m.get("time", "")
                if not t or ":" not in t:
                    continue
                try:
                    h, mm = t.split(":")
                    p_min = int(h) * 60 + int(mm)
                except ValueError:
                    continue
                gap = abs(now_min - p_min)
                if gap <= 120 and gap < best_gap:
                    best_gap = gap
                    best = m

            log = {
                "device_id": config.DEVICE_ID,
                "med_id": best["id"] if best else None,
                "med_name": best["name"] if best else None,
                "slot": best["time"] if best else None,
                "taken_at": now.isoformat(),
                "date": now.strftime("%Y-%m-%d"),
                "source": "voice",
            }
            if self.db:
                self.db.collection("medication_logs").add(log)
                print(f"[VoiceAgent] medication_logs 기록: {log['med_name'] or '(미매칭)'} @ {log['slot'] or '-'}")

            # 매칭 성공 시 stock 차감 (시연 안정성을 위해 별도 try)
            if best is not None:
                try:
                    current_stock = int(best.get("stock", 0) or 0)
                    if current_stock > 0:
                        for m in meds:
                            if m.get("id") == best["id"]:
                                m["stock"] = current_stock - 1
                                break
                        meds_file.write_text(
                            json.dumps(meds, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        print(f"[VoiceAgent] {best['name']} 잔량 차감: {current_stock} → {current_stock - 1}")
                except Exception as e:
                    print(f"[VoiceAgent] 잔량 차감 실패: {e}")
            return log
        except Exception as e:
            print(f"[VoiceAgent] medication_logs 기록 실패: {e}")
            return None

    def _on_firebase_snapshot(self, col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name == 'ADDED':
                # 새로 문서가 감지되면 URL 확보
                doc_data = change.document.to_dict()
                if 'url' in doc_data:
                    self.pending_photo_url = doc_data['url']

    def _is_wake_word(self, text: str) -> bool:
        """호출어가 포함되어 있는지 확인합니다."""
        return any(w in text for w in self._wake_words)

    def _activate_conversation(self):
        """호출어 감지 → 대화 모드 진입"""
        self.is_conversation_active = True
        self._last_interaction_time = time.time()
        print("[VoiceAgent] 호출어 감지! 대화 모드 진입")

    def _check_conversation_timeout(self):
        """대화 모드 타임아웃 체크 (무응답 시 대기 복귀)"""
        if not self.is_conversation_active:
            return
        if time.time() - self._last_interaction_time > self._conversation_timeout:
            self.is_conversation_active = False
            self.is_listening = False
            self.current_subtitle = ""
            print("[VoiceAgent] 대화 타임아웃 → 호출어 대기 모드 복귀")

    def _play_voice_message(self, msg):
        """보호자 음성 메시지를 재생합니다."""
        sender = msg.get("sender", "보호자")
        self._activate_conversation()
        self.speak(f"{sender}님이 음성 메시지를 보내셨어요! 들어보세요.")
        audio_path = config.VOICE_MSG_DIR / msg.get("filename", "")
        if audio_path.exists():
            try:
                self.current_subtitle = "음성 메시지 재생 중..."
                pygame.mixer.music.load(str(audio_path))
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    time.sleep(0.1)
                try:
                    pygame.mixer.music.unload()
                except AttributeError:
                    pass
            except Exception as e:
                print(f"[VoiceAgent] 음성 메시지 재생 실패: {e}")
            self.current_subtitle = ""
        # 메타데이터 played 갱신
        import json
        meta_path = config.VOICE_MSG_DIR / f"{msg['id']}.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["played"] = True
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def _handle_user_input(self, user_text: str, source):
        """사용자 발화를 처리합니다. (대화 모드에서 호출)"""
        self._last_interaction_time = time.time()
        self.chat_history.append(f"사용자: {user_text}")

        # 새 사진 수락/거절 (노인 응답 다양성 대응)
        if self.is_asking_photo:
            photo_yes = [
                "응", "어", "어어", "예", "네",
                "그래", "그러게", "그래라",
                "보여", "보여줘", "보여 줘", "보고 싶",
                "띄워", "띄워줘",
                "확인", "확인해",
                "좋아", "좋지", "좋겠",
                "봐", "볼래", "볼게",
            ]
            photo_no = [
                "아니", "안 봐", "안볼", "안 볼",
                "나중에", "이따가", "이따", "다음에",
                "됐어", "됐다", "괜찮", "안 돼", "안돼",
                "싫어", "싫다",
                "지금은 안", "지금은 됐",
            ]
            if any(w in user_text for w in photo_yes):
                self.speak("네! 화면에 예쁘게 띄울게요!")
                self.chat_history.append("AI: 네! 화면에 예쁘게 띄울게요!")
                self.new_photo_url = self.pending_photo_url
                self.pending_photo_url = None
                self.is_asking_photo = False
                if self.notifier:
                    self.notifier.notify_new_photo_viewed()
            elif any(w in user_text for w in photo_no):
                self.speak("알겠어요! 이따가 다시 물어볼게요 헤헤.")
                self.chat_history.append("AI: 알겠어요! 이따가 다시 물어볼게요 헤헤.")
                self.is_asking_photo = False
            else:
                response_text = self.get_openai_response(user_text)
                self.speak(response_text)
                self.chat_history.append(f"AI: {response_text}")
            return

        # 활동(게임/스트레칭) 트리거 — 키워드로 화면 전환 요청
        # 노인 표현 다양성: "심심해", "게임하자", "운동하자" 등
        game_keywords = ["게임하자", "게임 하자", "게임할래", "게임 할래", "심심해", "심심하", "놀자", "놀아"]
        stretch_keywords = ["운동하자", "운동 하자", "운동할래", "스트레칭", "체조", "몸 풀자", "몸풀자"]
        if any(k in user_text for k in game_keywords):
            self.requested_activity = "cognitive_game"
            response_text = "좋아요! 손주랑 가위바위보 져주기 게임 해요, 헤헤~"
            self.speak(response_text)
            self.chat_history.append(f"AI: {response_text}")
            return
        if any(k in user_text for k in stretch_keywords):
            self.requested_activity = "stretching"
            response_text = "네! 같이 몸 풀어봐요. 화면 따라 천천히 해주세요!"
            self.speak(response_text)
            self.chat_history.append(f"AI: {response_text}")
            return

        # 답장 녹음 (노인 표현 다양성)
        reply_keywords = [
            "답장", "답할래", "답할게", "답해", "답하",
            "보내줘", "보낼래", "보낼게", "보내자",
            "전해", "전해줘", "전할래", "말 전해", "말전해",
            "음성으로", "녹음해", "녹음할래", "녹음 해",
            "메시지", "메시지 보",
        ]
        if any(kw in user_text for kw in reply_keywords):
            self.speak("네! 지금부터 녹음할게요. 말씀해주세요!")
            self._record_reply(source)
            return

        # 긴급 상황 (항상 감지 — 대화 모드 아니어도)
        if any(kw in user_text for kw in self._emergency_keywords):
            print(f"[EMERGENCY] 긴급 상황 감지: {user_text}")
            self.is_emergency = True
            response_text = "할머니, 괜찮으세요?! 지금 바로 보호자에게 알릴게요! 잠시만 기다려주세요!"
            self.speak(response_text)
            self.chat_history.append(f"AI: {response_text}")
            if self.notifier:
                self.notifier.notify_emergency(user_text)
            return

        # 대화 종료 (노인 표현 다양성)
        end_keywords = [
            "그만", "그만해", "그만하자", "이제 그만", "고만",
            "들어가", "들어가야", "들어가자",
            "잘 자", "잘자", "잘 자라",
            "잘 가", "잘가",
            "안녕", "안녕히",
            "갈게", "갈래", "가야",
            "쉬어", "쉴래", "쉬자",
            "잠 잘", "잠잘", "자야", "잘게",
            "끊어", "끊자",
            "이제 됐", "고마워 그만",
        ]
        if any(w in user_text for w in end_keywords):
            self.speak("네, 알겠습니다. 푹 쉬세요! 다음에 또 올게요.")
            self.chat_history.append("AI: 네, 알겠습니다. 푹 쉬세요! 다음에 또 올게요.")
            self.is_conversation_active = False
            return

        # 약 복용 (노인 발음/방언 + 부정문 회피)
        # 긍정 패턴: "약 먹었어", "약 묵었어"(방언), "약 챙겼어", "약 삼켰어"
        # 부정 패턴 회피: "약 안 먹었어", "약 못 먹었어", "약 깜빡했어" 등은 매칭 안 함
        pill_taken_words = ["먹었", "묵었", "챙겼", "삼켰", "잘 먹", "챙겨 먹", "잡쉈"]
        pill_negation = [
            "안 먹", "안먹", "못 먹", "못먹",
            "안 챙", "안챙",
            "안 묵", "안묵",
            "안 삼", "안삼",
            "깜빡", "잊어", "잊었", "까먹",
            "아직 안", "아직안",
        ]
        is_pill_text = "약" in user_text and any(k in user_text for k in pill_taken_words)
        is_pill_negated = any(n in user_text for n in pill_negation)

        if is_pill_text and not is_pill_negated:
            print("[VoiceAgent] 약 복용 확인됨!")
            self.is_pill_taken = True
            self._match_and_log_medication()
            response_text = "아이고 잘하셨니더! 우리 할매 최고다!"
            self.play_sound("pill_praise.mp3", fallback_text=response_text)
            advice_text = "할머니, 약 드셨으니까 속 편하시게 시원한 물 한 잔 꼭 같이 드세요!"
            self.speak(advice_text)
            self.chat_history.append(f"AI: {response_text} {advice_text}")
            if self.notifier:
                self.notifier.notify_pill_taken()
        elif is_pill_text and is_pill_negated:
            # 명시적으로 안 드셨다고 한 경우 — 매칭하지 않고 부드럽게 안내
            response_text = "아직 안 드셨구나~ 시간 되시면 꼭 챙겨 드세요, 헤헤~"
            self.speak(response_text)
            self.chat_history.append(f"AI: {response_text}")
        # 식사 안부 (노인 표현 다양성)
        elif any(k in user_text for k in ["밥", "식사", "끼니", "아침 먹", "점심 먹", "저녁 먹", "잡수"]):
            response_text = "건강을 위해 식사는 꼭 챙겨 드세요."
            self.play_sound("meal_check.mp3", fallback_text=response_text)
            self.chat_history.append(f"AI: {response_text}")
        # 일반 대화 (GPT)
        else:
            response_text = self.get_openai_response(user_text)
            print(f"[AI 손주] {response_text}")
            self.speak(response_text)
            self.chat_history.append(f"AI: {response_text}")

    def _run_loop(self):
        """메인 루프: 호출어 대기 → 대화 모드 → 타임아웃 → 대기 복귀"""
        print("[VoiceAgent] 시작 (호출어: '앨범아')")
        self.play_sound("greet_home.mp3", fallback_text="할머니, 저 왔어요. '앨범아' 하고 불러주세요!")

        mic_retry_count = 0
        MAX_MIC_RETRIES = 5

        while self.is_running and mic_retry_count < MAX_MIC_RETRIES:
            try:
                with sr.Microphone() as source:
                    print("[VoiceAgent] 환경 소음 측정 중... (2초간 조용히 해주세요)")
                    self.recognizer.adjust_for_ambient_noise(source, duration=2.0)
                    mic_retry_count = 0
                    print(f"[VoiceAgent] 마이크 연결 성공 → 호출어 대기 중... (energy_threshold={self.recognizer.energy_threshold:.1f})")

                    while self.is_running:
                        # 외부 모듈(게임/스트레칭)이 자원 점유 중이면 대기
                        if not self.is_active:
                            self.is_listening = False
                            self.is_conversation_active = False
                            time.sleep(0.3)
                            continue
                        # 얼굴 미감지 시 마이크 정지
                        if not self.face_detected:
                            self.is_listening = False
                            self.is_conversation_active = False
                            time.sleep(0.3)
                            continue

                        # 보호자 음성 메시지 (대화 모드 무관하게 재생)
                        if self.pending_voice_msg:
                            msg = self.pending_voice_msg
                            self.pending_voice_msg = None
                            self._play_voice_message(msg)

                        # 사진 알림 (대화 모드 무관하게 알림)
                        if self.pending_photo_url and not self.is_asking_photo:
                            self.is_asking_photo = True
                            self._activate_conversation()
                            prompt = "방금 보호자가 사진을 보냈어. 어르신께 알려드리고 '볼까요?' 물어봐줘."
                            response_text = self.get_openai_response(prompt)
                            self.speak(response_text)
                            self.chat_history.append(f"AI: {response_text}")

                        # 대화 모드 타임아웃 체크
                        self._check_conversation_timeout()

                        # === 음성 입력 ===
                        user_text = self.listen(source)
                        if not user_text:
                            continue

                        print(f"[사용자] {user_text}")

                        # --- 긴급 상황은 호출어 없이도 항상 감지 ---
                        if any(kw in user_text for kw in self._emergency_keywords):
                            self._activate_conversation()
                            self._handle_user_input(user_text, source)
                            continue

                        # --- 호출어 대기 모드 ---
                        if not self.is_conversation_active:
                            if self._is_wake_word(user_text):
                                self._activate_conversation()
                                self._play_beep()
                                self.current_subtitle = "네, 말씀하세요!"
                                self.speak("네, 말씀하세요!")
                                # 호출어와 함께 말한 내용이 있으면 처리
                                remaining = user_text
                                for w in self._wake_words:
                                    remaining = remaining.replace(w, "").strip()
                                if remaining:
                                    self._handle_user_input(remaining, source)
                            else:
                                # 호출어 아님 → 무시 (TV 소리 등)
                                continue
                        else:
                            # --- 대화 모드 ---
                            self._handle_user_input(user_text, source)

                        time.sleep(0.3)

            except OSError as e:
                mic_retry_count += 1
                self.is_listening = False
                self.current_subtitle = "마이크 연결을 확인하고 있어요..."
                print(f"[VoiceAgent] 마이크 에러 ({mic_retry_count}/{MAX_MIC_RETRIES}): {e}")
                time.sleep(3)
                self.current_subtitle = ""
            except Exception as e:
                mic_retry_count += 1
                self.is_listening = False
                print(f"[VoiceAgent] 예상치 못한 에러 ({mic_retry_count}/{MAX_MIC_RETRIES}): {e}")
                time.sleep(3)

        if mic_retry_count >= MAX_MIC_RETRIES:
            self.current_subtitle = "마이크를 찾을 수 없어요. 연결을 확인해 주세요."
            print(f"[VoiceAgent] 마이크 재연결 {MAX_MIC_RETRIES}회 실패. 음성 기능 중단.")
            time.sleep(5)
            self.current_subtitle = ""

        print("[VoiceAgent] 대화 모드 종료")

    def _transcribe_audio(self, audio) -> str:
        """녹음된 오디오를 OpenAI Whisper API로 변환합니다."""
        import tempfile
        tmp_path = None
        try:
            wav_data = audio.get_wav_data()
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(wav_data)
                tmp_path = tmp.name

            with open(tmp_path, "rb") as audio_file:
                transcript = self.openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="ko"
                )
            return transcript.text.strip()
        except Exception as e:
            print(f"[VoiceAgent] OpenAI Whisper STT 에러: {e}")
            return ""
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def listen(self, source) -> str:
        # 외부에서 pause된 상태면 마이크 점유하지 않고 즉시 반환.
        # 게임/스트레칭 WebSocket이 자원을 쓸 때 마이크 충돌 방지.
        if not self.is_active:
            self.is_listening = False
            return ""

        self.is_listening = True
        self.current_user_text = ""
        print("[VOICE] 마이크 ON — 듣는 중")
        while self.is_running and self.is_active:
            try:
                audio = self.recognizer.listen(source, timeout=1.0, phrase_time_limit=10.0)
                # 디버그: 포착된 오디오 길이로 너무 짧으면 인식 실패 가능성 높음
                audio_sec = len(audio.frame_data) / (audio.sample_rate * audio.sample_width)
                print(f"[VoiceAgent] 소리 포착됨 ({audio_sec:.2f}초), Whisper 인식 중... ☁️")
                text = self._transcribe_audio(audio)
                self.is_listening = False
                if text:
                    self.current_user_text = text
                    return text
                else:
                    print(f"[VoiceAgent] ⚠️ Whisper가 빈 텍스트 반환 (오디오 {audio_sec:.2f}초). 음성이 너무 작거나 짧을 수 있음.")
                    return ""
            except sr.WaitTimeoutError:
                continue
            except Exception as e:
                print(f"[VoiceAgent] 음성 인식 중 에러 발생: {e}")
                self.is_listening = False
                return ""

        self.is_listening = False
        return ""

    def get_openai_response(self, text: str) -> str:
        messages = [{"role": "system", "content": self.system_prompt}]
        for msg in self.chat_history:
            role = "user" if msg.startswith("사용자") else "assistant"
            messages.append({"role": role, "content": msg})
        messages.append({"role": "user", "content": text})

        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=100,
                temperature=0.8
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"[VoiceAgent] OpenAI Chat API 에러: {e}")
            return "잠시만요~ 인터넷이 좀 아픈가 봐요! 다시 말해줄래? 웅!"

    def _stop_mixer_safely(self):
        """진행 중인 재생을 안전하게 중지 — 새 재생 시작 전 충돌 방지용."""
        try:
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
                time.sleep(0.05)  # 디바이스 자원 해제 대기
        except Exception:
            pass

    def _wait_until_done(self, timeout_sec: float = 30.0):
        """재생 완료 / 외부 pause / 타임아웃까지 대기. 게임 시작 시 즉시 중단."""
        start = time.time()
        try:
            while pygame.mixer.music.get_busy():
                if not self.is_active:
                    pygame.mixer.music.stop()
                    break
                if time.time() - start > timeout_sec:
                    pygame.mixer.music.stop()
                    break
                time.sleep(0.05)
        except Exception:
            pass

    def _play_beep(self):
        """대화 전 알림음을 재생합니다."""
        if not self.is_active:
            return
        beep_path = config.SOUNDS_DIR / "beep.wav"
        if beep_path.exists():
            try:
                self._stop_mixer_safely()
                pygame.mixer.music.load(str(beep_path))
                pygame.mixer.music.play()
                self._wait_until_done(timeout_sec=2.0)
            except Exception:
                pass

    def speak(self, text: str):
        if not self.is_active:
            return
        self._play_beep()
        self.current_subtitle = text
        try:
            response = self.openai_client.audio.speech.create(
                model="tts-1",
                voice="nova",
                input=text,
                speed=1.05
            )
            response.stream_to_file(self.temp_voice_path)

            self._stop_mixer_safely()
            pygame.mixer.music.load(self.temp_voice_path)
            pygame.mixer.music.play()
            self._wait_until_done(timeout_sec=20.0)

            try:
                pygame.mixer.music.unload()
            except AttributeError:
                pass

        except Exception as e:
            print(f"[VOICE] TTS 에러: {e}")
        finally:
            self.current_subtitle = ""
            # AI 발화 직후 에코 방지 대기
            time.sleep(0.5)

    def play_sound(self, filename: str, fallback_text: str):
        """준비된 MP3 파일을 우선 재생하고, 파일이 없으면 TTS로 대체(Fallback)합니다."""
        if not self.is_active:
            return
        filepath = config.SOUNDS_DIR / filename
        self.current_subtitle = fallback_text
        if filepath.exists():
            self._play_beep()
            try:
                self._stop_mixer_safely()
                pygame.mixer.music.load(str(filepath))
                pygame.mixer.music.play()
                self._wait_until_done(timeout_sec=15.0)
                try:
                    pygame.mixer.music.unload()
                except AttributeError:
                    pass
            except Exception as e:
                print(f"[VOICE] MP3 재생 에러({filename}): {e}")
                self.speak(fallback_text)
            self.current_subtitle = ""
        else:
            print(f"[VOICE] '{filename}' 미존재 — TTS로 대체")
            self.speak(fallback_text) # 이 안에서 current_subtitle이 초기화됨

    def _record_reply(self, source):
        """어르신의 답장 음성을 녹음합니다."""
        import uuid, json, wave
        self.is_listening = True
        self.current_subtitle = "녹음 중... 말씀해주세요"
        print("[VoiceAgent] 답장 녹음 시작")
        try:
            audio = self.recognizer.listen(source, timeout=3, phrase_time_limit=15)
            self.is_listening = False

            # STT로 텍스트도 추출 (OpenAI Whisper)
            text = self._transcribe_audio(audio)

            # WAV로 저장
            msg_id = f"reply_{uuid.uuid4().hex[:8]}"
            filename = f"{msg_id}.wav"
            filepath = config.VOICE_MSG_DIR / filename

            wav_data = audio.get_wav_data()
            with open(filepath, "wb") as f:
                f.write(wav_data)

            # 메타데이터 저장
            import datetime
            meta = {
                "id": msg_id,
                "sender": "어르신",
                "filename": filename,
                "direction": "to_family",
                "text": text,
                "created_at": datetime.datetime.now().isoformat(),
            }
            meta_path = config.VOICE_MSG_DIR / f"{msg_id}.json"
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

            self.speak("잘 녹음했어요! 보호자에게 전달할게요.")
            self.chat_history.append(f"사용자(답장): {text or '(음성 메시지)'}")

            if self.notifier:
                self.notifier.notify_voice_reply(text)

            print(f"[VoiceAgent] 답장 저장 완료: {msg_id}")
        except sr.WaitTimeoutError:
            self.is_listening = False
            self.speak("음성이 안 들렸어요. 다시 해볼까요?")
        except Exception as e:
            self.is_listening = False
            print(f"[VoiceAgent] 답장 녹음 에러: {e}")
            self.speak("녹음 중 문제가 생겼어요. 다시 해볼게요!")
        self.current_subtitle = ""

    def trigger_pill_reminder(self):
        """스케줄러에 의해 호출되어 복약 독촉 멘트를 발생시킵니다."""
        print("[VOICE] 복약 알람 발화")
        self.is_conversation_active = True
        reminder_text = "할머니, 약 드실 시간이에요. 잊지 말고 꼭 챙겨 드세요!"
        self.play_sound("pill_remind.mp3", fallback_text=reminder_text)
        self.is_conversation_active = False

    # ─────────────────────────────────────────────────────────
    # 자원 점유 제어 (게임/스트레칭이 마이크/스피커 쓸 때 일시 정지)
    # ─────────────────────────────────────────────────────────
    def pause(self):
        """마이크/대화 일시 정지. 진행 중인 listen은 1초 내에 빠져나감."""
        if not self.is_active:
            return
        self.is_active = False
        self.is_listening = False
        self.is_conversation_active = False
        # 진행 중인 TTS/효과음도 중단하여 게임 사운드와 충돌 방지
        try:
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
        except Exception:
            pass
        print("[VOICE] 일시 정지 (외부 모듈이 마이크/스피커 자원 점유)")

    def resume(self):
        """일시 정지 해제. 호출어 대기 모드로 복귀."""
        if self.is_active:
            return
        self.is_active = True
        print("[VOICE] 재개 — 호출어 대기 모드로 복귀")

# 단독 실행 테스트용
if __name__ == "__main__":
    agent = VoiceAgent()
    agent.start_conversation()
    try:
        while agent.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        agent.stop_conversation()
