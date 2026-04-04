import asyncio
import time
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 설정 상수
CAMERA_INDEX = 0
FRAME_INTERVAL = 0.1          # ~10 FPS
CORS_ORIGINS = ["http://localhost:5173"]

# DNN 감지 설정
DNN_CONFIDENCE = 0.55         # 이 값 이상만 얼굴로 인정
DETECTION_CONFIRM_FRAMES = 3  # N프레임 연속 감지 후 세션 시작

# 상태 전환 타이밍
GREETING_DURATION = 5.0       # GREETING → ACTIVE 자동 전환 (초)
ACTIVE_IDLE_TIMEOUT = 30.0    # ACTIVE에서 얼굴 미감지 후 IDLE 복귀 (초)

# 모델 경로
MODELS_DIR = Path(__file__).parent / "models"
PROTOTXT_PATH = str(MODELS_DIR / "deploy.prototxt")
MODEL_PATH = str(MODELS_DIR / "res10_300x300_ssd_iter_140000.caffemodel")


class FaceDetector:
    """
    상태 머신: idle → greeting → active → idle

    - idle:     아무도 없음. 디지털 액자 표시.
    - greeting: 얼굴 감지됨. "안녕하세요!" 5초 표시 후 자동 active 전환.
    - active:   콘텐츠 표시. 얼굴 미감지 30초 경과 시 idle 복귀.
                active 중 얼굴 다시 보여도 greeting으로 돌아가지 않음 (세션 유지).
    """

    def __init__(self):
        self.net = cv2.dnn.readNetFromCaffe(PROTOTXT_PATH, MODEL_PATH)
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

    async def broadcast(self, status: str):
        messages = {
            "idle":     "대기 모드",
            "greeting": "어르신 감지! 인사 모드",
            "active":   "콘텐츠 모드",
        }
        payload = {
            "status": status,
            "message": messages.get(status, ""),
            "detected": status in ("greeting", "active"),
        }
        disconnected = set()
        for client in self.clients:
            try:
                await client.send_json(payload)
            except Exception:
                disconnected.add(client)
        self.clients -= disconnected
        logger.info(f"상태 전환 → {status}")

    def _detect_and_encode(self, frame):
        """DNN 얼굴 감지 + 박스 그리기 + JPEG 인코딩"""
        h, w = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(frame, 1.0, (300, 300), (104, 177, 123))
        self.net.setInput(blob)
        detections = self.net.forward()

        face_found = False
        for i in range(detections.shape[2]):
            confidence = detections[0, 0, i, 2]
            if confidence > DNN_CONFIDENCE:
                face_found = True
                box = detections[0, 0, i, 3:7] * [w, h, w, h]
                x1, y1, x2, y2 = box.astype(int)
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 0), 2)
                label = f"{confidence:.0%}"
                cv2.putText(frame, label, (x1, y1 - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 0), 2)

        _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return face_found, jpeg.tobytes()

    async def _transition(self, new_status: str):
        self.current_status = new_status
        await self.broadcast(new_status)

    async def run_detection_loop(self):
        self.camera = cv2.VideoCapture(CAMERA_INDEX)
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
                await asyncio.sleep(FRAME_INTERVAL)
        finally:
            if self.camera:
                self.camera.release()
                logger.info("카메라 해제")

    async def _update_state(self, face_found: bool, now: float):
        status = self.current_status

        if status == "idle":
            if face_found:
                self._detect_streak += 1
                if self._detect_streak >= DETECTION_CONFIRM_FRAMES:
                    self._detect_streak = 0
                    self._greeting_start = now
                    await self._transition("greeting")
            else:
                self._detect_streak = 0

        elif status == "greeting":
            # 감지 여부 무관하게 5초 후 active 전환
            if self._greeting_start and (now - self._greeting_start) >= GREETING_DURATION:
                self._last_seen = now
                await self._transition("active")

        elif status == "active":
            if face_found:
                self._last_seen = now  # 타이머 리셋
            else:
                if self._last_seen and (now - self._last_seen) >= ACTIVE_IDLE_TIMEOUT:
                    self._detect_streak = 0
                    await self._transition("idle")

    def stop(self):
        self.running = False


detector = FaceDetector()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(detector.run_detection_loop())
    yield
    detector.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
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
