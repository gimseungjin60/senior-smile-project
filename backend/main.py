import asyncio
import base64
import time
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
import os
import glob
from typing import Optional
from PIL import Image, ImageDraw, ImageFont
import numpy as np

import cv2
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import datetime
import config

# Firebase Admin 초기화 — 다른 모듈보다 먼저 실행되어야 함
import firebase_admin
from firebase_admin import credentials as fb_credentials
from pathlib import Path as _Path
_key_path = _Path(__file__).parent / "serviceAccountKey.json"
if not firebase_admin._apps and _key_path.exists():
    firebase_admin.initialize_app(fb_credentials.Certificate(str(_key_path)))

from voice_agent import VoiceAgent
from notification import NotificationManager
from pairing import PairingManager
from auth import signup, login, verify_token, get_user
from firestore_listener import FirestorePairingListener
from devices_listener import DevicesListener
from livekit_publisher import LiveKitPublisher
from apscheduler.schedulers.background import BackgroundScheduler

# AI-bum 백엔드 연동 — 시연 환경에 따라 .env에서 오버라이드 가능
AIBUM_BACKEND_URL = os.environ.get("AIBUM_BACKEND_URL", "http://localhost:8001")
DEVICE_ID = config.DEVICE_ID
HEARTBEAT_INTERVAL = 60

