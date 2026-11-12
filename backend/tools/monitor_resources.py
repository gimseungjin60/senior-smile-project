"""
시연 안정성 점검 — voice_agent + vision_engine 동시 동작 시 라즈베리5 자원 사용량 모니터링.

사용법:
    cd backend
    python tools/monitor_resources.py            # 기본 60초 모니터링
    python tools/monitor_resources.py --duration 120 --interval 1.5

출력:
    [00:05] CPU 23.4% / MEM 412 MB / 코어별: [25, 18, 30, 22] / 프레임 17.3fps (vision)
    [00:10] CPU 67.1% / MEM 425 MB / 코어별: [85, 60, 72, 51] / 프레임 12.1fps (vision) ← 게임 중

장애 판정 기준 (시연 안전 가이드):
    - CPU 평균 > 85% 5초 이상: 음성 인식 지연/끊김 위험
    - 단일 코어 100% 지속: 비전 엔진 lock 경합 의심
    - 메모리 > 1.2GB: MediaPipe 모델 누수 의심
    - vision fps < 8: UX 저하, model_complexity 또는 캡처 간격 조정 필요
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

try:
    import psutil
except ImportError:
    print("psutil 미설치 — pip install psutil 후 실행해주세요.")
    sys.exit(1)


HEALTH_URL = os.environ.get("HEALTH_URL", "http://localhost:8000/health")


def fetch_vision_fps_estimate(probe_seconds: float = 1.0) -> float | None:
    """간이 fps 추정: 백엔드 /health 엔드포인트가 vision 통계를 노출하지 않으므로
    단순 응답 시간으로 백엔드 reactivity만 확인. 실제 fps는 별도 metric 엔드포인트 필요.
    여기서는 None 반환 — 추후 metric 엔드포인트가 생기면 채울 수 있음."""
    try:
        start = time.time()
        with urllib.request.urlopen(HEALTH_URL, timeout=2.0) as resp:
            resp.read()
        elapsed = time.time() - start
        return round(1.0 / elapsed, 1) if elapsed > 0 else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=60, help="모니터링 시간 (초)")
    parser.add_argument("--interval", type=float, default=1.0, help="샘플 간격 (초)")
    parser.add_argument("--json", action="store_true", help="JSON 라인 출력")
    args = parser.parse_args()

    print(f"\n[모니터] 시작 — 총 {args.duration}초, 간격 {args.interval}초")
    print(f"[모니터] HEALTH_URL = {HEALTH_URL}")
    print("─" * 80)

    samples = []
    start = time.time()
    cpu_warn_streak = 0

    psutil.cpu_percent(interval=None)  # 워밍업

    while time.time() - start < args.duration:
        time.sleep(args.interval)
        cpu_total = psutil.cpu_percent(interval=None)
        cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
        mem = psutil.virtual_memory()
        mem_used_mb = round((mem.total - mem.available) / 1024 / 1024)
        elapsed = round(time.time() - start)
        backend_resp = fetch_vision_fps_estimate()

        sample = {
            "t": elapsed,
            "cpu": cpu_total,
            "cpu_cores": cpu_per_core,
            "mem_mb": mem_used_mb,
            "backend_resp_per_sec": backend_resp,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        samples.append(sample)

        # 경고 판정
        flag = ""
        if cpu_total > 85:
            cpu_warn_streak += 1
            if cpu_warn_streak >= 5:
                flag += " ⚠️ CPU 5초 이상 85%↑"
        else:
            cpu_warn_streak = 0
        if any(c > 99 for c in cpu_per_core):
            flag += " ⚠️ 단일 코어 100% (lock 경합?)"
        if mem_used_mb > 1200:
            flag += " ⚠️ 메모리 1.2GB↑"

        if args.json:
            print(json.dumps(sample, ensure_ascii=False))
        else:
            cores_str = "[" + ", ".join(f"{c:>4.1f}" for c in cpu_per_core) + "]"
            mm = f"{elapsed:02d}:{elapsed % 60:02d}"
            resp_str = f"{backend_resp:.1f}/s" if backend_resp else "  N/A"
            print(f"[{mm}] CPU {cpu_total:>5.1f}% / MEM {mem_used_mb:>5d}MB / 코어 {cores_str} / 백엔드 {resp_str}{flag}")

    # 요약
    print("─" * 80)
    avg_cpu = sum(s["cpu"] for s in samples) / max(1, len(samples))
    max_cpu = max(s["cpu"] for s in samples)
    avg_mem = sum(s["mem_mb"] for s in samples) / max(1, len(samples))
    max_mem = max(s["mem_mb"] for s in samples)
    print(f"[요약] CPU 평균 {avg_cpu:.1f}% / 최대 {max_cpu:.1f}%")
    print(f"[요약] MEM 평균 {avg_mem:.0f}MB / 최대 {max_mem}MB")

    # 시연 안전 판정
    if max_cpu > 95:
        print("[판정] ⚠️ 시연 위험 — 피크 CPU 95%↑. 인지 게임 캡처 간격을 350~400ms로 늘려보세요.")
    elif avg_cpu > 75:
        print("[판정] ⚠️ 시연 주의 — 평균 CPU 75%↑. 동시 동작 자원 여유 부족.")
    else:
        print("[판정] ✅ 시연 가능 수준")


if __name__ == "__main__":
    main()
