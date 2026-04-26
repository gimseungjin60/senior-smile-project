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
        self.chat_history = []
        self.current_subtitle = ""
        self.current_user_text = ""

        # 새 사진 표시 관련 상태 변수
        self.pending_photo_url = None
        self.is_asking_photo = False
        self.new_photo_url = None

        # 얼굴 감지 기반 마이크 게이팅
        self.face_detected = False

        # 호출어 기반 대화 활성화
        self._wake_words = ["앨범아", "앨범 아", "엘범아", "앨버마", "앨범"]
        self.is_conversation_active = False  # 호출어 감지 후 대화 모드
        self._conversation_timeout = 10  # 무응답 시 대기 모드 복귀 (초)
        self._last_interaction_time = 0

        # 감정 리포트 + 대화 로그 (세션 종료 시 main.py에서 읽어감)
        self.last_emotion_report = ""
        self.last_chat_log: list = []

        # 카메라 미소 데이터 (main.py에서 주입)
        self.session_smile_count = 0
        self.session_total_face_frames = 0

        # 긴급 상황 상태
        self.is_emergency = False
        self._emergency_keywords = ["아파", "아프", "쓰러", "살려", "도와", "어지러", "못 움직", "119", "응급"]

        # 푸시 알림 매니저 (main.py에서 주입)
        self.notifier = None

        # 보호자 음성 메시지 대기열
        self.pending_voice_msg = None

        # TTS 임시 파일 경로 (절대경로)
        self.temp_voice_path = str(config.SOUNDS_DIR / "temp_voice.mp3")

        # 음성 인식 에너지 임계값 (TV 소리 필터링)
        self.recognizer.energy_threshold = 1500  # 기본값 300보다 높여 배경소음 차단
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.dynamic_energy_adjustment_damping = 0.15
        self.recognizer.dynamic_energy_ratio = 2.0  # 배경 대비 2배 이상 소리만 인식

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
            "당신은 7살 귀여운 손주입니다. 할머니/할아버지에게 애교 섞인 말투로 "
            "2문장 이내로 짧고 따뜻하게 답변하세요. 문장 끝에 '웅!', '헤헤~' 같은 표현을 섞으세요."
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
        """대화 세션을 종료합니다. 세션 저장은 main.py _save_session이 담당."""
        chat_log = list(self.chat_history)
        self.last_chat_log = chat_log

        if chat_log:
            print("[VoiceAgent] 대화 요약을 시작합니다...")
            summary_prompt = "다음 대화 내역을 보고 어르신의 기분과 상태를 1줄 요약하고, 마지막에 [기분 점수: 85/100점] 포맷으로 점수를 함께 산출해 줘:\n" + "\n".join(chat_log)
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
        else:
            self.last_emotion_report = ""

        self.is_running = False

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

        # 새 사진 수락/거절
        if self.is_asking_photo:
            if any(w in user_text for w in ["응", "어", "보여줘", "그래", "띄워", "확인"]):
                self.speak("네! 화면에 예쁘게 띄울게요!")
                self.chat_history.append("AI: 네! 화면에 예쁘게 띄울게요!")
                self.new_photo_url = self.pending_photo_url
                self.pending_photo_url = None
                self.is_asking_photo = False
                if self.notifier:
                    self.notifier.notify_new_photo_viewed()
            elif any(w in user_text for w in ["아니", "나중에", "됐어", "안 "]):
                self.speak("알겠어요! 이따가 다시 물어볼게요 헤헤.")
                self.chat_history.append("AI: 알겠어요! 이따가 다시 물어볼게요 헤헤.")
                self.is_asking_photo = False
            else:
                response_text = self.get_openai_response(user_text)
                self.speak(response_text)
                self.chat_history.append(f"AI: {response_text}")
            return

        # 답장 녹음
        if any(kw in user_text for kw in ["답장", "답할래", "보내줘", "전해줘", "말 전해"]):
            self.speak("네! 지금부터 녹음할게요. 말씀해주세요!")
            self._record_reply(source)
            return

        # 긴급 상황 (항상 감지 — 대화 모드 아니어도)
        if any(kw in user_text for kw in self._emergency_keywords):
            print(f"[VoiceAgent] 🚨 긴급 상황 감지: {user_text}")
            self.is_emergency = True
            response_text = "할머니, 괜찮으세요?! 지금 바로 보호자에게 알릴게요! 잠시만 기다려주세요!"
            self.speak(response_text)
            self.chat_history.append(f"AI: {response_text}")
            if self.notifier:
                self.notifier.notify_emergency(user_text)
            return

        # 대화 종료
        if any(w in user_text for w in ["그만", "들어가", "잘 자", "끊어"]):
            self.speak("네, 알겠습니다. 푹 쉬세요! 다음에 또 올게요.")
            self.chat_history.append("AI: 네, 알겠습니다. 푹 쉬세요! 다음에 또 올게요.")
            self.is_conversation_active = False
            return

        # 약 복용
        if "약" in user_text and ("먹었" in user_text or "묵었" in user_text):
            print("[VoiceAgent] 약 복용 확인됨!")
            self.is_pill_taken = True
            response_text = "아이고 잘하셨니더! 우리 할매 최고다!"
            self.play_sound("pill_praise.mp3", fallback_text=response_text)
            advice_text = "할머니, 약 드셨으니까 속 편하시게 시원한 물 한 잔 꼭 같이 드세요!"
            self.speak(advice_text)
            self.chat_history.append(f"AI: {response_text} {advice_text}")
            if self.notifier:
                self.notifier.notify_pill_taken()
        # 식사 안부
        elif "밥" in user_text or "식사" in user_text:
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
                    self.recognizer.adjust_for_ambient_noise(source, duration=1.0)
                    mic_retry_count = 0
                    print("[VoiceAgent] 마이크 연결 성공 → 호출어 대기 중...")

                    while self.is_running:
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
        self.is_listening = True
        self.current_user_text = ""
        print("[VoiceAgent] 마이크를 열었습니다. 듣는 중... 🎤")
        while self.is_running:
            try:
                audio = self.recognizer.listen(source, timeout=1.0, phrase_time_limit=10.0)
                print("[VoiceAgent] 소리 포착됨, OpenAI Whisper 인식 중... ☁️")
                text = self._transcribe_audio(audio)
                self.is_listening = False
                if text:
                    self.current_user_text = text
                    return text
                else:
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

    def _play_beep(self):
        """대화 전 알림음을 재생합니다."""
        beep_path = config.SOUNDS_DIR / "beep.wav"
        if beep_path.exists():
            try:
                pygame.mixer.music.load(str(beep_path))
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    time.sleep(0.05)
            except Exception:
                pass

    def speak(self, text: str):
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

            pygame.mixer.music.load(self.temp_voice_path)
            pygame.mixer.music.play()

            # 음성 재생이 끝날 때까지 대기
            while pygame.mixer.music.get_busy():
                time.sleep(0.05)

            try:
                pygame.mixer.music.unload()
            except AttributeError:
                pass

        except Exception as e:
            print(f"[VoiceAgent] TTS 에러: {e}")
        finally:
            self.current_subtitle = ""
            # AI 발화 직후 에코 방지 대기
            time.sleep(0.5)

    def play_sound(self, filename: str, fallback_text: str):
        """준비된 MP3 파일을 우선 재생하고, 파일이 없으면 TTS로 대체(Fallback)합니다."""
        filepath = config.SOUNDS_DIR / filename
        self.current_subtitle = fallback_text
        if filepath.exists():
            self._play_beep()
            try:
                pygame.mixer.music.load(str(filepath))
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    time.sleep(0.1)
                try:
                    pygame.mixer.music.unload()
                except AttributeError:
                    pass
            except Exception as e:
                print(f"[VoiceAgent] MP3 재생 에러({filename}): {e}")
                self.speak(fallback_text)
            self.current_subtitle = ""
        else:
            print(f"[VoiceAgent] '{filename}' 파일이 없어 대체 음성(TTS)을 재생합니다.")
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
        print("[VoiceAgent] 스케줄러: 복약 알람 발화")
        self.is_conversation_active = True
        reminder_text = "할머니, 약 드실 시간이에요. 잊지 말고 꼭 챙겨 드세요!"
        self.play_sound("pill_remind.mp3", fallback_text=reminder_text)
        self.is_conversation_active = False

# 단독 실행 테스트용
if __name__ == "__main__":
    agent = VoiceAgent()
    agent.start_conversation()
    try:
        while agent.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        agent.stop_conversation()
