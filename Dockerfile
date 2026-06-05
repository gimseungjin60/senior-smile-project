# 시니어 백엔드 (FastAPI) — Render Docker 배포용
# 빌드 컨텍스트 = 저장소 루트. Render: Runtime=Docker, Dockerfile Path=./Dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# opencv-python-headless 런타임 의존(libgthread → libglib2.0-0). headless 라 libGL 불필요.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 설치(레이어 캐시)
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

# 앱 소스 (models/photos/sounds 포함). 시크릿(.env, serviceAccountKey.json 등)은
# .dockerignore 로 제외 — 런타임 env 로 주입.
COPY backend/ ./

EXPOSE 8000

# Render 가 $PORT 를 주입한다(직접 설정 금지). serviceAccountKey.json 은 repo 에 없으므로,
# SERVICE_ACCOUNT_KEY_JSON env(서비스계정 JSON 전체)가 주어지면 기동 시점에 파일로 복원한다.
# (대안: Render Secret Files 로 /app/serviceAccountKey.json 직접 마운트)
CMD if [ -n "$SERVICE_ACCOUNT_KEY_JSON" ] && [ ! -f serviceAccountKey.json ]; then \
        printf '%s' "$SERVICE_ACCOUNT_KEY_JSON" > serviceAccountKey.json; \
    fi; \
    exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
