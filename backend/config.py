import os
import uuid
import json
from pathlib import Path
from dotenv import load_dotenv

# .env 파일 로드
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# ==========================================
# 디바이스 ID (자동 생성, 최초 1회)
# ==========================================
_device_file = Path(__file__).parent / "device_id.json"

def _get_or_create_device_id() -> str:
    if _device_file.exists():
        data = json.loads(_device_file.read_text(encoding="utf-8"))
        return data.get("device_id", "")
    device_id = f"frame-{uuid.uuid4().hex[:8]}"
    _device_file.write_text(json.dumps({"device_id": device_id}), encoding="utf-8")
    return device_id

DEVICE_ID = _get_or_create_device_id()

# ==========================================
# 기본 설정
# ==========================================
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))
FRAME_INTERVAL = 0.1          # ~10 FPS
CORS_ORIGINS = ["*"]

# ==========================================
# 비전 (DNN 감지) 설정
# ==========================================
DNN_CONFIDENCE = 0.55         # 이 값 이상만 얼굴로 인정
DETECTION_CONFIRM_FRAMES = 3  # N프레임 연속 감지 후 세션 시작

# 상태 전환 타이밍
GREETING_DURATION = 5.0       # GREETING → ACTIVE 자동 전환 (초)
ACTIVE_IDLE_TIMEOUT = 30.0    # ACTIVE에서 얼굴 미감지 후 IDLE 복귀 (초)

# 모델 경로
MODELS_DIR = Path(__file__).parent / "models"
PROTOTXT_PATH = str(MODELS_DIR / "deploy.prototxt")
MODEL_PATH = str(MODELS_DIR / "res10_300x300_ssd_iter_140000.caffemodel")

# ==========================================
# 대화형 모듈 (VoiceAgent) 설정
# ==========================================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
SOUNDS_DIR = Path(__file__).parent / "sounds"

# ==========================================
# 스케줄러 설정
# ==========================================
PILL_TIME = "09:00"

# ==========================================
# 날씨 설정
# ==========================================
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "")
WEATHER_CITY = os.environ.get("WEATHER_CITY", "Seoul")

# ==========================================
# 야간 모드 설정
# ==========================================
NIGHT_START = os.environ.get("NIGHT_START", "22:00")
NIGHT_END = os.environ.get("NIGHT_END", "07:00")

# ==========================================
# 음성 메시지 설정
# ==========================================
VOICE_MSG_DIR = Path(__file__).parent / "voice_messages"
VOICE_MSG_DIR.mkdir(exist_ok=True)

# ==========================================
# 일일 루틴 설정
# ==========================================
DAILY_ROUTINES = [
    {"time": "07:00", "type": "morning",  "message": "좋은 아침이에요! 오늘도 건강한 하루 보내세요."},
    {"time": "09:00", "type": "pill",     "message": ""},  # 기존 복약 스케줄러 사용
    {"time": "12:00", "type": "lunch",    "message": "점심 시간이에요! 맛있는 거 드세요."},
    {"time": "15:00", "type": "activity", "message": "잠깐 스트레칭 하시는 건 어떠세요? 몸이 가벼워져요!"},
    {"time": "18:00", "type": "dinner",   "message": "저녁 식사 시간이에요! 든든하게 드세요."},
    {"time": "21:00", "type": "night",    "message": "오늘 하루도 수고하셨어요. 편안한 밤 되세요."},
]
