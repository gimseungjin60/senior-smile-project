import os
import re
import queue
import time
import threading
import tempfile
import collections
from pathlib import Path
from openai import OpenAI
import config
import firebase_admin
from firebase_admin import credentials, firestore


# Whisper가 무음/잡음을 받았을 때 지어내는 환각(hallucination) 문구.
# 유튜브·방송 자막으로 학습된 탓에 무음 구간에서 이런 클로징 멘트를 생성한다.
# 부분 문자열 매칭으로 거른다 (예: "시청해주셔서 감사합니다" → "시청해주" 로 잡힘).
_HALLUCINATION_PATTERNS = [
    "시청해주", "시청해 주", "시청 해주",
    "구독과 좋아요", "좋아요와 구독", "구독 부탁", "구독, 좋아요",
    "mbc 뉴스", "kbs 뉴스", "sbs 뉴스", "ytn", "jtbc",
    "뉴스 이덕영", "뉴스였습니다", "뉴스입니다", "기자였습니다",
    "다음 영상에서", "다음 시간에", "다음 영상에",
    "한글자막", "엔딩 크레딧", "자막 제공",
    "함께해주셔서", "들어주셔서 감사",
]


def _is_hallucination(text: str) -> bool:
    """Whisper 환각 문구인지 판정. 소문자·공백 정규화 후 부분 매칭."""
    if not text:
        return True
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return any(p in norm for p in _HALLUCINATION_PATTERNS)


