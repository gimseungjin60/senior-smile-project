import os
from pathlib import Path
from dotenv import load_dotenv

# .env 파일 로드
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# ==========================================
# 기본 설정
# ==========================================
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))
FRAME_INTERVAL = 0.1          # ~10 FPS
CORS_ORIGINS = ["http://localhost:5173"]

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