# 카메라 입력 소스: "usb" (라즈베리파이의 cv2.VideoCapture) 또는 "tablet" (시니어 프론트 WebSocket)
CAMERA_SOURCE = os.environ.get("CAMERA_SOURCE", "usb").lower()


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
        self.font_path = "C:/Windows/Fonts/malgun.ttf"
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
            requested_activity = getattr(self.voice_agent, 'requested_activity', None)
            if new_photo_url:
                self.voice_agent.new_photo_url = None
            # 활동은 한 번만 전송하고 비움 (중복 트리거 방지)
            if requested_activity:
                self.voice_agent.requested_activity = None

        voice_state = (subtitle, is_listening, is_pill_taken, user_text, is_emergency, is_conversation_active)
        if (voice_state == getattr(self, '_last_voice_state', None)
                and not new_photo_url and not requested_activity):
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
        if requested_activity:
            payload["activity"] = requested_activity

        disconnected = set()
        for client in self.clients:
            try:
                await client.send_json(payload)
            except Exception:
                disconnected.add(client)
        self.clients -= disconnected

    async def broadcast_reminder(self, reminder_type: str, message: str, title: str = None):
        """리마인더를 프론트엔드에 전송 (컨텐츠 영역 교체용)

        title이 주어지면 그 값을 사용하고, 아니면 reminder_type별 기본 타이틀 사용.
        """
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
            "title": title or TITLES.get(reminder_type, message),
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

    def _detect_face_only(self, raw_frame):
        """얼굴 감지만 — UI 렌더링/JPEG 인코딩 없음. tablet 모드용 (시니어 프론트가 자체 UI 그림)."""
        blob = cv2.dnn.blobFromImage(raw_frame, 1.0, (300, 300), (104, 177, 123))
        self.net.setInput(blob)
        detections = self.net.forward()
        for i in range(detections.shape[2]):
            if detections[0, 0, i, 2] > config.DNN_CONFIDENCE:
                return True
        return False

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

        for i in range(detections.shape[2]):
            confidence = detections[0, 0, i, 2]
            if confidence > config.DNN_CONFIDENCE:
                face_found = True

                # 보호자 앱 카메라 뷰에 녹색 얼굴 박스가 거슬려서 제거 (시연용)

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
            # 보호자 앱 카메라 뷰에 펄스하는 빨간 마이크 원이 거슬려서 제거 (시연용)

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

            # 보호자 앱 카메라 뷰에 깜빡이는 빨간 박스가 거슬려서 PILL 표시 제거 (시연용)

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
        """AI-bum 백엔드로 하트비트 전송 + Firestore devices/{deviceId}.lastSeen 갱신"""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"{AIBUM_BACKEND_URL}/api/heartbeat", json={
                    "device_id": DEVICE_ID,
                    "current_state": self.current_status,
                })
        except Exception:
            pass

        try:
            from firebase_admin import firestore as _fs
            db = _fs.client()
            db.collection("devices").document(DEVICE_ID).set(
                {
                    "lastSeen": _fs.SERVER_TIMESTAMP,
                    "currentStatus": self.current_status,
                },
                merge=True,
            )
        except Exception as e:
            logger.warning(f"[heartbeat] Firestore 갱신 실패: {e}")

    async def _heartbeat_loop(self):
        """60초마다 하트비트 전송"""
        while self.running:
            await self._send_heartbeat()
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    # === 상태 전환 ===

    async def _transition(self, new_status: str):
        old_status = self.current_status
        self.current_status = new_status
        await self.broadcast(new_status)

        # AI-bum 백엔드에 세션 이벤트 전송 + 푸시 알림
        if new_status == "greeting" and old_status == "idle":
            self._session_start = time.time()
            await self._send_event("session_start")
            self.notifier.notify_session_start()
        elif new_status == "idle" and old_status == "active":
            duration = 0
            emotion_report = ""
            if self._session_start:
                duration = int(time.time() - self._session_start)
                self._session_start = None
            # 음성 에이전트의 감정 리포트 가져오기
            with self._voice_lock:
                if self.voice_agent:
                    emotion_report = getattr(self.voice_agent, 'last_emotion_report', "")
            await self._send_event("session_end", duration_seconds=duration, smile_count=0)
            self.notifier.notify_session_end(duration, emotion_report)

        # 음성 에이전트 시작/종료 (스레드 안전)
        with self._voice_lock:
            if new_status == "greeting":
                if self.voice_agent is None:
                    self.voice_agent = VoiceAgent()
                    self.voice_agent.notifier = self.notifier
                self.voice_agent.start_conversation()
            elif new_status == "idle":
                if self.voice_agent is not None:
                    self.voice_agent.stop_conversation()

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

    async def run_detection_loop(self):
        """페어링된 상태일 때만 얼굴 감지.
        CAMERA_SOURCE=usb: cv2.VideoCapture 로 라즈베리파이 USB 웹캠 직접 사용
        CAMERA_SOURCE=tablet: latest_frame 은 /ws/media 핸들러가 시니어 프론트(태블릿)로부터 받아 set"""
        self.running = True
        loop = asyncio.get_event_loop()
        consecutive_failures = 0
        MAX_FAILURES = 30

        is_tablet = CAMERA_SOURCE == "tablet"
        if is_tablet:
            logger.info("[Camera] tablet 모드 — /ws/media 로 시니어 프론트에서 frame 받음")

        try:
            while self.running:
                # 미페어링 가드 — 카메라/voice agent 정리
                if not self.pairing.is_paired:
                    if not is_tablet and self.camera is not None:
                        try:
                            self.camera.release()
                        except Exception:
                            pass
                        self.camera = None
                        logger.info("[Camera] 미페어링 — 카메라 해제")
                    self.latest_frame = None
                    try:
                        with self._voice_lock:
                            if self.voice_agent and getattr(self.voice_agent, 'is_running', False):
                                self.voice_agent.stop_conversation()
                                logger.info("[Camera] 미페어링 — voice_agent 종료")
                    except Exception as e:
                        logger.warning(f"[Camera] voice_agent 종료 실패: {e}")
                    if self.current_status != "idle":
                        await self._transition("idle")
                    await asyncio.sleep(1)
                    continue

                # 페어링 됨 — 카메라 source 분기
                if is_tablet:
                    # tablet 모드: latest_frame 은 /ws/media handler 가 외부에서 set
                    if self.latest_frame is None:
                        await asyncio.sleep(0.2)
                        continue
                    arr = np.frombuffer(self.latest_frame, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is None:
                        await asyncio.sleep(0.2)
                        continue
                    face_found = await loop.run_in_executor(None, self._detect_face_only, frame)
                    # latest_frame 은 JPEG 그대로 유지 (LiveKit publisher 가 그대로 publish)
                else:
                    # USB 모드: 기존 cv2.VideoCapture 흐름
                    if self.camera is None:
                        if not self._open_camera():
                            await asyncio.sleep(5)
                            continue
                        logger.info("[Camera] 페어링 완료 — 카메라 시작")
                        consecutive_failures = 0

                    ret, frame = await loop.run_in_executor(None, self.camera.read)
                    if not ret:
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_FAILURES:
                            logger.warning(f"카메라 프레임 {MAX_FAILURES}회 연속 실패. 재연결 시도...")
                            if self.current_status != "idle":
                                await self._transition("idle")
                            try:
                                if self.camera:
                                    self.camera.release()
                            except Exception:
                                pass
                            self.camera = None
                            consecutive_failures = 0
                            await asyncio.sleep(1)
                        else:
                            await asyncio.sleep(0.5)
                        continue

                    consecutive_failures = 0

                    face_found, jpeg = await loop.run_in_executor(
                        None, self._detect_and_encode, frame
                    )
                    self.latest_frame = jpeg

                now = time.time()

                if self.voice_agent:
                    self.voice_agent.face_detected = face_found

                await self._update_state(face_found, now)
                await self.broadcast_voice_state()
                await asyncio.sleep(config.FRAME_INTERVAL)
        finally:
            if not is_tablet and self.camera:
                try:
                    self.camera.release()
                except Exception:
                    pass
                logger.info("카메라 해제")

    async def _update_state(self, face_found: bool, now: float):
        status = self.current_status

        if status == "idle":
            if face_found:
                # 페어링 전에는 인사/대화 시작하지 않음 (보호자 연결 후에만 작동)
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


def _on_pairing_completed(device_id: str, uid: str):
    """FirestorePairingListener 가 PIN claim 감지 → 시니어 프론트에 페어링 완료 broadcast.
    DevicesListener 의 _on_device_paired 와 동일한 형식 사용 (시니어 프론트가 type==='pairing' 만 처리)."""
    loop = getattr(detector, '_loop', None)
    if not loop:
        return

    async def _broadcast():
        pairing_status = detector.pairing.get_status()
        n_clients = len(detector.clients)
        logger.info(f"[_on_pairing_completed] WS broadcast → {n_clients}개 client, uid={uid}, payload={pairing_status}")
        for client in list(detector.clients):
            try:
                await client.send_json({"type": "pairing", "pairing": pairing_status})
            except Exception as e:
                logger.warning(f"[_on_pairing_completed] client broadcast 실패: {e}")
    asyncio.run_coroutine_threadsafe(_broadcast(), loop)


_pairing_listener = FirestorePairingListener(
    pairing_manager=detector.pairing,
    on_paired=_on_pairing_completed,
)


def _on_device_paired(device_id: str):
    """devices/{deviceId}.pairedUids 가 채워지면 호출 → 프론트에 페어링 완료 broadcast"""
    loop = getattr(detector, '_loop', None)
    if not loop:
        return

    async def _broadcast():
        pairing_status = detector.pairing.get_status()
        n_clients = len(detector.clients)
        logger.info(f"[_on_device_paired] WS broadcast → {n_clients}개 client, payload={pairing_status}")
        for client in list(detector.clients):
            try:
                await client.send_json({"type": "pairing", "pairing": pairing_status})
            except Exception as e:
                logger.warning(f"[_on_device_paired] client broadcast 실패: {e}")
    asyncio.run_coroutine_threadsafe(_broadcast(), loop)


def _on_device_unpaired(device_id: str):
    """devices/{deviceId}.pairedUids 가 비면 호출 → voice_agent 종료 + 프론트에 페어링 해제 broadcast"""
    # 마이크 ON 상태로 살아있는 voice agent 즉시 종료
    try:
        with detector._voice_lock:
            if detector.voice_agent and getattr(detector.voice_agent, 'is_running', False):
                detector.voice_agent.stop_conversation()
                logger.info("[unpair] voice_agent 강제 종료")
    except Exception as e:
        logger.warning(f"[unpair] voice_agent 종료 실패: {e}")

    loop = getattr(detector, '_loop', None)
    if not loop:
        logger.warning("[_on_device_unpaired] _loop 없음 — broadcast 불가")
        return

    async def _broadcast():
        pairing_status = detector.pairing.get_status()
        n_clients = len(detector.clients)
        logger.info(f"[_on_device_unpaired] WS broadcast → {n_clients}개 client, payload={pairing_status}")
        # 시니어 프론트(App.jsx)가 기대하는 키는 'pairing' (payload 아님)
        for client in list(detector.clients):
            try:
                await client.send_json({"type": "pairing", "pairing": pairing_status})
            except Exception as e:
                logger.warning(f"[_on_device_unpaired] client broadcast 실패: {e}")
    asyncio.run_coroutine_threadsafe(_broadcast(), loop)


_livekit_publisher = LiveKitPublisher(detector)


def _on_camera_requested(requested: bool):
    """보호자 앱이 카메라 토글을 켜고/끄면 LiveKit publisher enable/disable"""
    loop = getattr(detector, '_loop', None)
    if not loop:
        return
    coro = _livekit_publisher.enable() if requested else _livekit_publisher.disable()
    asyncio.run_coroutine_threadsafe(coro, loop)


_devices_listener = DevicesListener(
    pairing_manager=detector.pairing,
    on_unpaired=_on_device_unpaired,
    on_paired=_on_device_paired,
    on_camera_requested=_on_camera_requested,
)

def scheduled_pill_reminder():
    if not detector.pairing.is_paired:
        logger.info("[Reminder] 미페어링 — pill 알림 무시")
        return
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

        if not detector.voice_agent.is_running and detector.current_status == "idle":
            detector.voice_agent.trigger_pill_reminder()
            # 10분 후 복용 미확인 시 보호자에게 알림
            check_date = datetime.datetime.now() + datetime.timedelta(minutes=10)
            scheduler.add_job(
                _check_pill_missed, 'date', run_date=check_date,
                id="pill_check", replace_existing=True
            )
        else:
            logger.info("현재 카메라/에이전트 동작 중. 알람 1분 연기")
            run_date = datetime.datetime.now() + datetime.timedelta(minutes=1)
            scheduler.add_job(scheduled_pill_reminder, 'date', run_date=run_date, id="pill_retry", replace_existing=True)


def _check_pill_missed():
    """복약 알림 후 10분 경과, 미복용 시 보호자 알림"""
    if not detector.pairing.is_paired:
        return
    with detector._voice_lock:
        if detector.voice_agent and detector.voice_agent.is_pill_taken:
            logger.info("복약 확인 완료. 미복용 알림 불필요.")
            return
    logger.info("복약 미확인. 보호자에게 푸시 알림 전송.")
    detector.notifier.notify_pill_missed()


def _events_tick():
    """매 분 0초에 Firestore `events` 컬렉션을 검사하여 현재 시각과 일치하는
    오늘 일정이 있으면 갤탭 ReminderScreen에 띄운다.

    실패해도 다른 스케줄에 영향 없도록 try/except로 감싼다.
    미페어링 상태에선 알림 안 띄움."""
    if not detector.pairing.is_paired:
        return
    try:
        import firebase_admin as fa
        if not fa._apps:
            return
        from firebase_admin import firestore as fs
        db = fs.client()
        now = datetime.datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        current_hm = now.strftime("%H:%M")

        # 오늘 날짜 일정만 조회
        docs = db.collection("events").where("date", "==", today_str).stream()
        type_to_reminder = {
            "family":   ("activity", "가족 일정 알림"),
            "medical":  ("pill",     "병원 일정 알림"),
            "activity": ("activity", "활동 시간이에요!"),
            "other":    ("activity", "일정 알림"),
        }
        for doc in docs:
            ev = doc.to_dict()
            if ev.get("time") != current_hm:
                continue
            reminder_type, default_label = type_to_reminder.get(ev.get("type", "other"), type_to_reminder["other"])
            title = ev.get("title", default_label)
            message = ev.get("description", "") or default_label
            try:
                loop = getattr(detector, '_loop', None)
                if loop:
                    asyncio.run_coroutine_threadsafe(
                        detector.broadcast_reminder(reminder_type, message, title=title),
                        loop
                    )
                logger.info(f"[events_tick] 일정 알림: {title} @ {current_hm}")
            except Exception as e:
                logger.warning(f"[events_tick] 브로드캐스트 실패: {e}")
    except Exception as e:
        logger.warning(f"[events_tick] 처리 실패: {e}")


def _get_medications_from_firestore() -> list:
    """Firestore medications/{deviceId}/items에서 약 목록을 읽어 반환합니다."""
    try:
        import firebase_admin as fa
        if not fa._apps:
            return []
        from firebase_admin import firestore as fs
        db = fs.client()
        items = db.collection("medications").document(DEVICE_ID).collection("items").get()
        result = []
        for doc in items:
            data = doc.to_dict()
            data["id"] = doc.id
            result.append(data)
        return result
    except Exception as e:
        logger.warning(f"[medication] Firestore 읽기 실패: {e}")
        return []


def _medication_tick():
    if not detector.pairing.is_paired:
        return
    """매 분 0초에 실행 — Firestore medications/{deviceId}/items의 enabled 처방 시간이
    현재와 일치하면 ReminderScreen + 음성 알림 + 보호자 푸시를 발송한다.
    """
    try:
        now = datetime.datetime.now()
        current_hm = now.strftime("%H:%M")
        meds = _get_medications_from_firestore()
        for m in meds:
            if not m.get("enabled", True):
                continue
            if m.get("time") != current_hm:
                continue

            name = m.get("name", "약")
            title = f"{name} 드실 시간이에요!"
            message = (
                f"{m.get('dosage', '')} {m.get('notes', '')}".strip()
                or "잊지 말고 꼭 챙겨 드세요!"
            )

            # 프론트엔드(갤탭) ReminderScreen 표시
            try:
                loop = getattr(detector, '_loop', None)
                if loop:
                    asyncio.run_coroutine_threadsafe(
                        detector.broadcast_reminder("pill", message, title=title),
                        loop
                    )
            except Exception as e:
                logger.warning(f"[medication_tick] 프론트 브로드캐스트 실패: {e}")

            # 음성 발화 (대화 중이 아닐 때만)
            try:
                with detector._voice_lock:
                    if detector.voice_agent is None:
                        detector.voice_agent = VoiceAgent()
                        detector.voice_agent.notifier = detector.notifier
                    if not detector.voice_agent.is_running and detector.current_status == "idle":
                        detector.voice_agent.trigger_pill_reminder()
            except Exception as e:
                logger.warning(f"[medication_tick] 음성 발화 실패: {e}")

            # 10분 후 미복용 체크 예약
            try:
                check_date = now + datetime.timedelta(minutes=10)
                scheduler.add_job(
                    _check_pill_missed, 'date', run_date=check_date,
                    id=f"pill_check_{m.get('id', current_hm)}", replace_existing=True,
                )
            except Exception as e:
                logger.warning(f"[medication_tick] 미복용 체크 예약 실패: {e}")

            logger.info(f"[medication_tick] 리마인더 발송: {name} @ {current_hm}")
    except Exception as e:
        logger.warning(f"[medication_tick] 처리 실패: {e}")


def scheduled_routine(routine_type: str, message: str):
    """일일 루틴 스케줄러에 의해 호출됩니다. 미페어링 시 무시."""
    if not detector.pairing.is_paired:
        return
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    detector._loop = asyncio.get_event_loop()  # 스케줄러 스레드에서 코루틴 실행용

    # 복약 알림 스케줄러 (config.PILL_TIME — 레거시 단일 시간)
    hr, mn = map(int, config.PILL_TIME.split(":"))
    scheduler.add_job(scheduled_pill_reminder, 'cron', hour=hr, minute=mn)

    # 보호자가 등록한 medications.json 기반 리마인더 (매 분 체크)
    scheduler.add_job(_medication_tick, 'cron', second=0, id="medication_tick")

    # 보호자가 등록한 Firestore events 기반 일정 알림 (매 분 체크)
    scheduler.add_job(_events_tick, 'cron', second=0, id="events_tick")

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

    _pairing_listener.start()
    _devices_listener.start()
    await _livekit_publisher.start()

    detection_task = asyncio.create_task(detector.run_detection_loop())
    heartbeat_task = asyncio.create_task(detector._heartbeat_loop())
    yield
    await _livekit_publisher.stop()
    _devices_listener.stop()
    _pairing_listener.stop()
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


# 시니어 프론트(태블릿) ↔ 백엔드 미디어 다리.
# binary 첫 byte 로 메시지 type 구분: 0x00 video frame(JPEG), 0x01 audio out(TTS), 0x02 audio in(PCM)
MEDIA_TYPE_VIDEO = 0x00
MEDIA_TYPE_AUDIO_OUT = 0x01
MEDIA_TYPE_AUDIO_IN = 0x02

# 시니어 프론트에 audio 를 broadcast 하기 위해 매체 connection 목록 유지
_media_clients: set[WebSocket] = set()


@app.websocket("/ws/media")
async def websocket_media_endpoint(websocket: WebSocket):
    await websocket.accept()
    _media_clients.add(websocket)
    logger.info(f"[/ws/media] 시니어 프론트 연결 (총 {len(_media_clients)}개)")

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if not data or len(data) < 2:
                continue
            mtype = data[0]
            payload = data[1:]

            if mtype == MEDIA_TYPE_VIDEO:
                # 태블릿 카메라 frame → detector.latest_frame 에 set
                detector.latest_frame = bytes(payload)
            elif mtype == MEDIA_TYPE_AUDIO_IN:
                # 태블릿 마이크 PCM chunk → voice_agent 입력 (Phase 3 에서 처리)
                if detector.voice_agent and hasattr(detector.voice_agent, 'feed_audio_chunk'):
                    try:
                        detector.voice_agent.feed_audio_chunk(bytes(payload))
                    except Exception as e:
                        logger.debug(f"[/ws/media] audio chunk feed 실패: {e}")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"[/ws/media] 예외: {e}")
    finally:
        _media_clients.discard(websocket)
        logger.info(f"[/ws/media] 시니어 프론트 연결 해제 (남은 {len(_media_clients)}개)")


