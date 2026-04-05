import asyncio
import time
import logging
from contextlib import asynccontextmanager
from pathlib import Path
import os
import glob
from typing import Optional
from PIL import Image, ImageDraw, ImageFont
import numpy as np

import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import datetime
import config
from voice_agent import VoiceAgent
from apscheduler.schedulers.background import BackgroundScheduler


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
        
        # 음성 대화 에이전트 인스턴스
        self.voice_agent: Optional[VoiceAgent] = None
        
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
        # color: RGB tuple
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
        if not self.voice_agent or not self.clients:
            return

        subtitle = self.voice_agent.current_subtitle or ""
        is_listening = self.voice_agent.is_listening
        is_pill_taken = self.voice_agent.is_pill_taken

        # 이전 상태와 동일하면 전송하지 않음
        voice_state = (subtitle, is_listening, is_pill_taken)
        if voice_state == getattr(self, '_last_voice_state', None):
            return
        self._last_voice_state = voice_state

        payload = {
            "type": "voice",
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

    def _detect_and_encode(self, raw_frame):
        """DNN 얼굴 감지 + 1280x720 UI 렌더링 + JPEG 인코딩"""
        target_w, target_h = 1280, 720
        frame = self._resize_and_crop(raw_frame, target_w, target_h)
        
        # --- Face Detection ---
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
                
                # 얼굴 박스 그리기는 실제 카메라 화면이 보일 때만 렌더링
                if self.current_status in ("greeting", "active"):
                    box = detections[0, 0, i, 3:7] * [raw_w, raw_h, raw_w, raw_h]
                    bx1, by1, bx2, by2 = box.astype(int)
                    
                    x1 = int(bx1 * scale) - x_offset
                    y1 = int(by1 * scale) - y_offset
                    x2 = int(bx2 * scale) - x_offset
                    y2 = int(by2 * scale) - y_offset
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 0), 2)

        # --- Slideshow Logic (idle) ---
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

        # --- UI Overlays (greeting or active) ---
        if self.current_status in ("greeting", "active"):
            # Listening Indicator
            if getattr(self.voice_agent, 'is_listening', False):
                radius = int(25 + 10 * np.sin(now * 8))
                cv2.circle(target_frame, (target_w - 60, 60), radius, (0, 0, 255), -1)
                cv2.circle(target_frame, (target_w - 60, 60), radius + 5, (255, 255, 255), 2)
            
            # Subtitle Bar (Bottom 20%)
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
                        color=(255, 255, 0) # RGB Yellow
                    )
            else:
                self.last_subtitle = ""

            # Pill Box
            if self.voice_agent and getattr(self.voice_agent, 'is_pill_taken', False):
                pill_text = "[ PILL DONE ]"
                bg_color = (0, 255, 0) # Green (RGB)
                alpha = 0.8
            else:
                pill_text = "[ CHECK PILL ]"
                bg_color = (255, 0, 0) # Red (RGB)
                alpha = (np.sin(now * 6) + 1) / 2.0 * 0.7 + 0.1 # 0.1 ~ 0.8 사이 빠른 깜빡임
                
            overlay = target_frame.copy()
            cv2.rectangle(overlay, (target_w - 360, target_h - 100), (target_w - 20, target_h - 20), bg_color[::-1], -1)
            cv2.addWeighted(overlay, alpha, target_frame, 1.0 - alpha, 0, target_frame)
            target_frame = self._draw_pillow_text(
                target_frame, pill_text, (target_w - 340, target_h - 80), size=40, color=(255, 255, 255)
            )

        # --- State Fade Logic ---
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

    async def _transition(self, new_status: str):
        self.current_status = new_status
        await self.broadcast(new_status)
        
        # [Bridge Code] 사람 감지 시 대화 에이전트 시작
        if new_status == "greeting":
            # 한 번도 안 만들어졌다면 생성
            if self.voice_agent is None:
                self.voice_agent = VoiceAgent()
            self.voice_agent.start_conversation()
        elif new_status == "idle":
            # 대기 모드로 돌아가면 대화 종료
            if self.voice_agent is not None:
                self.voice_agent.stop_conversation()

    async def run_detection_loop(self):
        self.camera = cv2.VideoCapture(config.CAMERA_INDEX)
        if not self.camera.isOpened():
            logger.error(
                "카메라를 열 수 없습니다.\n"
                "시스템 환경설정 > 개인 정보 보호 및 보안 > 카메라 권한을 확인하세요."
            )
            return

        logger.info("카메라 열림. 얼굴 감지 시작...")
        self.running = True
        loop = asyncio.get_event_loop()

        try:
            while self.running:
                ret, frame = await loop.run_in_executor(None, self.camera.read)
                if not ret:
                    await asyncio.sleep(0.5)
                    continue

                face_found, jpeg = await loop.run_in_executor(
                    None, self._detect_and_encode, frame
                )
                self.latest_frame = jpeg

                now = time.time()
                await self._update_state(face_found, now)
                await self.broadcast_voice_state()
                await asyncio.sleep(config.FRAME_INTERVAL)
        finally:
            if self.camera:
                self.camera.release()
                logger.info("카메라 해제")

    async def _update_state(self, face_found: bool, now: float):
        status = self.current_status

        if status == "idle":
            if face_found:
                self._detect_streak += 1
                if self._detect_streak >= config.DETECTION_CONFIRM_FRAMES:
                    self._detect_streak = 0
                    self._greeting_start = now
                    await self._transition("greeting")
            else:
                self._detect_streak = 0

        elif status == "greeting":
            # 감지 여부 무관하게 5초 후 active 전환
            if self._greeting_start and (now - self._greeting_start) >= config.GREETING_DURATION:
                self._last_seen = now
                await self._transition("active")

        elif status == "active":
            if face_found:
                self._last_seen = now  # 타이머 리셋
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
    if detector.voice_agent is None:
        detector.voice_agent = VoiceAgent()
        
    if not detector.voice_agent.is_running and detector.current_status == "idle":
        detector.voice_agent.trigger_pill_reminder()
    else:
        logger.info("현재 카메라/에이전트 동작 중. 알람 1분 연기")
        run_date = datetime.datetime.now() + datetime.timedelta(minutes=1)
        scheduler.add_job(scheduled_pill_reminder, 'date', run_date=run_date)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 스케줄러 등록 및 시작
    hr, mn = map(int, config.PILL_TIME.split(":"))
    scheduler.add_job(scheduled_pill_reminder, 'cron', hour=hr, minute=mn)
    scheduler.start()
    logger.info(f"스케줄러 시작 완료 (알람 시간: {config.PILL_TIME})")

    task = asyncio.create_task(detector.run_detection_loop())
    yield
    detector.stop()
    task.cancel()
    scheduler.shutdown()
    try:
        await task
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
    await websocket.send_json({
        "status": detector.current_status,
        "message": messages.get(detector.current_status, ""),
        "detected": detector.current_status in ("greeting", "active"),
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
