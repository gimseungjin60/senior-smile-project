import os
import time
import threading
import speech_recognition as sr
import google.generativeai as genai
from gtts import gTTS
import pygame
import config

def init_gemini():
    if config.GEMINI_API_KEY:
        genai.configure(api_key=config.GEMINI_API_KEY)
    else:
        print("[경고] GEMINI_API_KEY가 설정되지 않았습니다.")

class VoiceAgent:
    def __init__(self):
        init_gemini()
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        self.recognizer = sr.Recognizer()
        self.is_pill_taken = False
        self.is_running = False
        self.is_listening = False
        self.chat_history = []
        self.current_subtitle = ""
        
        # Pygame 믹서 초기화 (음성 재생용)
        pygame.mixer.init()
        
        # 시스템 프롬프트 (페르소나)
        self.system_prompt = (
            "너는 홀로 계신 할머니/할아버지를 극진히 모시는 다정한 손주야. "
            "예의 바르면서도 살갑고 차분한 표준어를 사용해. "
            "추상적인 조언보다는 '냉장고의 찌개', '따뜻한 보리차' 등 "
            "매우 구체적이고 일상적인 가족의 맥락을 상상해서 덧붙여줘."
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
            print("[VoiceAgent] 대화 요약을 시작합니다...")
            summary_prompt = "다음 대화 내역을 보고 어르신의 기분과 상태를 1줄 요약하고, 마지막에 [기분 점수: 85/100점] 포맷으로 점수를 함께 산출해 줘:\n" + "\n".join(self.chat_history)
            try:
                response = self.model.generate_content(summary_prompt)
                print(f"\n==================================\n[AI 감정 리포트]: {response.text.strip()}\n==================================\n")
            except Exception as e:
                print(f"[요약 에러] {e}")
            self.chat_history.clear()
            
        self.is_running = False

    def _run_loop(self):
        """실제 대화를 수행하는 메인 루프 (쓰레드 내부 실행)"""
        print("[VoiceAgent] 대화 모드 시작")
        self.play_sound("greet_home.mp3", fallback_text="할머니, 저 왔어요. 오늘 하루 어떠셨어요?")
        
        try:
            with sr.Microphone() as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=1.0)
                while self.is_running:
                    user_text = self.listen(source)
                    
                    if not user_text:
                        continue
                        
                    print(f"[사용자] {user_text}")
                    
                    self.chat_history.append(f"사용자: {user_text}")
                    
                    # 대화 종료 키워드 (테스트용)
                    if any(word in user_text for word in ["그만", "들어가", "잘 자", "끊어"]):
                        response_text = "네, 알겠습니다. 푹 쉬세요! 다음에 또 올게요."
                        self.speak(response_text)
                        self.chat_history.append(f"AI: {response_text}")
                        self.stop_conversation()
                        break

                    # 약 복용 여부 체크 (예외 처리 로직)
                    if "약" in user_text and ("먹었" in user_text or "묵었" in user_text):
                        print("[VoiceAgent] 약 복용 확인됨!")
                        self.is_pill_taken = True
                        response_text = "아이고 잘하셨니더! 우리 할매 최고다!"
                        print(f"[AI 손주] (mp3) {response_text}")
                        self.play_sound("pill_praise.mp3", fallback_text=response_text)
                        
                        advice_text = "할머니, 약 드셨으니까 속 편하시게 시원한 물 한 잔 꼭 같이 드세요!"
                        print(f"[AI 손주] {advice_text}")
                        self.speak(advice_text)
                        
                        self.chat_history.append(f"AI: {response_text} {advice_text}")
                    # 식사 안부 로직
                    elif "밥" in user_text or "식사" in user_text:
                        print("[VoiceAgent] 식사 안부 확인됨!")
                        response_text = "건강을 위해 식사는 꼭 챙겨 드세요."
                        print(f"[AI 손주] (mp3) {response_text}")
                        self.play_sound("meal_check.mp3", fallback_text=response_text)
                        self.chat_history.append(f"AI: {response_text}")
                    else:
                        response_text = self.get_gemini_response(user_text)
                        print(f"[AI 손주] {response_text}")
                        self.speak(response_text)
                        self.chat_history.append(f"AI: {response_text}")
                    
                    time.sleep(0.5)
        except Exception as e:
            print(f"🚨 [치명적 마이크 에러] 장치가 연결되어 있나요? {e}")
            self.is_listening = False
            time.sleep(2) # 짧은 후퇴
            
        print("[VoiceAgent] 대화 모드 종료")

    def listen(self, source) -> str:
        self.is_listening = True
        print("[VoiceAgent] 마이크를 열었습니다. 듣는 중... 🎤")
        # while루프는 main에서만 돌고, listen 내에서는 한 턴만 처리합니다.
        # timeout을 1초로 주어 is_running 플래그 전환을 허용하며 스레드 병목을 회피합니다.
        while self.is_running:
            try:
                audio = self.recognizer.listen(source, timeout=1.0, phrase_time_limit=10.0)
                print("[VoiceAgent] 소리 포착됨, 구글 인식 중... ☁️")
                text = self.recognizer.recognize_google(audio, language="ko-KR")
                self.is_listening = False
                return text
            except sr.WaitTimeoutError:
                # 1초 동안 말을 안 한 것임: 정상적인 루프백
                continue
            except sr.UnknownValueError:
                print("[VoiceAgent] 인식 실패 (마이크에 소리는 났으나 발음 불분명)")
                self.is_listening = False
                return ""
            except Exception as e:
                print(f"[VoiceAgent] 음성 인식 중 예측 불가 에러 발생: {e}")
                self.is_listening = False
                return ""
        
        self.is_listening = False
        return ""

    def get_gemini_response(self, text: str) -> str:
        prompt = f"{self.system_prompt}\n\n할머니/할아버지의 말: {text}\n다정한 손주로서 대답해줘:"
        try:
            response = self.model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            print(f"[VoiceAgent] Gemini API 에러: {e}")
            return "지금 연결이 조금 안 좋네요. 잠시 후에 다시 말씀드릴게요."

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
            tts = gTTS(text=text, lang="ko")
            filename = "temp_voice.mp3"
            tts.save(filename)
            
            pygame.mixer.music.load(filename)
            pygame.mixer.music.play()
            
            # 음성 재생이 끝날 때까지 대기
            while pygame.mixer.music.get_busy():
                time.sleep(0.1)
                
            try:
                # 재생 끝난 후 파일 잠금 해제 (pygame 버전에 따라 지원 안될수도 있음)
                pygame.mixer.music.unload()
            except AttributeError:
                pass
                
        except Exception as e:
            print(f"[VoiceAgent] TTS 에러: {e}")
        finally:
            self.current_subtitle = ""

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

    def trigger_pill_reminder(self):
        """스케줄러에 의해 호출되어 복약 독촉 멘트를 발생시킵니다."""
        print("[VoiceAgent] 스케줄러: 복약 알람 발화")
        reminder_text = "할머니, 약 드실 시간이에요. 잊지 말고 꼭 챙겨 드세요!"
        self.play_sound("pill_remind.mp3", fallback_text=reminder_text)

# 단독 실행 테스트용
if __name__ == "__main__":
    agent = VoiceAgent()
    agent.start_conversation()
    try:
        while agent.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        agent.stop_conversation()
