"""
Firestore pairing_requests 리스너
Cloud Function verifyPairing이 PIN을 claim하면 → PairingManager 상태 갱신 + WS 브로드캐스트
"""

import logging
import threading
from typing import Callable, Optional

from firebase_admin import firestore

import config

logger = logging.getLogger(__name__)


class FirestorePairingListener:
    def __init__(
        self,
        pairing_manager,
        on_paired: Optional[Callable[[str, str], None]] = None,
    ):
        """
        pairing_manager: PairingManager 인스턴스
        on_paired: (device_id, uid) → None 콜백 (WS 브로드캐스트용)
        """
        self.pairing_manager = pairing_manager
        self.on_paired = on_paired
        self._unsubscribe = None
        self._lock = threading.Lock()

        try:
            self.db = firestore.client()
        except Exception as e:
            logger.warning(f"[FsListener] Firestore 연결 실패: {e}")
            self.db = None

    def start(self):
        if not self.db:
            logger.warning("[FsListener] DB 없음 — 리스너 미시작")
            return

        device_id = config.DEVICE_ID
        col_ref = self.db.collection("pairing_requests")
        # 첫 snapshot 은 기존 문서들이 ADDED 로 전달됨 — 옛 claimed=true PIN 까지 처리되어
        # 미페어링 상태인데 is_paired=True 로 잘못 set 되는 버그 방지
        first_snapshot_skipped = {"done": False}

        def _on_snapshot(col_snapshot, changes, read_time):
            if not first_snapshot_skipped["done"]:
                first_snapshot_skipped["done"] = True
                logger.info(f"[FsListener] 첫 snapshot ({len(changes)}개 기존 문서) 스킵 — 시작 후 변화만 처리")
                return

            for change in changes:
                if change.type.name not in ("ADDED", "MODIFIED"):
                    continue
                data = change.document.to_dict()
                if data.get("deviceId") != device_id:
                    continue
                if not data.get("claimed"):
                    continue
                uid = data.get("claimedBy", "")
                with self._lock:
                    if self.pairing_manager.is_paired:
                        continue
                    self.pairing_manager.is_paired = True
                    self.pairing_manager.pairing_code = None

                logger.info(f"[FsListener] 페어링 완료 감지: uid={uid}")
                if self.on_paired:
                    try:
                        self.on_paired(device_id, uid)
                    except Exception as e:
                        logger.error(f"[FsListener] on_paired 콜백 오류: {e}")

        self._unsubscribe = col_ref.on_snapshot(_on_snapshot)
        logger.info("[FsListener] pairing_requests 리스너 시작")

    def stop(self):
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        logger.info("[FsListener] 리스너 중지")
