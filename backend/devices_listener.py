"""
devices/{deviceId} 문서 리스너
- pairedUids 배열이 비면 PairingManager.unpair() 호출 + 콜백 (WS broadcast)
- 자기 자신의 heartbeat 변경으로 매분 발화하므로 frozenset 캐시로 변경 시에만 처리
"""

import logging
from typing import Callable, Optional

from firebase_admin import firestore

import config

logger = logging.getLogger(__name__)


class DevicesListener:
    def __init__(
        self,
        pairing_manager,
        on_unpaired: Optional[Callable[[str], None]] = None,
        on_paired: Optional[Callable[[str], None]] = None,
        on_camera_requested: Optional[Callable[[bool], None]] = None,
    ):
        self.pairing_manager = pairing_manager
        self.on_unpaired = on_unpaired
        self.on_paired = on_paired
        self.on_camera_requested = on_camera_requested
        self._unsubscribe = None
        self._last_uids: Optional[frozenset] = None
        self._last_camera_requested: Optional[bool] = None

        try:
            self.db = firestore.client()
        except Exception as e:
            logger.warning(f"[DevicesListener] Firestore 연결 실패: {e}")
            self.db = None

    def start(self):
        if not self.db:
            logger.warning("[DevicesListener] DB 없음 — 리스너 미시작")
            return

        doc_ref = self.db.collection("devices").document(config.DEVICE_ID)

        def _on_snapshot(doc_snapshot, changes, read_time):
            for snap in doc_snapshot:
                if not snap.exists:
                    continue
                data = snap.to_dict() or {}
                uids = frozenset(data.get("pairedUids", []) or [])

                # pairedUids 변화 처리 (변화 시에만)
                if uids != self._last_uids:
                    self._last_uids = uids
                    if len(uids) == 0 and self.pairing_manager.is_paired:
                        logger.info("[DevicesListener] pairedUids 빔 → 페어링 해제 처리")
                        self.pairing_manager.unpair()
                        if self.on_unpaired:
                            try:
                                self.on_unpaired(config.DEVICE_ID)
                            except Exception as e:
                                logger.error(f"[DevicesListener] on_unpaired 콜백 오류: {e}")
                    elif len(uids) >= 1 and not self.pairing_manager.is_paired:
                        # 외부에서 페어링이 새로 들어온 경우(예: 다른 보호자 추가)
                        self.pairing_manager.is_paired = True
                        # PIN 도 정리 (페어링 됐으니 더 이상 필요 없음)
                        self.pairing_manager.pairing_code = None
                        logger.info(f"[DevicesListener] pairedUids={len(uids)} → is_paired=True 동기화")
                        if self.on_paired:
                            try:
                                self.on_paired(config.DEVICE_ID)
                            except Exception as e:
                                logger.error(f"[DevicesListener] on_paired 콜백 오류: {e}")

                # 카메라 요청 시그널 (uids 변화 여부와 무관하게 항상 체크)
                camera_requested = bool(data.get("cameraRequested", False))
                if camera_requested != self._last_camera_requested:
                    self._last_camera_requested = camera_requested
                    logger.info(f"[DevicesListener] cameraRequested={camera_requested}")
                    if self.on_camera_requested:
                        try:
                            self.on_camera_requested(camera_requested)
                        except Exception as e:
                            logger.error(f"[DevicesListener] on_camera_requested 콜백 오류: {e}")

        self._unsubscribe = doc_ref.on_snapshot(_on_snapshot)
        logger.info(f"[DevicesListener] devices/{config.DEVICE_ID} 리스너 시작")

    def stop(self):
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        logger.info("[DevicesListener] 리스너 중지")
