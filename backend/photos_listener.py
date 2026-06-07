"""
Firestore photos 컬렉션 리스너
보호자 앱이 새 사진을 업로드하면 → on_new_photo(device_id, uri) 콜백 호출
모든 기기의 photos를 한 번에 구독하고 deviceId 기준으로 디스패치.
"""

import logging
from typing import Callable, Optional

from firebase_admin import firestore

logger = logging.getLogger(__name__)


class PhotosListener:
    def __init__(self, on_new_photo: Optional[Callable[[str, str], None]] = None):
        """on_new_photo(device_id: str, uri: str)"""
        self.on_new_photo = on_new_photo
        self._unsubscribe = None

        try:
            self.db = firestore.client()
        except Exception as e:
            logger.warning(f"[PhotosListener] Firestore 연결 실패: {e}")
            self.db = None

    def start(self):
        if not self.db:
            logger.warning("[PhotosListener] DB 없음 — 리스너 미시작")
            return

        col_ref = self.db.collection("photos")
        first_snapshot_skipped = {"done": False}

        def _on_snapshot(col_snapshot, changes, read_time):
            if not first_snapshot_skipped["done"]:
                first_snapshot_skipped["done"] = True
                logger.info(f"[PhotosListener] 첫 snapshot ({len(changes)}개 기존 문서) 스킵")
                return

            for change in changes:
                if change.type.name != "ADDED":
                    continue
                data = change.document.to_dict() or {}
                device_id = data.get("deviceId")
                if not device_id:
                    continue
                if not data.get("displayOnDevice", True):
                    continue
                uri = data.get("uri")
                if not uri:
                    continue

                logger.info(f"[PhotosListener] 새 사진 감지 device={device_id}: {uri[:60]}...")
                if self.on_new_photo:
                    try:
                        self.on_new_photo(device_id, uri)
                    except Exception as e:
                        logger.error(f"[PhotosListener] on_new_photo 콜백 오류: {e}")

        self._unsubscribe = col_ref.on_snapshot(_on_snapshot)
        logger.info("[PhotosListener] photos 리스너 시작")

    def stop(self):
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        logger.info("[PhotosListener] 리스너 중지")
