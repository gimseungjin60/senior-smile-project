FROM python:3.11-slim

# OpenCV, PyAudio 등 시스템 의존성
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    portaudio19-dev \
    libportaudio2 \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements 먼저 복사 (레이어 캐시 활용)
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

# 소스 전체 복사
COPY . .

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