async def broadcast_audio_to_seniors(audio_bytes: bytes):
    """TTS 결과 audio (mp3 bytes) 를 시니어 프론트들에게 broadcast (Phase 2)."""
    if not _media_clients:
        return
    payload = bytes([MEDIA_TYPE_AUDIO_OUT]) + audio_bytes
    for client in list(_media_clients):
        try:
            await client.send_bytes(payload)
        except Exception as e:
            logger.debug(f"[broadcast_audio] client 전송 실패: {e}")
        logger.info(f"WebSocket 해제 (총 {len(detector.clients)}개)")


@app.websocket("/ws/vision")
async def ws_vision(websocket: WebSocket):
    """
    인지 게임(가위바위보 져주기) + 스트레칭 가이드용 비전 WebSocket.

    수신 메시지:
    - {"type": "config", "aiMove": "rock"|"paper"|"scissors"}  — RPS 게임 시작 시
    - {"type": "frame", "mode": "rps"|"pose", "data": "<base64 jpeg>"}

    송신 메시지:
    - RPS: {"type": "rps", "detected", "gesture", "confidence", "judgement", "aiMove"}
    - Pose: {"type": "pose", "detected", "landmarks", "angles"}
    """
    # vision_engine import는 mediapipe 미설치 환경에서 메인 앱 부팅을 막지 않도록 지연
    try:
        from core.vision_engine import VisionEngine, judge_lose_game
    except ImportError as e:
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "message": f"vision engine import 실패: {e}",
        })
        await websocket.close()
        return

    await websocket.accept()
    engine = VisionEngine()
    if not engine.is_ready():
        await websocket.send_json({
            "type": "error",
            "message": "MediaPipe 미설치 — 'pip install mediapipe' 후 재시작 필요",
        })
        await websocket.close()
        return

    # 게임/스트레칭 진입 — voice_agent 일시 정지 (마이크/스피커 자원 충돌 방지)
    voice = getattr(detector, 'voice_agent', None)
    if voice and hasattr(voice, 'pause'):
        try:
            voice.pause()
        except Exception as e:
            logger.warning(f"[GAME] voice_agent pause 실패: {e}")

    logger.info("[GAME] 비전 WebSocket 연결 — voice_agent 일시 정지")
    ai_move: Optional[str] = None

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")

            if mtype == "config":
                ai_move = msg.get("aiMove")
                await websocket.send_json({"type": "ack", "aiMove": ai_move})
                continue

            if mtype != "frame":
                continue

            data_b64 = msg.get("data", "")
            if not data_b64:
                continue

            # base64 jpeg → numpy BGR (data URL prefix 제거)
            try:
                img_bytes = base64.b64decode(data_b64.split(",")[-1])
                arr = np.frombuffer(img_bytes, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            except Exception:
                continue
            if frame is None:
                continue

            # 라즈베리5 부하 절감: 320 너비로 다운스케일 (가로/세로 비율 유지)
            h, w = frame.shape[:2]
            if w > 320:
                new_h = int(h * 320 / w)
                frame = cv2.resize(frame, (320, new_h))

            mode = msg.get("mode", "rps")
            if mode == "rps":
                # 블로킹 호출은 별도 스레드로 위임
                result = await asyncio.to_thread(engine.detect_hand, frame)
                judgement = (
                    judge_lose_game(ai_move, result.gesture) if ai_move else "unknown"
                )
                await websocket.send_json({
                    "type": "rps",
                    "detected": result.detected,
                    "gesture": result.gesture,
                    "confidence": result.confidence,
                    "judgement": judgement,
                    "aiMove": ai_move,
                })
            elif mode == "pose":
                result = await asyncio.to_thread(engine.detect_pose, frame)
                await websocket.send_json({
                    "type": "pose",
                    "detected": result.detected,
                    "landmarks": result.landmarks,
                    "angles": result.angles,
                })

    except WebSocketDisconnect:
        logger.info("[GAME] 비전 WebSocket 해제")
    except Exception as e:
        logger.error(f"[GAME] WebSocket 오류: {e}")
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        # 게임/스트레칭 종료 — voice_agent 재개
        if voice and hasattr(voice, 'resume'):
            try:
                voice.resume()
                logger.info("[GAME] 종료 — voice_agent 재개")
            except Exception as e:
                logger.warning(f"[GAME] voice_agent resume 실패: {e}")


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
        return None
    from firebase_admin import firestore as fs
    return fs.client()


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
async def api_me(token: str = ""):
    """현재 로그인된 사용자 정보"""
    payload = verify_token(token)
    if not payload:
        return {"success": False, "error": "인증이 필요합니다."}
    db = _get_db()
    user = get_user(db, payload["user_id"])
    if user:
        return {"success": True, "user": user}
    return {"success": True, "user": {"user_id": payload["user_id"], "name": payload["name"]}}


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
async def verify_pairing(payload: dict):
    """보호자 앱에서 코드를 입력하면 매칭을 수행합니다."""
    code = payload.get("code", "")
    user_id = payload.get("user_id", "")
    user_name = payload.get("user_name", "")
    fcm_token = payload.get("fcm_token", "")

    if not code or not user_id or not user_name:
        return {"success": False, "error": "code, user_id, user_name 필드가 필요합니다."}

    result = detector.pairing.verify_and_pair(code, user_id, user_name, fcm_token)

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
async def unpair_device():
    """페어링을 해제합니다."""
    detector.pairing.unpair()
    return {"success": True}


# === 알림 API ===

@app.post("/api/notifications/register")
async def register_push_token(payload: dict):
    """보호자 앱에서 FCM 디바이스 토큰을 등록합니다."""
    token = payload.get("token")
    if not token:
        return {"success": False, "error": "token 필드가 필요합니다."}
    registered = detector.notifier.register_token(token)
    return {"success": True, "registered": registered, "total": detector.notifier.get_token_count()}


@app.post("/api/notifications/unregister")
async def unregister_push_token(payload: dict):
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
        # 키 없으면 null 반환 → 시니어/모바일 둘 다 날씨 카드 자체를 숨김
        return None

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
async def list_medications():
    """복약 목록을 반환합니다."""
    return {"medications": _load_medications()}


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@app.post("/api/medications")
async def add_medication(data: dict):
    """복약 항목을 추가합니다."""
    import uuid as _uuid
    meds = _load_medications()
    med = {
        "id": f"med_{_uuid.uuid4().hex[:8]}",
        "name": data.get("name", ""),
        "time": data.get("time", "09:00"),
        "dosage": data.get("dosage", ""),
        "notes": data.get("notes", ""),
        "stock": _safe_int(data.get("stock"), 0),
        "enabled": True,
        "created_at": datetime.datetime.now().isoformat(),
    }
    meds.append(med)
    _save_medications(meds)
    return {"success": True, "medication": med}


@app.put("/api/medications/{med_id}")
async def update_medication(med_id: str, data: dict):
    """복약 항목을 수정합니다."""
    meds = _load_medications()
    for med in meds:
        if med["id"] == med_id:
            if "name" in data: med["name"] = data["name"]
            if "time" in data: med["time"] = data["time"]
            if "dosage" in data: med["dosage"] = data["dosage"]
            if "notes" in data: med["notes"] = data["notes"]
            if "enabled" in data: med["enabled"] = data["enabled"]
            if "stock" in data: med["stock"] = _safe_int(data["stock"], 0)
            _save_medications(meds)
            return {"success": True, "medication": med}
    return {"success": False, "message": "약을 찾을 수 없습니다."}


@app.delete("/api/medications/{med_id}")
async def delete_medication(med_id: str):
    """복약 항목을 삭제합니다."""
    meds = _load_medications()
    meds = [m for m in meds if m["id"] != med_id]
    _save_medications(meds)
    return {"success": True}


@app.get("/api/medications/history")
async def medication_history(limit: int = 30):
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


@app.get("/api/medications/calendar")
async def medication_calendar(
    from_: str = Query(None, alias="from"),
    to: str = None,
):
    """기간별 복약 캘린더 데이터를 반환합니다.

    쿼리 파라미터:
    - from: YYYY-MM-DD (기본값: 오늘 - 6일)
    - to:   YYYY-MM-DD (기본값: 오늘)
    """
    import datetime as _dt

    today = _dt.date.today()
    try:
        end_date = _dt.date.fromisoformat(to) if to else today
    except ValueError:
        end_date = today
    try:
        start_date = _dt.date.fromisoformat(from_) if from_ else (end_date - _dt.timedelta(days=6))
    except ValueError:
        start_date = end_date - _dt.timedelta(days=6)
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    meds = _load_medications()
    prescribed = [
        {
            "med_id": m.get("id"),
            "name": m.get("name", ""),
            "time": m.get("time", ""),
            "dosage": m.get("dosage", ""),
            "stock": _safe_int(m.get("stock"), 0),
        }
        for m in meds
        if m.get("enabled", True) and m.get("time")
    ]

    # 잔량 요약: 같은 이름의 약이 여러 시간(아침/저녁)에 처방되면 합쳐서 일일 횟수 계산
    stock_by_name = {}
    daily_count_by_name = {}
    for p in prescribed:
        name = p["name"]
        if not name:
            continue
        stock_by_name[name] = p["stock"]  # 같은 이름이면 동일 stock으로 가정 (시연용 단순화)
        daily_count_by_name[name] = daily_count_by_name.get(name, 0) + 1

    stock_alerts = []
    for name, stock in stock_by_name.items():
        per_day = daily_count_by_name.get(name, 1) or 1
        days_left = stock // per_day if per_day else stock
        if stock <= 0:
            level = "out"
        elif days_left <= 3:
            level = "critical"
        elif days_left <= 7:
            level = "warning"
        else:
            continue
        stock_alerts.append({
            "name": name,
            "stock": stock,
            "daily_count": per_day,
            "days_left": days_left,
            "level": level,
        })
    # 위급도 순 정렬
    _level_order = {"out": 0, "critical": 1, "warning": 2}
    stock_alerts.sort(key=lambda a: _level_order.get(a["level"], 9))

    # Firestore에서 medication_logs 조회 (없거나 실패하면 빈 결과)
    logs_by_date = {}
    try:
        import firebase_admin as fa
        if fa._apps:
            from firebase_admin import firestore as fs
            db = fs.client()
            start_iso = start_date.isoformat()
            end_iso = (end_date + _dt.timedelta(days=1)).isoformat()
            docs = (
                db.collection("medication_logs")
                .where("device_id", "==", config.DEVICE_ID)
                .where("date", ">=", start_iso)
                .where("date", "<", end_iso)
                .stream()
            )
            for doc in docs:
                d = doc.to_dict()
                date_key = d.get("date", "")
                logs_by_date.setdefault(date_key, []).append({
                    "med_id": d.get("med_id"),
                    "med_name": d.get("med_name"),
                    "slot": d.get("slot"),
                    "taken_at": d.get("taken_at"),
                })
    except Exception as e:
        print(f"[medication_calendar] Firestore 조회 실패: {e}")

    now = _dt.datetime.now()
    days = []
    cursor = start_date
    while cursor <= end_date:
        date_key = cursor.isoformat()
        taken = logs_by_date.get(date_key, [])
        taken_med_ids = {t["med_id"] for t in taken if t.get("med_id")}

        # 미복용 추정: 처방 시간 + 2시간 지났는데 같은 med_id 기록 없으면 missed
        missed = []
        for p in prescribed:
            try:
                h, mm = p["time"].split(":")
                p_dt = _dt.datetime.combine(cursor, _dt.time(int(h), int(mm)))
            except (ValueError, AttributeError):
                continue
            cutoff = p_dt + _dt.timedelta(hours=2)
            if now < cutoff:
                continue  # 아직 복용 시간이 지나지 않음
            if p["med_id"] not in taken_med_ids:
                missed.append({"med_id": p["med_id"], "name": p["name"], "time": p["time"]})

        days.append({
            "date": date_key,
            "prescribed": prescribed,
            "taken": taken,
            "missed": missed,
            "summary": {
                "prescribed_count": len(prescribed),
                "taken_count": len(taken),
                "missed_count": len(missed),
            },
        })
        cursor += _dt.timedelta(days=1)

    return {
        "from": start_date.isoformat(),
        "to": end_date.isoformat(),
        "days": days,
        "stock_alerts": stock_alerts,
    }


# === 음성 메시지 API ===

@app.post("/api/voice-messages/send")
async def send_voice_message(file: UploadFile = File(...), sender: str = "보호자"):
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
async def list_voice_messages(direction: str = ""):
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
async def get_voice_audio(msg_id: str):
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
async def list_photos(limit: int = 50):
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
async def upload_photo(file: UploadFile = File(...)):
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

def _parse_mood_score(emotion_report: str) -> int:
    """감정 리포트 문자열에서 기분 점수를 추출합니다."""
    import re
    match = re.search(r'\[기분\s*점수[:\s]*(\d+)', emotion_report or "")
    if match:
        return int(match.group(1))
    return 0


# 한국어 감정 키워드 → 표준 키 매핑 (theme/emotions.js의 EMOTION_META 키와 동기화)
_EMOTION_KEYWORDS = {
    "happiness": ["행복", "기쁨", "기뻐", "즐거", "신나", "웃음", "밝", "좋아", "행복해"],
    "sadness":   ["슬픔", "슬퍼", "우울", "외로", "쓸쓸", "눈물", "그리워"],
    "anger":     ["화남", "분노", "짜증", "성나", "화가"],
    "fear":      ["두려", "무서", "불안", "걱정", "초조"],
    "surprise":  ["놀람", "놀랐", "깜짝"],
    "disgust":   ["불쾌", "역겨", "싫"],
    "contempt":  ["경멸", "비웃"],
    "neutral":   ["평온", "차분", "보통", "그저그", "평범"],
}


def _classify_emotion(emotion_report: str) -> str:
    """리포트 텍스트에서 감정 키를 추출합니다.

    1) "[감정: KEY]" 형식이 있으면 그것 우선 (voice_agent가 명시적으로 분류한 경우)
    2) 한국어 키워드 매칭
    3) 둘 다 실패하면 mood_score 기반 추정
    """
    import re
    text = emotion_report or ""

    # 1) 명시적 태그
    m = re.search(r'\[감정[:\s]*([a-z]+)\]', text)
    if m:
        key = m.group(1).strip().lower()
        if key in _EMOTION_KEYWORDS:
            return key

    # 2) 한국어 키워드
    for key, words in _EMOTION_KEYWORDS.items():
        for w in words:
            if w in text:
                return key

    # 3) 점수 기반 추정
    score = _parse_mood_score(text)
    if score >= 70:
        return "happiness"
    if score >= 40:
        return "neutral"
    if score > 0:
        return "sadness"
    return "neutral"


def _aggregate_emotions(sessions: list) -> tuple:
    """세션 리스트에서 (emotion_counts dict, dominant_emotion) 반환."""
    counts = {}
    for s in sessions:
        key = _classify_emotion(s.get("emotion_report", ""))
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return {}, "neutral"
    dominant = max(counts.items(), key=lambda kv: kv[1])[0]
    return counts, dominant


def _detection_seconds_for_date(db, device_id: str, date_str: str) -> int:
    """detection_events 컬렉션의 session_end 이벤트들의 duration_seconds 합산."""
    try:
        import datetime as _dt
        start = _dt.datetime.fromisoformat(f"{date_str}T00:00:00")
        end = _dt.datetime.fromisoformat(f"{date_str}T23:59:59")
        docs = (
            db.collection("detection_events")
            .where("device_id", "==", device_id)
            .where("type", "==", "session_end")
            .where("timestamp", ">=", start)
            .where("timestamp", "<=", end)
            .stream()
        )
        total = 0
        for doc in docs:
            data = doc.to_dict()
            total += int(data.get("duration_seconds", 0) or 0)
        return total
    except Exception as e:
        logger.warning(f"[Reports] detection_seconds 집계 오류: {e}")
        return 0


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


@app.get("/api/reports/summary")
async def get_report_summary(device_id: str = ""):
    """홈 화면용 요약 데이터를 반환합니다."""
    did = device_id or config.DEVICE_ID
    today = datetime.date.today().isoformat()

    result = {
        "device": {"device_id": did, "last_detection": None},
        "today": {
            "mood_score": 0,
            "total_detection_seconds": 0,
            "total_smiles": 0,
            "session_count": 0,
            "visit_count": 0,
            "conversation_count": 0,
            "emotion_counts": {},
            "dominant_emotion": "neutral",
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

        # 오늘 세션
        today_sessions = _get_sessions_for_date(db, did, today)
        scores = []
        for s in today_sessions:
            score = _parse_mood_score(s.get("emotion_report", ""))
            if score > 0:
                scores.append(score)

        # 대화 세션: 사용자 발화가 1개 이상 있는 세션
        conversation_sessions = [s for s in today_sessions if int(s.get("message_count", 0) or 0) > 0]

        # 감정 분포
        emotion_counts, dominant = _aggregate_emotions(today_sessions)

        # 감지 시간: detection_events의 session_end 합산 (대체로 정확). 0이면 message_count*30s 추정
        det_sec = _detection_seconds_for_date(db, did, today)
        if det_sec == 0 and today_sessions:
            est = sum(int(s.get("message_count", 0) or 0) for s in today_sessions) * 30
            det_sec = est

        result["today"]["session_count"] = len(today_sessions)
        result["today"]["visit_count"] = len(today_sessions)
        result["today"]["conversation_count"] = len(conversation_sessions)
        result["today"]["mood_score"] = round(sum(scores) / len(scores)) if scores else 0
        result["today"]["total_smiles"] = sum(
            1 for s in today_sessions if _parse_mood_score(s.get("emotion_report", "")) >= 70
        )
        result["today"]["total_detection_seconds"] = det_sec
        result["today"]["emotion_counts"] = emotion_counts
        result["today"]["dominant_emotion"] = dominant

        # 주간 차트 (7일)
        weekly = []
        for i in range(6, -1, -1):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            day_sessions = _get_sessions_for_date(db, did, d) if i > 0 else today_sessions
            day_scores = [_parse_mood_score(s.get("emotion_report", "")) for s in day_sessions]
            day_scores = [s for s in day_scores if s > 0]
            _, day_dom = _aggregate_emotions(day_sessions)
            weekly.append({
                "date": d,
                "visits": len(day_sessions),
                "mood_score": round(sum(day_scores) / len(day_scores)) if day_scores else 0,
                "dominant_emotion": day_dom,
            })
        result["weekly_chart"] = weekly

    except Exception as e:
        logger.warning(f"[Reports] summary 오류: {e}")

    return result


@app.get("/api/reports/daily")
async def get_daily_report(device_id: str = "", date: str = ""):
    """일간 상세 리포트를 반환합니다."""
    did = device_id or config.DEVICE_ID
    target_date = date or datetime.date.today().isoformat()

    result = {
        "date": target_date,
        "mood_score": 0,
        "total_detection_seconds": 0,
        "total_smiles": 0,
        "session_count": 0,
        "hourly_detection": {},
    }

    try:
        import firebase_admin as fa
        if not fa._apps:
            return result
        from firebase_admin import firestore as fs
        db = fs.client()

        sessions = _get_sessions_for_date(db, did, target_date)
        scores = []
        hourly = {}

        for s in sessions:
            score = _parse_mood_score(s.get("emotion_report", ""))
            if score > 0:
                scores.append(score)
            created = s.get("created_at", "")
            if created:
                try:
                    hour = str(datetime.datetime.fromisoformat(created).hour)
                    hourly[hour] = hourly.get(hour, 0) + 1
                except (ValueError, TypeError):
                    pass

        result["session_count"] = len(sessions)
        result["mood_score"] = round(sum(scores) / len(scores)) if scores else 0
        result["total_smiles"] = sum(
            1 for s in sessions
            if _parse_mood_score(s.get("emotion_report", "")) >= 70
        )
        result["hourly_detection"] = hourly

    except Exception as e:
        logger.warning(f"[Reports] daily 오류: {e}")

    return result


@app.get("/api/reports/weekly")
async def get_weekly_report(device_id: str = ""):
    """주간 요약 리포트를 반환합니다."""
    did = device_id or config.DEVICE_ID

    result = {
        "avg_mood_score": 0,
        "avg_detection_seconds": 0,
        "avg_smiles": 0,
        "mood_trend": "stable",
        "daily_breakdown": [],
    }

    try:
        import firebase_admin as fa
        if not fa._apps:
            return result
        from firebase_admin import firestore as fs
        db = fs.client()

        all_scores = []
        breakdown = []

        for i in range(6, -1, -1):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            sessions = _get_sessions_for_date(db, did, d)
            day_scores = [_parse_mood_score(s.get("emotion_report", "")) for s in sessions]
            day_scores = [s for s in day_scores if s > 0]
            avg = round(sum(day_scores) / len(day_scores)) if day_scores else 0
            all_scores.extend(day_scores)
            breakdown.append({
                "date": d,
                "session_count": len(sessions),
                "mood_score": avg,
                "total_smiles": sum(1 for s in day_scores if s >= 70),
                "total_detection_seconds": len(sessions) * 300,  # 세션당 평균 5분 추정
            })

        result["daily_breakdown"] = breakdown
        result["avg_mood_score"] = round(sum(all_scores) / len(all_scores)) if all_scores else 0
        result["avg_smiles"] = round(sum(b["total_smiles"] for b in breakdown) / 7, 1)

        # 추세 계산 (전반 3일 vs 후반 4일)
        first_half = [s for b in breakdown[:3] for s in [b["mood_score"]] if s > 0]
        second_half = [s for b in breakdown[3:] for s in [b["mood_score"]] if s > 0]
        if first_half and second_half:
            avg_first = sum(first_half) / len(first_half)
            avg_second = sum(second_half) / len(second_half)
            if avg_second > avg_first + 5:
                result["mood_trend"] = "improving"
            elif avg_second < avg_first - 5:
                result["mood_trend"] = "declining"

    except Exception as e:
        logger.warning(f"[Reports] weekly 오류: {e}")

    return result


# === 보호자 대시보드 API ===

@app.get("/api/sessions")
async def get_sessions(limit: int = 20):
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


@app.get("/snapshot")
async def snapshot():
    """단일 JPEG 프레임 반환 — RN <Image>에서 폴링 가능 (MJPEG는 네이티브 미지원)"""
    frame = detector.latest_frame
    if not frame:
        return Response(status_code=204)
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )
