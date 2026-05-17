"""
LiveKit publisher (on-demand)
- 보호자 앱이 카메라 토글을 켜면 (devices/{deviceId}.cameraRequested=true) → enable()
- 토글 끄면 → disable() → room.disconnect() → empty_timeout 후 LiveKit session inactive
- 백엔드 lifespan 동안 publisher 인스턴스는 살아있지만, 실제 LiveKit 연결은 시청 요청 있을 때만.
"""

import asyncio
import logging
import os
from typing import Optional

import cv2
import numpy as np

import config

logger = logging.getLogger(__name__)


class LiveKitPublisher:
    def __init__(self, detector):
        self.detector = detector
        self.url = os.environ.get("LIVEKIT_URL", "")
        self.api_key = os.environ.get("LIVEKIT_API_KEY", "")
        self.api_secret = os.environ.get("LIVEKIT_API_SECRET", "")
        self.room = None
        self.source = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._api = None
        self._rtc = None
        self._enable_lock = asyncio.Lock()

    async def start(self):
        """백엔드 시작 시 호출 — SDK/환경 검증만, 실제 connect 안 함"""
        if not (self.url and self.api_key and self.api_secret):
            logger.warning("[LiveKit] 환경변수 미설정 (LIVEKIT_URL/KEY/SECRET) — publisher 비활성")
            return
        try:
            from livekit import api, rtc
            self._api = api
            self._rtc = rtc
            logger.info("[LiveKit] publisher 준비 완료 (on-demand 모드, viewer 요청 시 connect)")
        except ImportError:
            logger.warning("[LiveKit] livekit/livekit-api 미설치 — publisher 비활성")

    async def enable(self):
        """보호자가 카메라 요청 → room connect + publish"""
        async with self._enable_lock:
            if self.room is not None:
                return  # 이미 연결됨
            if self._api is None or self._rtc is None:
                return  # SDK/환경 미준비

            try:
                room_name = f"device-{config.DEVICE_ID}"
                token = (
                    self._api.AccessToken(self.api_key, self.api_secret)
                    .with_identity("senior")
                    .with_name("senior")
                    .with_grants(self._api.VideoGrants(room_join=True, room=room_name, can_publish=True))
                    .to_jwt()
                )

                self.room = self._rtc.Room()
                await self.room.connect(self.url, token)
                logger.info(f"[LiveKit] enable: room 연결 완료 ({room_name})")

                self.source = self._rtc.VideoSource(640, 480)
                track = self._rtc.LocalVideoTrack.create_video_track("camera", self.source)
                options = self._rtc.TrackPublishOptions(source=self._rtc.TrackSource.SOURCE_CAMERA)
                await self.room.local_participant.publish_track(track, options)
                logger.info(f"[LiveKit] enable: track publish 완료")

                self._running = True
                self._task = asyncio.create_task(self._frame_loop(self._rtc))
            except Exception as e:
                logger.warning(f"[LiveKit] enable 실패: {e}")
                # 실패 시 부분 상태 정리
                if self.room:
                    try:
                        await self.room.disconnect()
                    except Exception:
                        pass
                self.room = None
                self.source = None

    async def disable(self):
        """보호자가 카메라 꺼짐 → room disconnect → empty_timeout 후 session inactive"""
        async with self._enable_lock:
            if self.room is None:
                return

            self._running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
                self._task = None

            try:
                await self.room.disconnect()
            except Exception:
                pass
            self.room = None
            self.source = None
            logger.info("[LiveKit] disable: room 연결 종료")

    async def _frame_loop(self, rtc):
        """detector.latest_frame(JPEG) 을 디코드 → BGRA → LiveKit 프레임으로 push (20 FPS)"""
        while self._running:
            jpeg = self.detector.latest_frame
            if jpeg:
                try:
                    arr = np.frombuffer(jpeg, dtype=np.uint8)
                    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if bgr is not None:
                        if bgr.shape[1] != 640 or bgr.shape[0] != 480:
                            bgr = cv2.resize(bgr, (640, 480))
                        bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
                        frame = rtc.VideoFrame(640, 480, rtc.VideoBufferType.BGRA, bgra.tobytes())
                        self.source.capture_frame(frame)
                except Exception as e:
                    logger.debug(f"[LiveKit] frame push 실패: {e}")
            await asyncio.sleep(0.05)  # 20 FPS

    async def stop(self):
        """백엔드 종료 시 호출"""
        await self.disable()
        logger.info("[LiveKit] publisher 중지")