class VoiceAgent:
    def __init__(self, device_id: str = ""):
        self.device_id = device_id or config.DEVICE_ID
        if not config.OPENAI_API_KEY or config.OPENAI_API_KEY.startswith("여기에"):
            print("[VoiceAgent] ⚠️ OpenAI API 키가 설정되지 않았습니다. .env 파일을 확인하세요.")
        self.openai_client = OpenAI(api_key=config.OPENAI_API_KEY)

        self.is_pill_taken = False
        self.is_running = False
        self.is_listening = False
        self.is_active = True
        self.chat_history = collections.deque(maxlen=5)
        self.current_subtitle = ""
        self.current_user_text = ""

        self.pending_photo_url = None
        self.is_asking_photo = False
        self.new_photo_url = None

        self.face_detected = False

        self._wake_words = [
            "앨범아", "앨범 아",
            "앨버마", "앨버 마",
            "앨봄아", "앨봄 아",
            "앰범아", "앰버마",
            "엘범아", "엘버마",
            "앨범야", "앨범 야",
            "앨범 아이", "앨버마이",
        ]
        self.is_conversation_active = False
        self._conversation_timeout = 10
        self._last_interaction_time = 0

        self.last_emotion_report = ""
        self.is_emergency = False

        self._emergency_keywords = [
            "아파", "아프", "어지러", "어지럽", "토할", "메스꺼",
            "쓰러", "쓰러졌", "넘어졌", "넘어 졌", "못 움직", "못움직", "못 일어",
            "숨이 차", "숨이 안", "가슴이 답답", "가슴이 아", "심장이",
            "살려", "도와줘", "도와주", "사람 좀",
            "119", "응급", "구급",
            "다쳤", "피가 나", "피가 안", "기절",
        ]

        self.notifier = None
        self.pending_voice_msg = None
        self.requested_activity = None

        self.temp_voice_path = str(config.SOUNDS_DIR / "temp_voice.mp3")

        # 서버 모드 오디오 파이프라인 (VAD → WAV → Whisper)
        self.audio_in: queue.Queue = queue.Queue()
        self.audio_out = None           # VoiceSocketBridge.send로 주입됨
        self.is_speaking = False        # half-duplex 플래그
        self.playback_done = threading.Event()

        # Firebase 연동
        # 클라우드: main.py가 FIREBASE_KEY_B64 환경변수로 이미 초기화함 (파일 없음).
        # 로컬: serviceAccountKey.json 파일로 초기화. 둘 중 무엇이든 앱이 떠 있으면 client 사용.
        self.db = None
        try:
            if not firebase_admin._apps:
                key_path = Path(__file__).parent / "serviceAccountKey.json"
                if key_path.exists():
                    firebase_admin.initialize_app(credentials.Certificate(str(key_path)))
            if firebase_admin._apps:
                self.db = firestore.client()
                self.db.collection('photo_notifs').on_snapshot(self._on_firebase_snapshot)
                print("[VoiceAgent] Firebase 실시간 감시 시작 완료")
            else:
                print("[VoiceAgent] Firebase 미초기화 (키/환경변수 없음). 사진 알림 비활성화.")
        except Exception as e:
            print(f"[VoiceAgent] Firebase 초기화 에러 (사진 알림 비활성화): {e}")
            self.db = None

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

    # ──────────────────────────────────────────
    # 오디오 I/O (VAD half-duplex)
    # ──────────────────────────────────────────

    def _emit(self, payload: dict):
        """클라이언트로 제어 메시지 송신. audio_out 미주입이면 무시."""
        if self.audio_out:
            try:
                self.audio_out(payload)
            except Exception as e:
                print(f"[VoiceAgent] audio_out 송신 실패: {e}")

    def _play_and_wait(self, payload: dict, timeout_sec: float):
        """half-duplex 재생: 클라에 재생 지시 → playback_done 대기.
        대기 동안 is_speaking=True → listen()이 인입 오디오를 드롭(에코 방지)."""
        if not self.is_active:
            return
        self.is_speaking = True
        self.playback_done.clear()
        self._emit(payload)
        if not self.playback_done.wait(timeout=timeout_sec):
            print(f"[VoiceAgent] playback_done 타임아웃({timeout_sec}s) — 강제 진행")
        self.is_speaking = False
        # 재생 완료 = 사용자 차례 시작. 여기서 타이머를 리셋해야 AI 발화 시간이
        # 대화 타임아웃에 포함되지 않음 (half-duplex라 재생 중엔 사용자가 말할 수 없음).
        self._last_interaction_time = time.time()

    def _transcribe_audio(self, audio_bytes: bytes, fmt: str = "wav") -> str:
        """브라우저에서 받은 오디오 bytes를 Whisper API로 변환."""
        if not audio_bytes:
            return ""
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            with open(tmp_path, "rb") as f:
                transcript = self.openai_client.audio.transcriptions.create(
                    model="whisper-1", file=f, language="ko",
                    temperature=0.0,  # 환각 최소화
                )
            text = transcript.text.strip()
            # 무음/잡음을 음성으로 오탐 → Whisper 환각 클로징 멘트 차단
            if _is_hallucination(text):
                print(f"[VoiceAgent] ⚠️ Whisper 환각 추정 무시: '{text}'")
                return ""
            return text
        except Exception as e:
            print(f"[VoiceAgent] Whisper STT 에러: {e}")
            return ""
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def listen(self) -> str:
        """브라우저 VAD 발화 큐(audio_in)에서 다음 발화를 꺼내 Whisper로 인식."""
        if not self.is_active:
            self.is_listening = False
            return ""
        self.is_listening = True
        self.current_user_text = ""
        try:
            audio_bytes = self.audio_in.get(timeout=1.0)
        except queue.Empty:
            self.is_listening = False
            return ""
        # 재생 중 들어온 발화는 에코 위험 → 드롭 (half-duplex 최종 안전망)
        if self.is_speaking or not self.is_active:
            self.is_listening = False
            return ""
        print(f"[VoiceAgent] 발화 수신 ({len(audio_bytes)}B), Whisper 인식 중... ☁️")
        text = self._transcribe_audio(audio_bytes)
        self.is_listening = False
        if text:
            self.current_user_text = text
            return text
        print("[VoiceAgent] ⚠️ Whisper 빈 텍스트 (음성이 너무 작거나 짧음)")
        return ""

    def _play_beep(self):
        """대화 전 알림음 — 클라이언트가 /sounds/beep.wav 재생."""
        if not self.is_active:
            return
        self._play_and_wait({"type": "beep", "url": "/sounds/beep.wav"}, timeout_sec=3.0)

    def speak(self, text: str):
        """TTS(mp3) 생성 후 클라이언트가 /tts/latest 로 가져가 재생하도록 지시."""
        if not self.is_active:
            return
        self.current_subtitle = text
        try:
            response = self.openai_client.audio.speech.create(
                model="tts-1", voice="nova", input=text, speed=1.05
            )
            response.stream_to_file(self.temp_voice_path)
            self._play_and_wait(
                {"type": "speak", "url": "/tts/latest", "ts": time.time()},
                timeout_sec=20.0,
            )
        except Exception as e:
            print(f"[VOICE] TTS 에러: {e}")
        finally:
            self.current_subtitle = ""

    def play_sound(self, filename: str, fallback_text: str):
        """준비된 MP3를 클라이언트가 /sounds/{filename}로 재생. 파일 없으면 TTS 대체."""
        if not self.is_active:
            return
        self.current_subtitle = fallback_text
        filepath = config.SOUNDS_DIR / filename
        if filepath.exists():
            self._play_and_wait(
                {"type": "speak", "url": f"/sounds/{filename}"}, timeout_sec=15.0,
            )
            self.current_subtitle = ""
        else:
            print(f"[VOICE] '{filename}' 미존재 — TTS로 대체")
            self.speak(fallback_text)

    # ──────────────────────────────────────────
    # 메인 루프
    # ──────────────────────────────────────────

    def start_conversation(self):
        if self.is_running:
            return
        self.is_running = True
        self.chat_history.clear()
        self.thread = threading.Thread(target=self._run_loop_server, daemon=True)
        self.thread.start()

    def stop_conversation(self):
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
            self._save_session_to_db(chat_log, report)
            self.chat_history.clear()
        self.is_running = False

    def _run_loop_server(self):
        """메인 루프: 브라우저 VAD 발화 큐 소비 → 호출어 대기 → 대화 모드 → 타임아웃."""
        print("[VoiceAgent] 서버 모드 시작 (Silero VAD / /ws/voice)")
        # 시작 인사(greet_home.mp3 "할머니 다녀오셨어요 보고싶었어요") 자동 재생 비활성화

        while self.is_running:
            try:
                if not self.is_active:
                    self.is_listening = False
                    self.is_conversation_active = False
                    # stale 오디오 드레인
                    while not self.audio_in.empty():
                        try:
                            self.audio_in.get_nowait()
                        except Exception:
                            break
                    time.sleep(0.3)
                    continue

                if self.pending_voice_msg:
                    msg = self.pending_voice_msg
                    self.pending_voice_msg = None
                    self._play_voice_message(msg)

                if self.pending_photo_url and not self.is_asking_photo:
                    self.is_asking_photo = True
                    self._activate_conversation()
                    prompt = "방금 보호자가 사진을 보냈어. 어르신께 알려드리고 '볼까요?' 물어봐줘."
                    response_text = self.get_openai_response(prompt)
                    self.speak(response_text)
                    self.chat_history.append(f"AI: {response_text}")

                self._check_conversation_timeout()

                user_text = self.listen()
                if not user_text:
                    continue

                print(f"[사용자] {user_text}")
                self.current_user_text = user_text

                if any(kw in user_text for kw in self._emergency_keywords):
                    self._activate_conversation()
                    self._handle_user_input(user_text)
                    continue

                if not self.is_conversation_active:
                    if self._is_wake_word(user_text):
                        self._activate_conversation()
                        self._play_beep()
                        self.current_subtitle = "네, 말씀하세요!"
                        self.speak("네, 말씀하세요!")
                        remaining = user_text
                        for w in self._wake_words:
                            remaining = remaining.replace(w, "").strip()
                        if remaining:
                            self._handle_user_input(remaining)
                    # 호출어 아님 → 무시 (TV 소리 등)
                else:
                    self._handle_user_input(user_text)

                time.sleep(0.3)

            except Exception as e:
                self.is_listening = False
                print(f"[VoiceAgent] 루프 에러: {e}")
                time.sleep(1)

        print("[VoiceAgent] 서버 모드 종료")

    # ──────────────────────────────────────────
    # 대화 처리
    # ──────────────────────────────────────────

    def _is_wake_word(self, text: str) -> bool:
        return any(w in text for w in self._wake_words)

    def _activate_conversation(self):
        self.is_conversation_active = True
        self._last_interaction_time = time.time()
        print("[VoiceAgent] 호출어 감지! 대화 모드 진입")

    def _check_conversation_timeout(self):
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
                self._play_and_wait(
                    {"type": "speak", "url": f"/voice-msg/{msg.get('filename', '')}"},
                    timeout_sec=30.0,
                )
            except Exception as e:
                print(f"[VoiceAgent] 음성 메시지 재생 실패: {e}")
            self.current_subtitle = ""
        import json
        meta_path = config.VOICE_MSG_DIR / f"{msg['id']}.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["played"] = True
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def _handle_user_input(self, user_text: str, source=None):
        self._last_interaction_time = time.time()
        self.chat_history.append(f"사용자: {user_text}")

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

        reply_keywords = [
            "답장", "답할래", "답할게", "답해", "답하",
            "보내줘", "보낼래", "보낼게", "보내자",
            "전해", "전해줘", "전할래", "말 전해", "말전해",
            "음성으로", "녹음해", "녹음할래", "녹음 해",
            "메시지", "메시지 보",
        ]
        if any(kw in user_text for kw in reply_keywords):
            self.speak("답장 녹음은 지금 준비 중이에요! 조금만 기다려주세요.")
            return

        if any(kw in user_text for kw in self._emergency_keywords):
            print(f"[EMERGENCY] 긴급 상황 감지: {user_text}")
            self.is_emergency = True
            response_text = "할머니, 괜찮으세요?! 지금 바로 보호자에게 알릴게요! 잠시만 기다려주세요!"
            self.speak(response_text)
            self.chat_history.append(f"AI: {response_text}")
            if self.notifier:
                self.notifier.notify_emergency(user_text)
            return

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
            if self.db:
                try:
                    import datetime as _dt
                    self.db.collection("devices").document(self.device_id).set(
                        {"lastPillTakenAt": _dt.datetime.now(tz=_dt.timezone.utc)},
                        merge=True,
                    )
                except Exception as _e:
                    print(f"[VoiceAgent] lastPillTakenAt 업데이트 실패: {_e}")
            response_text = "아이고 잘하셨니더! 우리 할매 최고다!"
            self.play_sound("pill_praise.mp3", fallback_text=response_text)
            advice_text = "할머니, 약 드셨으니까 속 편하시게 시원한 물 한 잔 꼭 같이 드세요!"
            self.speak(advice_text)
            self.chat_history.append(f"AI: {response_text} {advice_text}")
            if self.notifier:
                self.notifier.notify_pill_taken()
        elif is_pill_text and is_pill_negated:
            response_text = "아직 안 드셨구나~ 시간 되시면 꼭 챙겨 드세요, 헤헤~"
            self.speak(response_text)
            self.chat_history.append(f"AI: {response_text}")
        elif any(k in user_text for k in ["밥", "식사", "끼니", "아침 먹", "점심 먹", "저녁 먹", "잡수"]):
            response_text = "건강을 위해 식사는 꼭 챙겨 드세요."
            self.play_sound("meal_check.mp3", fallback_text=response_text)
            self.chat_history.append(f"AI: {response_text}")
        else:
            response_text = self.get_openai_response(user_text)
            print(f"[AI 손주] {response_text}")
            self.speak(response_text)
            self.chat_history.append(f"AI: {response_text}")

    def get_openai_response(self, text: str) -> str:
        messages = [{"role": "system", "content": self.system_prompt}]
        for msg in self.chat_history:
            role = "user" if msg.startswith("사용자") else "assistant"
            messages.append({"role": role, "content": msg})
        messages.append({"role": "user", "content": text})
        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini", messages=messages, max_tokens=100, temperature=0.8
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"[VoiceAgent] OpenAI Chat API 에러: {e}")
            return "잠시만요~ 인터넷이 좀 아픈가 봐요! 다시 말해줄래? 웅!"

    def trigger_pill_reminder(self):
        print("[VOICE] 복약 알람 발화")
        self.is_conversation_active = True
        self.play_sound("pill_remind.mp3", fallback_text="할머니, 약 드실 시간이에요. 잊지 말고 꼭 챙겨 드세요!")
        self.is_conversation_active = False

    def pause(self):
        """마이크/대화 일시 정지. half-duplex 대기도 해제."""
        if not self.is_active:
            return
        self.is_active = False
        self.is_listening = False
        self.is_conversation_active = False
        self.playback_done.set()  # 재생 대기가 있으면 깨움
        print("[VOICE] 일시 정지 (외부 모듈이 자원 점유)")

    def resume(self):
        if self.is_active:
            return
        self.is_active = True
        print("[VOICE] 재개 — 호출어 대기 모드로 복귀")

    # ──────────────────────────────────────────
    # Firebase / DB
    # ──────────────────────────────────────────

    def _on_firebase_snapshot(self, col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name == 'ADDED':
                doc_data = change.document.to_dict()
                if 'url' in doc_data:
                    self.pending_photo_url = doc_data['url']

    def _save_session_to_db(self, chat_log: list, emotion_report: str):
        if not self.db:
            return
        try:
            import datetime
            self.db.collection("sessions").add({
                "device_id": self.device_id,
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
        try:
            import datetime
            from google.cloud.firestore_v1 import transforms

            # 약 시간은 한국시간 기준 → 서버(UTC)와 9시간 어긋나지 않도록 KST로 비교
            _KST = datetime.timezone(datetime.timedelta(hours=9))
            now = datetime.datetime.now(_KST)
            now_min = now.hour * 60 + now.minute
            device_id = self.device_id

            meds = []
            if self.db:
                items_ref = self.db.collection("medications").document(device_id).collection("items")
                for doc in items_ref.get():
                    data = doc.to_dict()
                    data["id"] = doc.id
                    meds.append(data)

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
                "device_id": device_id,
                "deviceId": device_id,
                "med_id": best["id"] if best else None,
                "med_name": best["name"] if best else None,
                "slot": best.get("time") if best else None,
                "taken_at": now.isoformat(),
                "date": now.strftime("%Y-%m-%d"),
                "source": "voice",
            }
            if self.db:
                self.db.collection("medication_logs").add(log)
                print(f"[VoiceAgent] medication_logs 기록: {log['med_name'] or '(미매칭)'} @ {log['slot'] or '-'}")

            if best is not None and self.db:
                try:
                    item_ref = (self.db.collection("medications")
                                .document(device_id)
                                .collection("items")
                                .document(best["id"]))

                    @self.db.transaction()
                    def _decrement(tx, ref):
                        snap = ref.get(transaction=tx)
                        if not snap.exists:
                            return
                        current = int(snap.get("stock") or 0)
                        if current > 0:
                            tx.update(ref, {"stock": current - 1})
                            print(f"[VoiceAgent] {best['name']} 잔량: {current} → {current - 1}")

                    _decrement(item_ref)
                except Exception as e:
                    print(f"[VoiceAgent] 잔량 차감 실패: {e}")

            return log
        except Exception as e:
            print(f"[VoiceAgent] medication_logs 기록 실패: {e}")
            return None
