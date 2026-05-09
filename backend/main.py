import asyncio
import time
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
import os
import glob
import json
from typing import Optional
from PIL import Image, ImageDraw, ImageFont
import numpy as np

import cv2
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import datetime
import config
from voice_agent import VoiceAgent
from notification import NotificationManager
from pairing import PairingManager
from auth import signup, login, verify_token, get_user
from apscheduler.schedulers.background import BackgroundScheduler

# AI-bum 백엔드 연동
AIBUM_BACKEND_URL = "http://localhost:8001"
DEVICE_ID = config.DEVICE_ID
HEARTBEAT_INTERVAL = 60


class FaceDetector:
    """
    상태 머신: idle → greeting → active → idle

    - idle:     아무도 없음. 디지털 액자 표시.
    - greeting: 얼굴 감지됨. "안녕하세요!" 5초 표시 후 자동 active 전환.
    - active:   콘텐츠 표시. 얼굴 미감지 30초 경과 시 idle 복귀.
                active 중 얼굴 다시 보여도 greeting으로 돌아가지 않음 (세션 유지).
    """

    def __init__(self):
        self.net = cv2.dnn.readNetFromCaffe(config.PROTOTXT_PATH, config.MODEL_PATH)
        self.camera: Optional[cv2.VideoCapture] = None
        self.running = False
        self.current_status = "idle"
        self.clients: set[WebSocket] = set()
        self.latest_frame: Optional[bytes] = None

        # 연속 감지 스트릭 (idle → greeting 진입 확인용)
        self._detect_streak = 0

        # GREETING 진입 시각 (5초 타이머용)
        self._greeting_start: Optional[float] = None

        # ACTIVE 중 마지막 얼굴 감지 시각 (30초 타이머용)
        self._last_seen: Optional[float] = None

        # 세션 시작 시각 (ai-bum 이벤트 전송용)
        self._session_start: Optional[float] = None

        # 음성 대화 에이전트 인스턴스
        self.voice_agent: Optional[VoiceAgent] = None
        self._voice_lock = threading.Lock()

        # 페어링 + 푸시 알림 매니저
        self.pairing = PairingManager()
        self.notifier = NotificationManager()
        self.notifier.pairing = self.pairing

        # UI 관련 상태
        self.font_path = self._get_korean_font()
        self.photos_dir = Path(__file__).parent / "photos"
        self.photos_dir.mkdir(exist_ok=True)
        self.photo_files = []
        self.slide_index = 0
        self.last_slide_time = time.time()
        self.prev_slide_frame = None

        self.last_output_frame = None
        self.state_blend_start = time.time()
        self.prev_state = "idle"

        # 추가 UI 상태 변수
        self.last_subtitle = ""
        self.subtitle_start_time = 0.0

        self._load_photos()

        # 미소 감지 (OpenCV Haar cascade, 추가 의존성 없음)
        self.smile_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_smile.xml"
        )
        self._session_smile_frames = 0
        self._session_total_frames = 0
        self._smile_check_counter = 0

        # FER+ 감정 분류 (ONNX, 8개 클래스)
        self.emotion_labels = [
            "neutral", "happiness", "surprise", "sadness",
            "anger", "disgust", "fear", "contempt",
        ]
        emotion_model_path = config.MODELS_DIR / "emotion-ferplus-8.onnx"
        self.emotion_net = None
        if emotion_model_path.exists():
            try:
                self.emotion_net = cv2.dnn.readNetFromONNX(str(emotion_model_path))
                print("[FaceDetector] FER+ 모델 로드 완료")
            except Exception as e:
                print(f"[FaceDetector] FER+ 로드 실패 (smile만 사용): {e}")
        self._session_emotion_counts: dict = {}
        self._emotion_check_counter = 0
        self._last_emotion_label: Optional[str] = None
        self._emotion_transition_count = 0

        # last_detection Firestore 갱신 throttle (30초 간격)
        self._last_detection_write = 0.0
        self._greeting_state_file = Path(__file__).parent / "greeting_state.json"

    def _should_play_daily_greeting(self) -> bool:
        today = datetime.date.today().isoformat()
        try:
            if self._greeting_state_file.exists():
                data = json.loads(self._greeting_state_file.read_text(encoding="utf-8"))
                if data.get("last_greeting_date") == today:
                    return False
        except Exception:
            pass
        try:
            self._greeting_state_file.write_text(
                json.dumps({"last_greeting_date": today}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
        return True

    def _load_photos(self):
        self.photo_files = glob.glob(str(self.photos_dir / "*.jpg")) + glob.glob(str(self.photos_dir / "*.png"))
        if self.photo_files:
            self.prev_slide_frame = self._resize_and_crop(cv2.imread(self.photo_files[0]))
        else:
            self.prev_slide_frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    def _resize_and_crop(self, image, target_w=1280, target_h=720):
        if image is None:
            return np.zeros((target_h, target_w, 3), dtype=np.uint8)
        h, w = image.shape[:2]
        scale = max(target_w / w, target_h / h)
        new_w, new_h = int(w * scale), int(h * scale)
        resized = cv2.resize(image, (new_w, new_h))
        x = (new_w - target_w) // 2
        y = (new_h - target_h) // 2
        return resized[y:y+target_h, x:x+target_w]

    def _draw_pillow_text(self, bgr_img, text, position, size, color=(255, 255, 0), stroke_width=3, stroke_fill="black"):
        rgb_img = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb_img)
        draw = ImageDraw.Draw(pil_img)
        try:
            font = ImageFont.truetype(self.font_path, size)
        except IOError:
            font = ImageFont.load_default()
        draw.text(position, text, font=font, fill=color, stroke_width=stroke_width, stroke_fill=stroke_fill)
        return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    async def broadcast(self, status: str):
        messages = {
            "idle":     "대기 모드",
            "greeting": "어르신 감지! 인사 모드",
            "active":   "콘텐츠 모드",
        }
        subtitle = ""
        is_listening = False
        is_pill_taken = False
        if self.voice_agent:
            subtitle = self.voice_agent.current_subtitle or ""
            is_listening = self.voice_agent.is_listening
            is_pill_taken = self.voice_agent.is_pill_taken

        payload = {
            "status": status,
            "message": messages.get(status, ""),
            "detected": status in ("greeting", "active"),
            "subtitle": subtitle,
            "isListening": is_listening,
            "isPillTaken": is_pill_taken,
            "pairing": self.pairing.get_status(),
        }
        disconnected = set()
        for client in self.clients:
            try:
                await client.send_json(payload)
            except Exception:
                disconnected.add(client)
        self.clients -= disconnected
        logger.info(f"상태 전환 → {status}")

    async def broadcast_voice_state(self):
        """음성 에이전트의 자막/리스닝 상태를 프론트엔드에 전송"""
        if not self.clients:
            return

        with self._voice_lock:
            if not self.voice_agent:
                return
            subtitle = self.voice_agent.current_subtitle or ""
            is_listening = self.voice_agent.is_listening
            is_pill_taken = self.voice_agent.is_pill_taken
            is_emergency = getattr(self.voice_agent, 'is_emergency', False)
            user_text = getattr(self.voice_agent, 'current_user_text', "")
            new_photo_url = getattr(self.voice_agent, 'new_photo_url', None)
            is_conversation_active = getattr(self.voice_agent, 'is_conversation_active', False)
            if new_photo_url:
                self.voice_agent.new_photo_url = None

        voice_state = (subtitle, is_listening, is_pill_taken, user_text, is_emergency, is_conversation_active)
        if voice_state == getattr(self, '_last_voice_state', None) and not new_photo_url:
            return
        self._last_voice_state = voice_state

        payload = {
            "type": "voice",
            "subtitle": subtitle,
            "userText": user_text,
            "isListening": is_listening,
            "isPillTaken": is_pill_taken,
            "isEmergency": is_emergency,
            "isConversationActive": is_conversation_active,
        }

        if new_photo_url:
            payload["newPhotoUrl"] = new_photo_url

        disconnected = set()
        for client in self.clients:
            try:
                await client.send_json(payload)
            except Exception:
                disconnected.add(client)
        self.clients -= disconnected

    async def broadcast_reminder(self, reminder_type: str, message: str):
        """리마인더를 프론트엔드에 전송 (컨텐츠 영역 교체용)"""
        TITLES = {
            "morning": "좋은 아침이에요!",
            "pill":    "약 드실 시간이에요!",
            "lunch":   "점심 시간이에요!",
            "activity":"스트레칭 시간이에요!",
            "dinner":  "저녁 시간이에요!",
            "night":   "편안한 밤 되세요",
        }
        payload = {
            "type": "reminder",
            "reminderType": reminder_type,
            "title": TITLES.get(reminder_type, message),
            "message": message,
            "time": datetime.datetime.now().strftime("%H:%M"),
        }
        disconnected = set()
        for client in self.clients:
            try:
                await client.send_json(payload)
            except Exception:
                disconnected.add(client)
        self.clients -= disconnected
        logger.info(f"[리마인더] {reminder_type} 프론트엔드 전송")

    async def broadcast_notification(self, title: str, body: str, event_type: str):
        """푸시 알림용 메시지를 WebSocket으로도 브로드캐스트 (웹 알림용)"""
        payload = {
            "type": "notification",
            "title": title,
            "body": body,
            "eventType": event_type,
            "time": datetime.datetime.now().strftime("%H:%M"),
        }
        disconnected = set()
        for client in self.clients:
            try:
                await client.send_json(payload)
            except Exception:
                disconnected.add(client)
        self.clients -= disconnected
        logger.info(f"[웹알림] {title} 프론트엔드 전송")

    def _detect_and_encode(self, raw_frame):
        """DNN 얼굴 감지 + 1280x720 UI 렌더링 + JPEG 인코딩"""
        target_w, target_h = 1280, 720
        frame = self._resize_and_crop(raw_frame, target_w, target_h)

        blob = cv2.dnn.blobFromImage(raw_frame, 1.0, (300, 300), (104, 177, 123))
        self.net.setInput(blob)
        detections = self.net.forward()

        face_found = False
        raw_h, raw_w = raw_frame.shape[:2]
        scale = max(target_w / raw_w, target_h / raw_h)
        x_offset = (int(raw_w * scale) - target_w) // 2
        y_offset = (int(raw_h * scale) - target_h) // 2

        last_face_box = None  # 미소 감지용 raw 좌표

        for i in range(detections.shape[2]):
            confidence = detections[0, 0, i, 2]
            if confidence > config.DNN_CONFIDENCE:
                face_found = True

                if self.current_status in ("greeting", "active"):
                    box = detections[0, 0, i, 3:7] * [raw_w, raw_h, raw_w, raw_h]
                    bx1, by1, bx2, by2 = box.astype(int)
                    last_face_box = (bx1, by1, bx2, by2)

                    x1 = int(bx1 * scale) - x_offset
                    y1 = int(by1 * scale) - y_offset
                    x2 = int(bx2 * scale) - x_offset
                    y2 = int(by2 * scale) - y_offset
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 0), 2)

        # 미소 감지 + FER+ 감정 분류: 5프레임마다 1회, raw 얼굴 ROI 사용
        if face_found and last_face_box and self.current_status in ("greeting", "active"):
            self._smile_check_counter = (self._smile_check_counter + 1) % 5
            if self._smile_check_counter == 0:
                self._session_total_frames += 1
                bx1, by1, bx2, by2 = last_face_box
                face_roi = raw_frame[max(0, by1):by2, max(0, bx1):bx2]
                if face_roi.size > 0:
                    gray_roi = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
                    smiles = self.smile_cascade.detectMultiScale(gray_roi, 1.7, 22)
                    if len(smiles) > 0:
                        self._session_smile_frames += 1
                    # FER+ 추론 (모델 로드됐을 때만)
                    if self.emotion_net is not None:
                        self._emotion_check_counter = (self._emotion_check_counter + 1) % 5
                        if self._emotion_check_counter == 0:
                            try:
                                resized = cv2.resize(gray_roi, (64, 64))
                                blob = resized.reshape(1, 1, 64, 64).astype("float32")
                                self.emotion_net.setInput(blob)
                                out = self.emotion_net.forward()[0]
                                label = self.emotion_labels[int(out.argmax())]
                                self._session_emotion_counts[label] = (
                                    self._session_emotion_counts.get(label, 0) + 1
                                )
                                if self._last_emotion_label and self._last_emotion_label != label:
                                    self._emotion_transition_count += 1
                                self._last_emotion_label = label
                                print(f"[FER+] {label} (누적: {dict(self._session_emotion_counts)})")
                            except Exception:
                                pass

        target_frame = frame.copy()
        now = time.time()

        if self.current_status == "idle":
            if not self.photo_files:
                self._load_photos()

            if self.photo_files:
                if now - self.last_slide_time > 5.0:
                    self.slide_index = (self.slide_index + 1) % len(self.photo_files)
                    self.last_slide_time = now
                    self.prev_slide_frame = target_frame.copy() if 'slide_frame' in locals() else self.prev_slide_frame

                slide_frame = self._resize_and_crop(cv2.imread(self.photo_files[self.slide_index]))

                slide_alpha = min(1.0, (now - self.last_slide_time) / 1.0)
                target_frame = cv2.addWeighted(self.prev_slide_frame, 1.0 - slide_alpha, slide_frame, slide_alpha, 0)
                self.prev_slide_frame = target_frame
            else:
                target_frame = np.zeros((target_h, target_w, 3), dtype=np.uint8)

        if self.current_status in ("greeting", "active"):
            if self.voice_agent and self.voice_agent.current_subtitle:
                if self.voice_agent.current_subtitle != self.last_subtitle:
                    self.last_subtitle = self.voice_agent.current_subtitle
                    self.subtitle_start_time = now

                char_count = int((now - self.subtitle_start_time) * 15)
                display_text = self.last_subtitle[:char_count]

                overlay = target_frame.copy()
                bar_y1 = int(target_h * 0.8)
                cv2.rectangle(overlay, (0, bar_y1), (target_w, target_h), (0, 0, 0), -1)
                cv2.addWeighted(overlay, 150/255.0, target_frame, 1.0 - 150/255.0, 0, target_frame)

                if display_text:
                    target_frame = self._draw_pillow_text(
                        target_frame,
                        display_text,
                        (50, bar_y1 + 25),
                        size=54,
                        color=(255, 255, 0)
                    )
            else:
                self.last_subtitle = ""

        if self.prev_state != self.current_status:
            self.state_blend_start = now
            self.prev_state = self.current_status

        blend_alpha = min(1.0, (now - self.state_blend_start) / 1.0)

        if self.last_output_frame is None:
            self.last_output_frame = target_frame

        final_frame = cv2.addWeighted(self.last_output_frame, 1.0 - blend_alpha, target_frame, blend_alpha, 0)

        if blend_alpha >= 1.0:
            self.last_output_frame = target_frame.copy()

        _, jpeg = cv2.imencode('.jpg', final_frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return face_found, jpeg.tobytes()

    # === AI-bum 백엔드 연동 ===

    async def _send_event(self, event_type: str, **kwargs):
        """AI-bum 백엔드로 감지 이벤트 전송 (실패해도 무시)"""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"{AIBUM_BACKEND_URL}/api/events", json={
                    "device_id": DEVICE_ID,
                    "type": event_type,
                    **kwargs,
                })
            logger.info(f"이벤트 전송: {event_type}")
        except Exception as e:
            logger.warning(f"이벤트 전송 실패 (무시): {e}")

    async def _send_heartbeat(self):
        """AI-bum 백엔드로 하트비트 전송"""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"{AIBUM_BACKEND_URL}/api/heartbeat", json={
                    "device_id": DEVICE_ID,
                    "current_state": self.current_status,
                })
        except Exception:
            pass

    async def _heartbeat_loop(self):
        """60초마다 하트비트 전송"""
        while self.running:
            await self._send_heartbeat()
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    # === 상태 전환 ===

    async def _transition(self, new_status: str):
        old_status = self.current_status
        self.current_status = new_status

        # AI-bum 백엔드에 세션 이벤트 전송 + 푸시 알림
        if new_status == "greeting" and old_status == "idle":
            self._session_start = time.time()
            self._session_smile_frames = 0
            self._session_total_frames = 0
            self._session_emotion_counts = {}
            self._emotion_check_counter = 0
            self._last_emotion_label = None
            self._emotion_transition_count = 0
            await self._send_event("session_start")
            self.notifier.notify_session_start()
        elif new_status == "idle" and old_status == "active":
            duration = 0
            if self._session_start:
                duration = int(time.time() - self._session_start)
                self._session_start = None

            emotion_report = ""
            chat_log = []
            with self._voice_lock:
                if self.voice_agent:
                    self.voice_agent.session_smile_count = self._session_smile_frames
                    self.voice_agent.session_total_face_frames = self._session_total_frames
                    self.voice_agent.stop_conversation()
                    emotion_report = self.voice_agent.last_emotion_report
                    chat_log = list(self.voice_agent.last_chat_log)
                    self.voice_agent.chat_history.clear()
                    self.voice_agent.is_emergency = False
                    self.voice_agent.is_pill_taken = False

            await self._send_event(
                "session_end",
                duration_seconds=duration,
                smile_count=self._session_smile_frames,
                smile_ratio=round(
                    self._session_smile_frames / max(1, self._session_total_frames), 3
                ),
            )
            self.notifier.notify_session_end(duration, emotion_report)

            await self._save_session(
                duration_seconds=duration,
                chat_log=chat_log,
                emotion_report=emotion_report,
                smile_frames=self._session_smile_frames,
                total_face_frames=self._session_total_frames,
                emotion_counts=dict(self._session_emotion_counts),
                emotion_transition_count=self._emotion_transition_count,
            )
            self._session_smile_frames = 0
            self._session_total_frames = 0
            self._session_emotion_counts = {}
            self._last_emotion_label = None
            self._emotion_transition_count = 0

        # 세션 저장 완료 후 브로드캐스트 (앱이 fetchSummary 시 데이터 준비 보장)
        await self.broadcast(new_status)

        # 음성 에이전트 시작 (idle 전환 시 종료는 위에서 처리됨)
        with self._voice_lock:
            if new_status == "greeting":
                if self.voice_agent is None:
                    self.voice_agent = VoiceAgent()
                    self.voice_agent.notifier = self.notifier
                self.voice_agent.start_conversation(
                    play_welcome=self._should_play_daily_greeting()
                )

    async def _save_session(self, *, duration_seconds, chat_log, emotion_report,
                            smile_frames, total_face_frames, emotion_counts,
                            emotion_transition_count):
        """카메라 인식 세션을 Firestore에 저장합니다. 대화 여부와 무관하게 호출됩니다."""
        db = _get_db()
        if not db:
            return
        try:
            smile_ratio = round(smile_frames / max(1, total_face_frames), 3)
            total_em = sum(emotion_counts.values()) or 1
            dominant = max(emotion_counts, key=emotion_counts.get) if emotion_counts else "neutral"
            
            neutral_ratio = round(emotion_counts.get("neutral", 0) / total_em, 3)
            positive_reaction_ratio = round(
                (emotion_counts.get("happiness", 0) + emotion_counts.get("surprise", 0)) / total_em, 3
            )
            conversation_turn_count = sum(1 for line in chat_log if line.startswith("사용자:"))
            pill_taken = False
            is_emergency = False
            with self._voice_lock:
                if self.voice_agent:
                    pill_taken = self.voice_agent.is_pill_taken
                    is_emergency = self.voice_agent.is_emergency
            has_conversation = len(chat_log) > 0
            reactivity = _compute_reactivity_metrics(
                duration_seconds=duration_seconds,
                total_face_frames=total_face_frames,
                emotion_counts=emotion_counts,
                emotion_transition_count=emotion_transition_count,
                has_conversation=has_conversation,
                conversation_turn_count=conversation_turn_count,
            )
            attention_flags = _derive_attention_flags(
                reactivity_status=reactivity["status"],
                duration_seconds=duration_seconds,
                has_conversation=has_conversation,
                conversation_turn_count=conversation_turn_count,
                neutral_ratio=neutral_ratio,
                positive_reaction_ratio=positive_reaction_ratio,
                is_emergency=is_emergency,
            )
            summary_text = (emotion_report or "").strip() or _build_signal_summary_text(
                reactivity_status=reactivity["status"],
                conversation_turn_count=conversation_turn_count,
                duration_seconds=duration_seconds,
                attention_flags=attention_flags,
            )
            db.collection("sessions").add({
                "device_id": config.DEVICE_ID,
                "created_at": datetime.datetime.now().isoformat(),
                "duration_seconds": duration_seconds,
                "messages": chat_log,
                "message_count": len(chat_log),
                "has_conversation": has_conversation,
                "conversation_turn_count": conversation_turn_count,
                "emotion_report": emotion_report,
                "pill_taken": pill_taken,
                "is_emergency": is_emergency,
                "smile_frame_count": smile_frames,
                "total_face_frames": total_face_frames,
                "smile_ratio": smile_ratio,
                "emotion_counts": emotion_counts,
                "dominant_emotion": dominant,
                "emotion_transition_count": emotion_transition_count,
                "neutral_ratio": neutral_ratio,
                "positive_reaction_ratio": positive_reaction_ratio,
                "reactivity_score_camera": reactivity["score"],
                "reactivity_band": reactivity["band"],
                "reactivity_status": reactivity["status"],
                "mood_score_camera": reactivity["score"],
                "signal_summary": {
                    "text": summary_text,
                    "flags": attention_flags,
                },
            })
            print(f"[FaceDetector] 세션 저장 완료 (대화:{len(chat_log)}건, 시간:{duration_seconds}초, 감정:{dominant})")
        except Exception as e:
            print(f"[FaceDetector] 세션 저장 실패: {e}")

    def _maybe_update_last_detection(self):
        """얼굴 감지 시 Firestore last_detection 필드를 30초 간격으로 갱신합니다."""
        now = time.time()
        if now - self._last_detection_write < 30:
            return
        db = _get_db()
        if not db:
            return
        self._last_detection_write = now
        try:
            db.collection("devices").document(config.DEVICE_ID).update({
                "last_detection": datetime.datetime.now().isoformat(),
            })
        except Exception:
            pass

    def _open_camera(self) -> bool:
        """카메라를 열고 성공 여부를 반환합니다."""
        if self.camera and self.camera.isOpened():
            self.camera.release()
        self.camera = cv2.VideoCapture(config.CAMERA_INDEX)
        if self.camera.isOpened():
            logger.info("카메라 열림")
            return True
        logger.error("카메라를 열 수 없습니다. 연결 상태를 확인하세요.")
        return False

    @staticmethod
    def _get_korean_font() -> Optional[str]:
        import platform
        system = platform.system()
        candidates = {
            "Darwin": [
                "/System/Library/Fonts/AppleSDGothicNeo.ttc",
                "/Library/Fonts/AppleGothic.ttf",
            ],
            "Linux": [
                "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            ],
            "Windows": ["C:/Windows/Fonts/malgun.ttf"],
        }.get(system, [])
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    async def run_detection_loop(self):
        if not self._open_camera():
            return

        logger.info("얼굴 감지 시작...")
        self.running = True
        loop = asyncio.get_event_loop()
        consecutive_failures = 0
        MAX_FAILURES = 30  # 연속 실패 시 카메라 재연결

        try:
            while self.running:
                ret, frame = await loop.run_in_executor(None, self.camera.read)
                if not ret:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_FAILURES:
                        logger.warning(f"카메라 프레임 {MAX_FAILURES}회 연속 실패. 재연결 시도...")
                        # 현재 active 상태면 idle로 복귀
                        if self.current_status != "idle":
                            await self._transition("idle")
                        if self._open_camera():
                            consecutive_failures = 0
                            logger.info("카메라 재연결 성공")
                        else:
                            logger.error("카메라 재연결 실패. 5초 후 재시도...")
                            await asyncio.sleep(5)
                    else:
                        await asyncio.sleep(0.5)
                    continue

                consecutive_failures = 0

                face_found, jpeg = await loop.run_in_executor(
                    None, self._detect_and_encode, frame
                )
                self.latest_frame = jpeg

                now = time.time()

                # 얼굴 감지 상태를 voice_agent에 전달 (마이크 게이팅)
                if self.voice_agent:
                    self.voice_agent.face_detected = face_found

                await self._update_state(face_found, now)
                await self.broadcast_voice_state()
                await asyncio.sleep(config.FRAME_INTERVAL)
        finally:
            if self.camera:
                self.camera.release()
                logger.info("카메라 해제")

    async def _update_state(self, face_found: bool, now: float):
        if face_found:
            self._maybe_update_last_detection()

        status = self.current_status

        if status == "idle":
            if face_found:
                if not self.pairing.is_paired:
                    self._detect_streak = 0
                    return
                self._detect_streak += 1
                if self._detect_streak >= config.DETECTION_CONFIRM_FRAMES:
                    self._detect_streak = 0
                    self._greeting_start = now
                    await self._transition("greeting")
            else:
                self._detect_streak = 0

        elif status == "greeting":
            if self._greeting_start and (now - self._greeting_start) >= config.GREETING_DURATION:
                self._last_seen = now
                await self._transition("active")

        elif status == "active":
            if face_found:
                self._last_seen = now
            else:
                if self._last_seen and (now - self._last_seen) >= config.ACTIVE_IDLE_TIMEOUT:
                    self._detect_streak = 0
                    await self._transition("idle")

    def stop(self):
        self.running = False
        if self.voice_agent is not None:
            self.voice_agent.stop_conversation()


detector = FaceDetector()
scheduler = BackgroundScheduler()

def scheduled_pill_reminder():
    logger.info("스케줄러 훅: 복약 시간 도달")
    # 프론트엔드에 리마인더 화면 표시
    try:
        loop = getattr(detector, '_loop', None)
        if loop:
            asyncio.run_coroutine_threadsafe(
                detector.broadcast_reminder("pill", "할머니, 약 드실 시간이에요. 잊지 말고 꼭 챙겨 드세요!"),
                loop
            )
    except Exception as e:
        logger.warning(f"[리마인더] 브로드캐스트 실패: {e}")

    with detector._voice_lock:
        if detector.voice_agent is None:
            detector.voice_agent = VoiceAgent()
            detector.voice_agent.notifier = detector.notifier
        
        # 콜백 함수 정의
        def _on_pill_taken():
            print("[Notification] 복약 완료 알림 DB 저장 시도")
            detector.notifier.notify_pill_taken()
            
            loop = getattr(detector, '_loop', None)
            if loop:
                print("[WebSocket] 복약 완료 알림 브로드캐스트 시작")
                asyncio.run_coroutine_threadsafe(
                    detector.broadcast_notification("약 복용 완료", "어르신이 방금 약을 드셨어요.", "pill_taken"),
                    loop
                )
            else:
                print("[WebSocket] 경고: 이벤트 루프(_loop)를 찾을 수 없어 브로드캐스트를 실패했습니다.")
        
        # 기존 voice_agent가 있더라도 콜백을 항상 최신으로 등록
        detector.voice_agent.on_pill_taken = _on_pill_taken

        if not detector.voice_agent.is_running:
            # 음성 에이전트 스레드를 시작해야 마이크가 작동합니다.
            detector.voice_agent.start_conversation(play_welcome=False)
            
        detector.voice_agent.trigger_pill_reminder()
        check_date = datetime.datetime.now() + datetime.timedelta(minutes=10)
        scheduler.add_job(
            _check_pill_missed, 'date', run_date=check_date,
            id="pill_check", replace_existing=True
        )


def _check_pill_missed():
    """복약 알림 후 10분 경과, 미복용 시 보호자 알림"""
    with detector._voice_lock:
        if detector.voice_agent:
            if detector.voice_agent.is_pill_taken:
                logger.info("복약 확인 완료. 미복용 알림 불필요.")
                return
            else:
                # 10분 경과 시 복약 확인 상태 해제
                detector.voice_agent.pill_check_active = False
                detector.voice_agent.is_conversation_active = False
    logger.info("복약 미확인. 보호자에게 푸시 알림 전송.")
    detector.notifier.notify_pill_missed()
    
    # 웹 앱용 WebSocket 알림 추가
    try:
        loop = getattr(detector, '_loop', None)
        if loop:
            asyncio.run_coroutine_threadsafe(
                detector.broadcast_notification("약 복용 미확인", "알림을 드렸지만 아직 약 복용이 확인되지 않았어요.", "pill_missed"),
                loop
            )
    except Exception as e:
        logger.warning(f"[웹알림] 브로드캐스트 실패: {e}")


def scheduled_routine(routine_type: str, message: str):
    """일일 루틴 스케줄러에 의해 호출됩니다."""
    if routine_type == "pill":
        return  # 기존 복약 스케줄러 사용
    logger.info(f"[루틴] {routine_type}: {message}")
    # 프론트엔드에 리마인더 화면 표시
    try:
        loop = getattr(detector, '_loop', None)
        if loop:
            asyncio.run_coroutine_threadsafe(
                detector.broadcast_reminder(routine_type, message),
                loop
            )
    except Exception as e:
        logger.warning(f"[리마인더] 브로드캐스트 실패: {e}")

    with detector._voice_lock:
        if detector.voice_agent is None:
            detector.voice_agent = VoiceAgent()
            detector.voice_agent.notifier = detector.notifier
        if not detector.voice_agent.is_running:
            detector.voice_agent.speak(message)


def _schedule_medication(med: dict):
    """개별 복약 항목을 APScheduler에 동적으로 등록합니다."""
    try:
        time_str = med.get("time", "")
        if not time_str or not med.get("enabled", True):
            return
        hr, mn = map(int, time_str.split(":"))
        med_name = med.get("name", "약")
        med_id = med["id"]

        def remind():
            msg = f"{med_name} 드실 시간이에요!"
            try:
                loop = getattr(detector, '_loop', None)
                if loop:
                    asyncio.run_coroutine_threadsafe(
                        detector.broadcast_reminder("pill", msg), loop
                    )
            except Exception as e:
                logger.warning(f"[복약] 브로드캐스트 실패: {e}")
            with detector._voice_lock:
                if detector.voice_agent is None:
                    detector.voice_agent = VoiceAgent()
                    detector.voice_agent.notifier = detector.notifier
                if not detector.voice_agent.is_running:
                    detector.voice_agent.start_conversation(play_welcome=False)
                    
                detector.voice_agent.trigger_pill_reminder()
                # 테스트를 위해 1분 후 복용 미확인 시 보호자에게 알림 (테스트 후 다시 10분으로 복구 예정)
                check_date = datetime.datetime.now() + datetime.timedelta(minutes=1)
                scheduler.add_job(
                    _check_pill_missed, 'date', run_date=check_date,
                    id=f"pill_check_{med_id}", replace_existing=True
                )

        scheduler.add_job(remind, 'cron', hour=hr, minute=mn,
                          id=f"med_{med_id}", replace_existing=True)
        logger.info(f"[복약] 스케줄 등록: {med_name} @ {time_str}")
    except Exception as e:
        logger.warning(f"[복약] 스케줄 등록 실패: {e}")


def _unschedule_medication(med_id: str):
    """개별 복약 항목을 APScheduler에서 제거합니다."""
    try:
        job_id = f"med_{med_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            logger.info(f"[복약] 스케줄 제거: {med_id}")
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    detector._loop = asyncio.get_event_loop()  # 스케줄러 스레드에서 코루틴 실행용

    # 복약 알림 스케줄러
    hr, mn = map(int, config.PILL_TIME.split(":"))
    scheduler.add_job(scheduled_pill_reminder, 'cron', hour=hr, minute=mn)

    # 일일 루틴 스케줄러
    for routine in config.DAILY_ROUTINES:
        if routine["type"] == "pill":
            continue  # 복약은 별도 처리
        r_hr, r_mn = map(int, routine["time"].split(":"))
        scheduler.add_job(
            scheduled_routine, 'cron', hour=r_hr, minute=r_mn,
            args=[routine["type"], routine["message"]],
            id=f"routine_{routine['type']}",
        )
        logger.info(f"  루틴 등록: {routine['time']} - {routine['type']}")

    scheduler.start()
    logger.info(f"스케줄러 시작 완료 (알람: {config.PILL_TIME}, 루틴: {len(config.DAILY_ROUTINES)}개)")

    # medications.json에서 동적 복약 스케줄 일괄 등록
    for med in _load_medications():
        _schedule_medication(med)

    detection_task = asyncio.create_task(detector.run_detection_loop())
    heartbeat_task = asyncio.create_task(detector._heartbeat_loop())
    yield
    detector.stop()
    detection_task.cancel()
    heartbeat_task.cancel()
    scheduler.shutdown()
    try:
        await detection_task
    except asyncio.CancelledError:
        pass
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    detector.clients.add(websocket)
    logger.info(f"WebSocket 연결 (총 {len(detector.clients)}개)")

    messages = {"idle": "대기 모드", "greeting": "어르신 감지! 인사 모드", "active": "콘텐츠 모드"}
    pairing_status = detector.pairing.get_status()
    await websocket.send_json({
        "status": detector.current_status,
        "message": messages.get(detector.current_status, ""),
        "detected": detector.current_status in ("greeting", "active"),
        "pairing": pairing_status,
    })

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        detector.clients.discard(websocket)
        logger.info(f"WebSocket 해제 (총 {len(detector.clients)}개)")


@app.get("/status")
async def get_status():
    return {"status": detector.current_status}


@app.get("/health")
async def health_check():
    """헬스체크 엔드포인트 — 앱 상태 전체 요약"""
    camera_ok = detector.camera is not None and detector.camera.isOpened() if detector.camera else False
    voice_ok = detector.voice_agent is not None and detector.voice_agent.is_running if detector.voice_agent else False

    return {
        "status": "ok" if detector.running else "degraded",
        "camera": "connected" if camera_ok else "disconnected",
        "voice_agent": "running" if voice_ok else "stopped",
        "current_state": detector.current_status,
        "ws_clients": len(detector.clients),
        "scheduler": "running" if scheduler.running else "stopped",
    }


# === 인증 API ===

def _get_db():
    """Firestore DB 인스턴스를 안전하게 가져옵니다."""
    import firebase_admin as fa
    if not fa._apps:
        try:
            from firebase_admin import credentials
            key_path = Path(__file__).parent / "serviceAccountKey.json"
            if key_path.exists():
                cred = credentials.Certificate(str(key_path))
                fa.initialize_app(cred)
            else:
                return None
        except Exception as e:
            logger.warning(f"[Firebase] 초기화 실패: {e}")
            return None
    from firebase_admin import firestore as fs
    return fs.client()


def require_auth(authorization: str = Header(None)) -> dict:
    """보호자 API 인증 의존성. Authorization: Bearer <token> 헤더 필요."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    token = authorization[7:]
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")
    return payload


@app.post("/api/auth/signup")
async def api_signup(payload: dict):
    """보호자 회원가입"""
    db = _get_db()
    return signup(
        db,
        email=payload.get("email", ""),
        password=payload.get("password", ""),
        name=payload.get("name", ""),
    )


@app.post("/api/auth/login")
async def api_login(payload: dict):
    """보호자 로그인"""
    db = _get_db()
    return login(
        db,
        email=payload.get("email", ""),
        password=payload.get("password", ""),
    )


@app.get("/api/auth/me")
async def api_me(user: dict = Depends(require_auth)):
    """현재 로그인된 사용자 정보"""
    db = _get_db()
    user_data = get_user(db, user["user_id"])
    if user_data:
        return {"success": True, "user": user_data}
    return {"success": True, "user": {"user_id": user["user_id"], "name": user["name"]}}


# === 페어링 API ===

@app.get("/api/pairing/status")
async def pairing_status():
    """현재 페어링 상태를 반환합니다."""
    return detector.pairing.get_status()


PAIRING_CODE_EXPIRY = 300


@app.post("/api/pairing/code")
async def generate_pairing_code():
    """새 페어링 코드를 생성합니다 (5분 유효)."""
    code = detector.pairing.generate_code()
    return {
        "code": code,
        "expires_in": PAIRING_CODE_EXPIRY,
        "device_id": detector.pairing.device_id,
    }


@app.post("/api/pairing/verify")
async def verify_pairing(request: Request, payload: dict):
    """보호자 앱에서 코드를 입력하면 매칭을 수행합니다."""
    code = payload.get("code", "")
    user_id = payload.get("user_id", "")
    user_name = payload.get("user_name", "")
    fcm_token = payload.get("fcm_token", "")

    if not code or not user_id or not user_name:
        return {"success": False, "error": "code, user_id, user_name 필드가 필요합니다."}

    client_ip = request.client.host if request.client else "?"
    result = detector.pairing.verify_and_pair(code, user_id, user_name, fcm_token, client_ip)

    # 매칭 성공 시 FCM 토큰 자동 등록 + WebSocket으로 시니어 앱에 알림
    if result.get("success"):
        if fcm_token:
            detector.notifier.register_token(fcm_token)

        # 시니어 앱(프론트엔드)에 페어링 완료 알림 전송
        pairing_msg = {
            "type": "pairing",
            "paired": True,
            "family_id": result.get("family_id"),
            "user_name": user_name,
            "status": detector.current_status,
            "pairing": detector.pairing.get_status(),
        }
        disconnected = set()
        for client in list(detector.clients):
            try:
                await client.send_json(pairing_msg)
            except Exception:
                disconnected.add(client)
        detector.clients -= disconnected
        logger.info(f"[Pairing] WebSocket으로 페어링 완료 알림 전송 ({len(detector.clients)}개 클라이언트)")

    return result


@app.post("/api/pairing/unpair")
async def unpair_device(user: dict = Depends(require_auth)):
    """페어링을 해제합니다."""
    detector.pairing.unpair()

    if detector.current_status != "idle":
        await detector._transition("idle")

    unpair_msg = {
        "type": "pairing",
        "paired": False,
        "pairing": detector.pairing.get_status(),
    }
    disconnected = set()
    for client in list(detector.clients):
        try:
            await client.send_json(unpair_msg)
        except Exception:
            disconnected.add(client)
    detector.clients -= disconnected
    logger.info("[Pairing] 페어링 해제 완료, WS 알림 전송")

    return {"success": True}


# === 알림 API ===

@app.post("/api/notifications/register")
async def register_push_token(payload: dict, user: dict = Depends(require_auth)):
    """보호자 앱에서 FCM 디바이스 토큰을 등록합니다."""
    token = payload.get("token")
    if not token:
        return {"success": False, "error": "token 필드가 필요합니다."}
    registered = detector.notifier.register_token(token)
    return {"success": True, "registered": registered, "total": detector.notifier.get_token_count()}


@app.post("/api/notifications/unregister")
async def unregister_push_token(payload: dict, user: dict = Depends(require_auth)):
    """보호자 앱에서 FCM 디바이스 토큰을 해제합니다."""
    token = payload.get("token")
    if not token:
        return {"success": False, "error": "token 필드가 필요합니다."}
    removed = detector.notifier.unregister_token(token)
    return {"success": True, "removed": removed, "total": detector.notifier.get_token_count()}


# === 날씨 API ===

_weather_cache = {"data": None, "fetched_at": 0}
WEATHER_CACHE_TTL = 1800  # 30분 캐시


@app.get("/api/weather")
async def get_weather():
    """현재 날씨 정보를 반환합니다 (OpenWeatherMap)."""
    now = time.time()

    # 캐시 유효하면 재사용
    if _weather_cache["data"] and (now - _weather_cache["fetched_at"]) < WEATHER_CACHE_TTL:
        return _weather_cache["data"]

    if not config.WEATHER_API_KEY:
        return {
            "temperature": "--",
            "condition": "API 키 없음",
            "icon": "❓",
        }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={
                    "q": config.WEATHER_CITY,
                    "appid": config.WEATHER_API_KEY,
                    "units": "metric",
                    "lang": "kr",
                },
            )
            res.raise_for_status()
            w = res.json()

        icon_map = {
            "Clear": "☀️", "Clouds": "☁️", "Rain": "🌧️",
            "Drizzle": "🌦️", "Thunderstorm": "⛈️", "Snow": "❄️",
            "Mist": "🌫️", "Fog": "🌫️", "Haze": "🌫️",
        }
        main_weather = w["weather"][0]["main"]
        result = {
            "temperature": f"{round(w['main']['temp'])}°",
            "condition": w["weather"][0]["description"],
            "icon": icon_map.get(main_weather, "🌤️"),
            "humidity": w["main"]["humidity"],
            "city": config.WEATHER_CITY,
        }
        _weather_cache["data"] = result
        _weather_cache["fetched_at"] = now
        return result
    except Exception as e:
        logger.warning(f"날씨 API 실패: {e}")
        if _weather_cache["data"]:
            return _weather_cache["data"]
        return {
            "temperature": "--",
            "condition": "조회 실패",
            "icon": "❓",
        }


# === 야간 모드 API ===

@app.get("/api/nightmode")
async def get_night_mode():
    """현재 야간 모드 여부를 반환합니다."""
    now = datetime.datetime.now()
    current_time = now.strftime("%H:%M")

    night_start = config.NIGHT_START
    night_end = config.NIGHT_END

    # 자정을 넘기는 경우 (22:00~07:00)
    if night_start > night_end:
        is_night = current_time >= night_start or current_time < night_end
    else:
        is_night = night_start <= current_time < night_end

    return {
        "is_night": is_night,
        "night_start": night_start,
        "night_end": night_end,
        "current_time": current_time,
    }


# === 루틴 API ===

@app.get("/api/routines")
async def get_routines():
    """일일 루틴 목록을 반환합니다."""
    return {"routines": config.DAILY_ROUTINES, "pill_time": config.PILL_TIME}


# === 복약 관리 API ===

_medications_file = Path(__file__).parent / "medications.json"


def _load_medications() -> list:
    import json
    if _medications_file.exists():
        return json.loads(_medications_file.read_text(encoding="utf-8"))
    return []


def _save_medications(meds: list):
    import json
    _medications_file.write_text(json.dumps(meds, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/medications")
async def list_medications(user: dict = Depends(require_auth)):
    """복약 목록을 반환합니다."""
    return {"medications": _load_medications()}


@app.post("/api/medications")
async def add_medication(data: dict, user: dict = Depends(require_auth)):
    """복약 항목을 추가합니다."""
    import uuid as _uuid
    meds = _load_medications()
    med = {
        "id": f"med_{_uuid.uuid4().hex[:8]}",
        "name": data.get("name", ""),
        "time": data.get("time", "09:00"),
        "dosage": data.get("dosage", ""),
        "notes": data.get("notes", ""),
        "enabled": True,
        "created_at": datetime.datetime.now().isoformat(),
    }
    meds.append(med)
    _save_medications(meds)
    _schedule_medication(med)
    return {"success": True, "medication": med}


@app.put("/api/medications/{med_id}")
async def update_medication(med_id: str, data: dict, user: dict = Depends(require_auth)):
    """복약 항목을 수정합니다."""
    meds = _load_medications()
    for med in meds:
        if med["id"] == med_id:
            if "name" in data: med["name"] = data["name"]
            if "time" in data: med["time"] = data["time"]
            if "dosage" in data: med["dosage"] = data["dosage"]
            if "notes" in data: med["notes"] = data["notes"]
            if "enabled" in data: med["enabled"] = data["enabled"]
            _save_medications(meds)
            _unschedule_medication(med_id)
            _schedule_medication(med)
            return {"success": True, "medication": med}
    return {"success": False, "message": "약을 찾을 수 없습니다."}


@app.delete("/api/medications/{med_id}")
async def delete_medication(med_id: str, user: dict = Depends(require_auth)):
    """복약 항목을 삭제합니다."""
    meds = _load_medications()
    meds = [m for m in meds if m["id"] != med_id]
    _save_medications(meds)
    _unschedule_medication(med_id)
    return {"success": True}


@app.get("/api/medications/history")
async def medication_history(limit: int = 30, user: dict = Depends(require_auth)):
    """복약 이력을 반환합니다 (세션 기반)."""
    try:
        import firebase_admin as fa
        if not fa._apps:
            return {"history": []}
        from firebase_admin import firestore as fs
        db = fs.client()
        docs = (
            db.collection("sessions")
            .where("device_id", "==", config.DEVICE_ID)
            .where("pill_taken", "==", True)
            .order_by("created_at", direction=fs.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        history = []
        for doc in docs:
            d = doc.to_dict()
            history.append({
                "date": d.get("created_at", ""),
                "pill_taken": True,
            })
        return {"history": history}
    except Exception as e:
        return {"history": [], "error": str(e)}


# === 음성 메시지 API ===

@app.post("/api/voice-messages/send")
async def send_voice_message(file: UploadFile = File(...), sender: str = "보호자", user: dict = Depends(require_auth)):
    """보호자가 어르신에게 음성 메시지를 보냅니다."""
    import uuid
    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
    filename = f"{msg_id}.webm"
    filepath = config.VOICE_MSG_DIR / filename

    content = await file.read()
    filepath.write_bytes(content)

    # 메타데이터 저장
    import json
    meta = {
        "id": msg_id,
        "sender": sender,
        "filename": filename,
        "direction": "to_senior",
        "played": False,
        "created_at": datetime.datetime.now().isoformat(),
    }
    meta_path = config.VOICE_MSG_DIR / f"{msg_id}.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # 액자에 알림 (voice_agent의 pending으로 전달)
    with detector._voice_lock:
        if detector.voice_agent:
            detector.voice_agent.pending_voice_msg = meta

    logger.info(f"[VoiceMsg] 보호자 → 어르신 메시지 저장: {msg_id}")
    return {"success": True, "message_id": msg_id}


@app.get("/api/voice-messages")
async def list_voice_messages(direction: str = "", user: dict = Depends(require_auth)):
    """음성 메시지 목록을 반환합니다."""
    import json
    messages = []
    for meta_file in sorted(config.VOICE_MSG_DIR.glob("*.json"), reverse=True):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if direction and meta.get("direction") != direction:
                continue
            messages.append(meta)
        except Exception:
            continue
    return {"messages": messages[:50]}


@app.get("/api/voice-messages/{msg_id}/audio")
async def get_voice_audio(msg_id: str, user: dict = Depends(require_auth)):
    """음성 메시지 오디오 파일을 반환합니다."""
    # webm 또는 mp3 파일 찾기
    for ext in ["webm", "mp3", "wav"]:
        filepath = config.VOICE_MSG_DIR / f"{msg_id}.{ext}"
        if filepath.exists():
            return FileResponse(filepath)
    return {"error": "파일을 찾을 수 없습니다."}


# === 사진 API ===

PHOTOS_DIR = Path(__file__).parent / "photos"
PHOTOS_DIR.mkdir(exist_ok=True)


@app.get("/api/photos")
async def list_photos(limit: int = 50, user: dict = Depends(require_auth)):
    """사진 목록을 반환합니다."""
    files = sorted(PHOTOS_DIR.glob("*.jpg"), key=lambda f: f.stat().st_mtime, reverse=True)
    files += sorted(PHOTOS_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)
    photos = []
    for f in files[:limit]:
        photos.append({
            "id": f.stem,
            "filename": f.name,
            "created_at": datetime.datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        })
    return {"photos": photos}


@app.post("/api/photos/upload")
async def upload_photo(file: UploadFile = File(...), user: dict = Depends(require_auth)):
    """보호자가 어르신 액자에 사진을 업로드합니다."""
    import uuid as _uuid
    ext = Path(file.filename).suffix.lower() if file.filename else ".jpg"
    if ext not in (".jpg", ".jpeg", ".png"):
        ext = ".jpg"
    photo_id = f"photo_{_uuid.uuid4().hex[:8]}"
    filename = f"{photo_id}{ext}"
    filepath = PHOTOS_DIR / filename

    content = await file.read()
    filepath.write_bytes(content)

    # 사진 목록 다시 로드
    detector._load_photos()

    # WebSocket으로 새 사진 알림
    new_photo_url = f"/api/photos/{filename}"
    payload = {"type": "new_photo", "newPhotoUrl": new_photo_url}
    disconnected = set()
    for client in detector.clients:
        try:
            await client.send_json(payload)
        except Exception:
            disconnected.add(client)
    detector.clients -= disconnected

    logger.info(f"[Photo] 사진 업로드 완료: {filename}")
    return {"success": True, "filename": filename, "url": new_photo_url}


@app.get("/api/photos/{filename}")
async def get_photo(filename: str):
    """개별 사진 파일을 반환합니다."""
    filepath = PHOTOS_DIR / filename
    if filepath.exists():
        return FileResponse(filepath)
    return {"error": "사진을 찾을 수 없습니다."}


# === 보호자 리포트 API ===

def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def _compute_reactivity_metrics(*, duration_seconds: int, total_face_frames: int, emotion_counts: dict,
                                emotion_transition_count: int, has_conversation: bool,
                                conversation_turn_count: int) -> dict:
    total_em = sum(emotion_counts.values())
    if total_face_frames < 3 or total_em < 3 or duration_seconds < 10:
        return {"score": 0, "band": "low", "status": "insufficient_data"}

    neutral_ratio = emotion_counts.get("neutral", 0) / max(total_em, 1)
    positive_ratio = (
        emotion_counts.get("happiness", 0) + emotion_counts.get("surprise", 0)
    ) / max(total_em, 1)

    score = 20.0
    score += min(duration_seconds / 240.0, 1.0) * 20.0
    score += min(emotion_transition_count / 6.0, 1.0) * 20.0
    score += positive_ratio * 20.0
    if has_conversation:
        score += 12.0
    score += min(conversation_turn_count / 4.0, 1.0) * 8.0

    if not has_conversation and neutral_ratio > 0.85:
        score -= 8.0
    if duration_seconds >= 120 and emotion_transition_count >= 2:
        score += 6.0

    score = round(_clamp(score))
    if score >= 70:
        status = "high"
    elif score >= 45:
        status = "normal"
    else:
        status = "low"
    return {"score": score, "band": status, "status": status}


def _get_sessions_for_date(db, device_id: str, date_str: str) -> list:
    """특정 날짜의 세션 목록을 반환합니다."""
    # created_at이 문자열 또는 Firestore Timestamp일 수 있으므로 둘 다 처리
    start = f"{date_str}T00:00:00"
    end = f"{date_str}T23:59:59"
    try:
        docs = (
            db.collection("sessions")
            .where("device_id", "==", device_id)
            .where("created_at", ">=", start)
            .where("created_at", "<=", end)
            .stream()
        )
        sessions = []
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            # Firestore Timestamp → ISO 문자열 변환
            ca = data.get("created_at")
            if hasattr(ca, "isoformat"):
                data["created_at"] = ca.isoformat()
            sessions.append(data)
        return sessions
    except Exception:
        # Timestamp 타입 불일치 시 전체 조회 후 필터링
        docs = (
            db.collection("sessions")
            .where("device_id", "==", device_id)
            .stream()
        )
        sessions = []
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            ca = data.get("created_at")
            if hasattr(ca, "isoformat"):
                ca_str = ca.isoformat()
                data["created_at"] = ca_str
            else:
                ca_str = str(ca) if ca else ""
            if ca_str.startswith(date_str):
                sessions.append(data)
        return sessions


def _aggregate_emotions(sessions: list) -> dict:
    """세션 목록의 emotion_counts를 합산합니다."""
    counts: dict = {}
    for s in sessions:
        for k, v in (s.get("emotion_counts") or {}).items():
            counts[k] = counts.get(k, 0) + v
    return counts


def _dominant_emotion(counts: dict) -> str:
    return max(counts, key=counts.get) if counts else "neutral"


def _average_metric(sessions: list, key: str) -> float:
    values = [s.get(key) for s in sessions if isinstance(s.get(key), (int, float))]
    return round(sum(values) / len(values), 1) if values else 0.0


def _aggregate_attention_flags(sessions: list) -> dict:
    counts = {}
    for session in sessions:
        signal_summary = session.get("signal_summary") or {}
        for flag in signal_summary.get("flags", []):
            counts[flag] = counts.get(flag, 0) + 1
    return counts


def _derive_attention_flags(*, reactivity_status: str, duration_seconds: int, has_conversation: bool,
                            conversation_turn_count: int, neutral_ratio: float,
                            positive_reaction_ratio: float, is_emergency: bool) -> list[str]:
    flags = []
    if is_emergency:
        flags.append("emergency")
    if reactivity_status == "insufficient_data":
        flags.append("insufficient_data")
    if reactivity_status == "low":
        flags.append("low_reactivity")
    if duration_seconds >= 120 and not has_conversation:
        flags.append("no_conversation")
    if neutral_ratio >= 0.85 and positive_reaction_ratio <= 0.15 and not has_conversation:
        flags.append("flat_affect")
    if conversation_turn_count >= 3:
        flags.append("engaged_conversation")
    return flags


def _build_signal_summary_text(*, reactivity_status: str, conversation_turn_count: int,
                               duration_seconds: int, attention_flags: list[str]) -> str:
    if "emergency" in attention_flags:
        return "도움 요청 신호가 감지되어 즉시 확인이 필요한 상태입니다."
    if reactivity_status == "insufficient_data":
        return "오늘은 표정과 대화 데이터가 충분하지 않아 해석을 보류했습니다."
    if reactivity_status == "high":
        if conversation_turn_count >= 2:
            return "대화 참여와 표정 변화가 함께 보여 반응이 비교적 좋은 편이었습니다."
        return "표정 변화가 비교적 잘 관찰되어 반응성이 높은 편으로 보입니다."
    if reactivity_status == "normal":
        if conversation_turn_count >= 1:
            return "평소 수준의 반응과 대화 참여가 확인되었습니다."
        return "전반적으로 평소 수준의 반응이 관찰되었습니다."
    if duration_seconds >= 120:
        return "반응이 평소보다 적어 안부 확인이 필요할 수 있습니다."
    return "짧은 관찰 동안 반응이 크지 않았습니다."


def _status_label(status: str) -> str:
    return {
        "high": "반응 좋음",
        "normal": "평소 수준",
        "low": "반응 적음",
        "insufficient_data": "데이터 부족",
    }.get(status, "데이터 부족")


def _summarize_interaction(sessions: list) -> dict:
    conversation_sessions = sum(1 for s in sessions if s.get("has_conversation"))
    turn_count = sum(s.get("conversation_turn_count", 0) for s in sessions)
    total_duration = sum(s.get("duration_seconds", 0) for s in sessions)
    return {
        "visit_count": len(sessions),
        "conversation_count": conversation_sessions,
        "conversation_turn_count": turn_count,
        "total_detection_seconds": total_duration,
    }


def _reactivity_status_counts(sessions: list) -> dict:
    counts = {"high": 0, "normal": 0, "low": 0, "insufficient_data": 0}
    for session in sessions:
        status = session.get("reactivity_status", "insufficient_data")
        if status not in counts:
            status = "insufficient_data"
        counts[status] += 1
    return counts


def _dominant_reactivity_status(sessions: list) -> str:
    counts = _reactivity_status_counts(sessions)
    return max(counts, key=counts.get) if any(counts.values()) else "insufficient_data"


def _build_signal_breakdown(sessions: list) -> dict:
    total_emotions = _aggregate_emotions(sessions)
    return {
        "emotion_counts": total_emotions,
        "dominant_emotion": _dominant_emotion(total_emotions),
        "avg_neutral_ratio": _average_metric(sessions, "neutral_ratio"),
        "avg_positive_reaction_ratio": _average_metric(sessions, "positive_reaction_ratio"),
        "avg_smile_ratio": _average_metric(sessions, "smile_ratio"),
        "avg_transition_count": _average_metric(sessions, "emotion_transition_count"),
    }


def _baseline_comparison(today_sessions: list, baseline_sessions: list) -> dict:
    today_turns = sum(s.get("conversation_turn_count", 0) for s in today_sessions)
    baseline_turns = sum(s.get("conversation_turn_count", 0) for s in baseline_sessions)
    today_duration = sum(s.get("duration_seconds", 0) for s in today_sessions)
    baseline_duration = sum(s.get("duration_seconds", 0) for s in baseline_sessions)
    baseline_status = _dominant_reactivity_status(baseline_sessions) if baseline_sessions else "insufficient_data"
    return {
        "baseline_reactivity_status": baseline_status,
        "visit_delta": len(today_sessions) - len(baseline_sessions),
        "conversation_turn_delta": today_turns - baseline_turns,
        "detection_seconds_delta": today_duration - baseline_duration,
    }


def _reactivity_change(current_status: str, baseline_status: str) -> str:
    order = {"insufficient_data": 0, "low": 1, "normal": 2, "high": 3}
    if current_status == "insufficient_data":
        return "insufficient_data"
    diff = order.get(current_status, 0) - order.get(baseline_status, 0)
    if diff >= 1:
        return "improved"
    if diff <= -1:
        return "declined"
    return "stable"


def _latest_signal_text(sessions: list, fallback_status: str) -> str:
    for session in reversed(sessions):
        signal_summary = session.get("signal_summary") or {}
        text = (signal_summary.get("text") or "").strip()
        if text:
            return text
    return _build_signal_summary_text(
        reactivity_status=fallback_status,
        conversation_turn_count=0,
        duration_seconds=0,
        attention_flags=[],
    )


@app.get("/api/reports/summary")
async def get_report_summary(device_id: str = "", user: dict = Depends(require_auth)):
    """홈 화면용 요약 데이터를 반환합니다."""
    did = device_id or config.DEVICE_ID
    today = datetime.date.today().isoformat()

    result = {
        "device": {"device_id": did, "last_detection": None},
        "today": {
            "mood_score": 0,
            "mood_score_camera": 0,
            "mood_score_llm": 0,
            "total_detection_seconds": 0,
            "total_smiles": 0,
            "session_count": 0,
            "visit_count": 0,
            "conversation_count": 0,
            "conversation_turn_count": 0,
            "emotion_counts": {},
            "dominant_emotion": "neutral",
            "reactivity_status": "insufficient_data",
            "reactivity_change": "insufficient_data",
            "interaction_summary": {},
            "attention_flags": [],
            "signal_breakdown": {},
            "baseline_comparison": {},
            "signal_summary": {"text": "오늘은 표정과 대화 데이터가 충분하지 않아 해석을 보류했습니다.", "flags": []},
        },
        "weekly_chart": [],
    }

    try:
        import firebase_admin as fa
        if not fa._apps:
            return result
        from firebase_admin import firestore as fs
        db = fs.client()

        # 디바이스 정보
        dev_doc = db.collection("devices").document(did).get()
        if dev_doc.exists:
            dev_data = dev_doc.to_dict()
            last_det = dev_data.get("last_detection")
            if last_det:
                result["device"]["last_detection"] = (
                    last_det.isoformat() if hasattr(last_det, "isoformat") else str(last_det)
                )

        today_sessions = _get_sessions_for_date(db, did, today)
        baseline_sessions = []
        for i in range(1, 8):
            baseline_sessions.extend(
                _get_sessions_for_date(db, did, (datetime.date.today() - datetime.timedelta(days=i)).isoformat())
            )

        interaction = _summarize_interaction(today_sessions)
        breakdown = _build_signal_breakdown(today_sessions)
        reactivity_status = _dominant_reactivity_status(today_sessions)
        attention_counts = _aggregate_attention_flags(today_sessions)
        baseline = _baseline_comparison(today_sessions, baseline_sessions)
        result["today"]["session_count"] = len(today_sessions)
        result["today"]["visit_count"] = interaction["visit_count"]
        result["today"]["conversation_count"] = interaction["conversation_count"]
        result["today"]["conversation_turn_count"] = interaction["conversation_turn_count"]
        result["today"]["mood_score"] = 0
        result["today"]["mood_score_camera"] = 0
        result["today"]["mood_score_llm"] = 0
        result["today"]["total_smiles"] = sum(s.get("smile_frame_count", 0) for s in today_sessions)
        result["today"]["total_detection_seconds"] = interaction["total_detection_seconds"]
        result["today"]["emotion_counts"] = breakdown["emotion_counts"]
        result["today"]["dominant_emotion"] = breakdown["dominant_emotion"]
        result["today"]["reactivity_status"] = reactivity_status
        result["today"]["reactivity_change"] = _reactivity_change(
            reactivity_status, baseline.get("baseline_reactivity_status", "insufficient_data")
        )
        result["today"]["interaction_summary"] = {
            **interaction,
            "reactivity_label": _status_label(reactivity_status),
        }
        result["today"]["attention_flags"] = sorted(
            attention_counts,
            key=lambda item: (-attention_counts[item], item),
        )
        result["today"]["signal_breakdown"] = breakdown
        result["today"]["baseline_comparison"] = baseline
        result["today"]["signal_summary"] = {
            "text": _latest_signal_text(today_sessions, reactivity_status),
            "flags": result["today"]["attention_flags"],
        }

        weekly = []
        for i in range(6, -1, -1):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            day_sessions = _get_sessions_for_date(db, did, d) if i > 0 else today_sessions
            interaction = _summarize_interaction(day_sessions)
            flag_counts = _aggregate_attention_flags(day_sessions)
            day_status = _dominant_reactivity_status(day_sessions)
            weekly.append({
                "date": d,
                "visits": interaction["visit_count"],
                "conversations": interaction["conversation_count"],
                "reactivity_status": day_status,
                "attention_flags": sorted(flag_counts, key=lambda item: (-flag_counts[item], item)),
            })
        result["weekly_chart"] = weekly

    except Exception as e:
        logger.warning(f"[Reports] summary 오류: {e}")

    return result


@app.get("/api/reports/daily")
async def get_daily_report(device_id: str = "", date: str = "", user: dict = Depends(require_auth)):
    """일간 상세 리포트를 반환합니다."""
    did = device_id or config.DEVICE_ID
    target_date = date or datetime.date.today().isoformat()

    result = {
        "date": target_date,
        "mood_score": 0,
        "mood_score_camera": 0,
        "mood_score_llm": 0,
        "total_detection_seconds": 0,
        "total_smiles": 0,
        "session_count": 0,
        "visit_count": 0,
        "conversation_count": 0,
        "conversation_turn_count": 0,
        "emotion_counts": {},
        "dominant_emotion": "neutral",
        "hourly_detection": {},
        "hourly_emotions": {},
        "avg_smile_ratio": 0,
        "reactivity_status": "insufficient_data",
        "reactivity_change": "insufficient_data",
        "interaction_summary": {},
        "attention_flags": [],
        "signal_breakdown": {},
        "baseline_comparison": {},
        "signal_summary": {"text": "오늘은 표정과 대화 데이터가 충분하지 않아 해석을 보류했습니다.", "flags": []},
    }

    try:
        import firebase_admin as fa
        if not fa._apps:
            return result
        from firebase_admin import firestore as fs
        db = fs.client()

        sessions = _get_sessions_for_date(db, did, target_date)
        hourly: dict = {}
        hourly_em: dict = {}

        for s in sessions:
            created = s.get("created_at", "")
            if created:
                try:
                    hour = str(datetime.datetime.fromisoformat(created).hour)
                    hourly[hour] = hourly.get(hour, 0) + 1
                    dom = s.get("dominant_emotion", "neutral")
                    if hour not in hourly_em:
                        hourly_em[hour] = {}
                    hourly_em[hour][dom] = hourly_em[hour].get(dom, 0) + 1
                except (ValueError, TypeError):
                    pass

        baseline_sessions = []
        for i in range(1, 8):
            baseline_date = (datetime.date.fromisoformat(target_date) - datetime.timedelta(days=i)).isoformat()
            baseline_sessions.extend(_get_sessions_for_date(db, did, baseline_date))

        interaction = _summarize_interaction(sessions)
        breakdown = _build_signal_breakdown(sessions)
        reactivity_status = _dominant_reactivity_status(sessions)
        attention_counts = _aggregate_attention_flags(sessions)
        baseline = _baseline_comparison(sessions, baseline_sessions)

        result["session_count"] = len(sessions)
        result["visit_count"] = interaction["visit_count"]
        result["conversation_count"] = interaction["conversation_count"]
        result["conversation_turn_count"] = interaction["conversation_turn_count"]
        result["mood_score"] = 0
        result["mood_score_camera"] = 0
        result["mood_score_llm"] = 0
        result["total_smiles"] = sum(s.get("smile_frame_count", 0) for s in sessions)
        result["total_detection_seconds"] = interaction["total_detection_seconds"]
        result["avg_smile_ratio"] = breakdown["avg_smile_ratio"]
        result["emotion_counts"] = breakdown["emotion_counts"]
        result["dominant_emotion"] = breakdown["dominant_emotion"]
        result["hourly_detection"] = hourly
        result["hourly_emotions"] = hourly_em
        result["reactivity_status"] = reactivity_status
        result["reactivity_change"] = _reactivity_change(
            reactivity_status, baseline.get("baseline_reactivity_status", "insufficient_data")
        )
        result["interaction_summary"] = {
            **interaction,
            "reactivity_label": _status_label(reactivity_status),
        }
        result["attention_flags"] = sorted(attention_counts, key=lambda item: (-attention_counts[item], item))
        result["signal_breakdown"] = breakdown
        result["baseline_comparison"] = baseline
        result["signal_summary"] = {
            "text": _latest_signal_text(sessions, reactivity_status),
            "flags": result["attention_flags"],
        }

    except Exception as e:
        logger.warning(f"[Reports] daily 오류: {e}")

    return result


@app.get("/api/reports/weekly")
async def get_weekly_report(device_id: str = "", user: dict = Depends(require_auth)):
    """주간 요약 리포트를 반환합니다."""
    did = device_id or config.DEVICE_ID

    result = {
        "avg_mood_score": 0,
        "avg_detection_seconds": 0,
        "avg_smiles": 0,
        "mood_trend": "stable",
        "daily_breakdown": [],
        "reactivity_change": "insufficient_data",
        "attention_flags": [],
        "interaction_summary": {},
        "signal_breakdown": {},
        "baseline_comparison": {},
        "signal_summary": {"text": "지난 7일간 데이터가 충분하지 않습니다.", "flags": []},
    }

    try:
        import firebase_admin as fa
        if not fa._apps:
            return result
        from firebase_admin import firestore as fs
        db = fs.client()

        breakdown = []
        total_duration = 0
        all_sessions = []

        for i in range(6, -1, -1):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            sessions = _get_sessions_for_date(db, did, d)
            all_sessions.extend(sessions)
            day_duration = sum(s.get("duration_seconds", 0) for s in sessions)
            total_duration += day_duration
            day_smiles = sum(s.get("smile_frame_count", 0) for s in sessions)
            interaction = _summarize_interaction(sessions)
            attention_counts = _aggregate_attention_flags(sessions)
            day_status = _dominant_reactivity_status(sessions)

            breakdown.append({
                "date": d,
                "session_count": len(sessions),
                "visit_count": interaction["visit_count"],
                "conversation_count": interaction["conversation_count"],
                "conversation_turn_count": interaction["conversation_turn_count"],
                "mood_score": 0,
                "total_smiles": day_smiles,
                "total_detection_seconds": day_duration,
                "reactivity_status": day_status,
                "attention_flags": sorted(attention_counts, key=lambda item: (-attention_counts[item], item)),
            })

        result["daily_breakdown"] = breakdown
        result["avg_mood_score"] = 0
        result["avg_smiles"] = round(sum(b["total_smiles"] for b in breakdown) / 7, 1)
        result["avg_detection_seconds"] = round(total_duration / 7)
        recent_sessions = []
        for i in range(7, 14):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            recent_sessions.extend(_get_sessions_for_date(db, did, d))
        current_status = _dominant_reactivity_status(all_sessions)
        baseline_status = _dominant_reactivity_status(recent_sessions)
        weekly_attention_counts = _aggregate_attention_flags(all_sessions)
        result["mood_trend"] = "stable"
        result["reactivity_change"] = _reactivity_change(current_status, baseline_status)
        result["attention_flags"] = sorted(
            weekly_attention_counts,
            key=lambda item: (-weekly_attention_counts[item], item),
        )
        result["interaction_summary"] = {
            **_summarize_interaction(all_sessions),
            "reactivity_label": _status_label(current_status),
        }
        result["signal_breakdown"] = _build_signal_breakdown(all_sessions)
        result["baseline_comparison"] = _baseline_comparison(all_sessions, recent_sessions)
        result["signal_summary"] = {
            "text": _latest_signal_text(all_sessions, current_status),
            "flags": result["attention_flags"],
        }

    except Exception as e:
        logger.warning(f"[Reports] weekly 오류: {e}")

    return result


# === 보호자 대시보드 API ===

@app.get("/api/sessions")
async def get_sessions(limit: int = 20, user: dict = Depends(require_auth)):
    """최근 대화 세션 목록을 반환합니다."""
    try:
        import firebase_admin as fa
        if not fa._apps:
            return {"sessions": [], "error": "Firebase 미연결"}
        from firebase_admin import firestore as fs
        db = fs.client()
        docs = (
            db.collection("sessions")
            .where("device_id", "==", config.DEVICE_ID)
            .order_by("created_at", direction=fs.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        sessions = []
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            sessions.append(data)
        return {"sessions": sessions}
    except Exception as e:
        return {"sessions": [], "error": str(e)}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    """보호자용 대시보드 HTML 페이지"""
    html_path = Path(__file__).parent / "dashboard.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>dashboard.html 파일이 없습니다.</h1>"


async def _mjpeg_generator():
    while True:
        frame = detector.latest_frame
        if frame:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        await asyncio.sleep(0.05)


@app.get("/video")
async def video_stream():
    return StreamingResponse(
        _mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )
