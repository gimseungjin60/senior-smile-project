"""
페어링 매니저
- 시니어 디바이스 ↔ 보호자 앱 1:1(1:N) 매칭
- 6자리 페어링 코드 생성/검증
- Firebase Firestore에 매칭 정보 저장
"""

import random
import time
import logging
import threading
from typing import Optional

import firebase_admin
from firebase_admin import firestore

import config

logger = logging.getLogger(__name__)

PAIRING_CODE_LENGTH = 6
PAIRING_CODE_EXPIRY = 300  # 5분


class PairingManager:
    def __init__(self):
        self.device_id = config.DEVICE_ID
        self.pairing_code: Optional[str] = None
        self.code_expires_at: float = 0
        self.is_paired = False
        self.family_id: Optional[str] = None
        self._lock = threading.Lock()

        # Firebase DB
        self.db = None
        if firebase_admin._apps:
            try:
                self.db = firestore.client()
                logger.info(f"[Pairing] Firebase 연결 완료 (device: {self.device_id})")
            except Exception as e:
                logger.warning(f"[Pairing] Firebase 연결 실패: {e}")

        # 기존 페어링 상태 복구
        self._restore_pairing()

    def _restore_pairing(self):
        """서버 재시작 시 기존 페어링 상태를 Firebase에서 복구"""
        if not self.db:
            return
        try:
            doc = self.db.collection("devices").document(self.device_id).get()
            if doc.exists:
                data = doc.to_dict()
                if data.get("paired"):
                    self.is_paired = True
                    self.family_id = data.get("family_id")
                    logger.info(f"[Pairing] 기존 페어링 복구: family={self.family_id}")
        except Exception as e:
            logger.warning(f"[Pairing] 페어링 복구 실패: {e}")

    def generate_code(self) -> str:
        """6자리 페어링 코드를 생성합니다. 5분 후 만료."""
        with self._lock:
            self.pairing_code = "".join([str(random.randint(0, 9)) for _ in range(PAIRING_CODE_LENGTH)])
            self.code_expires_at = time.time() + PAIRING_CODE_EXPIRY

        # Firebase에 코드 등록
        if self.db:
            try:
                self.db.collection("devices").document(self.device_id).set({
                    "pairing_code": self.pairing_code,
                    "code_expires_at": self.code_expires_at,
                    "paired": self.is_paired,
                    "family_id": self.family_id,
                }, merge=True)
            except Exception as e:
                logger.warning(f"[Pairing] 코드 Firebase 저장 실패: {e}")

        logger.info(f"[Pairing] 코드 생성: {self.pairing_code} (5분 유효)")
        return self.pairing_code

    def get_code(self) -> Optional[str]:
        """현재 유효한 코드를 반환합니다. 만료 시 None."""
        with self._lock:
            if self.pairing_code and time.time() < self.code_expires_at:
                return self.pairing_code
            return None

    def get_remaining_seconds(self) -> int:
        """코드 남은 시간(초)"""
        with self._lock:
            if self.pairing_code and time.time() < self.code_expires_at:
                return int(self.code_expires_at - time.time())
            return 0

    def verify_and_pair(self, code: str, user_id: str, user_name: str, fcm_token: str = "") -> dict:
        """
        보호자 앱에서 코드를 입력하면 매칭을 수행합니다.
        Returns: {"success": bool, "error": str, "family_id": str}
        """
        with self._lock:
            # 코드 유효성 검사
            if not self.pairing_code:
                return {"success": False, "error": "페어링 코드가 생성되지 않았습니다."}
            if time.time() >= self.code_expires_at:
                self.pairing_code = None
                return {"success": False, "error": "코드가 만료되었습니다. 새 코드를 생성해주세요."}
            if code != self.pairing_code:
                return {"success": False, "error": "코드가 일치하지 않습니다."}

            # 매칭 수행
            if not self.family_id:
                self.family_id = f"family_{self.device_id}_{int(time.time())}"

            self.is_paired = True
            self.pairing_code = None  # 사용한 코드 폐기

        # Firebase에 저장
        if self.db:
            try:
                # 디바이스 문서 업데이트
                self.db.collection("devices").document(self.device_id).set({
                    "paired": True,
                    "family_id": self.family_id,
                    "pairing_code": None,
                }, merge=True)

                # 가족 그룹 생성/업데이트
                family_ref = self.db.collection("families").document(self.family_id)
                family_doc = family_ref.get()

                if family_doc.exists:
                    # 기존 가족에 멤버 추가
                    family_ref.update({
                        "members": firestore.ArrayUnion([{
                            "user_id": user_id,
                            "name": user_name,
                            "fcm_token": fcm_token,
                            "joined_at": time.time(),
                        }])
                    })
                else:
                    # 새 가족 그룹 생성
                    family_ref.set({
                        "senior_device_id": self.device_id,
                        "members": [{
                            "user_id": user_id,
                            "name": user_name,
                            "fcm_token": fcm_token,
                            "joined_at": time.time(),
                        }],
                        "created_at": time.time(),
                    })

                # 사용자 문서 생성
                self.db.collection("users").document(user_id).set({
                    "name": user_name,
                    "family_id": self.family_id,
                    "device_id": self.device_id,
                    "fcm_token": fcm_token,
                    "role": "보호자",
                }, merge=True)

            except Exception as e:
                logger.error(f"[Pairing] Firebase 저장 실패: {e}")

        logger.info(f"[Pairing] 매칭 완료! user={user_name}, family={self.family_id}")
        return {
            "success": True,
            "family_id": self.family_id,
            "device_id": self.device_id,
        }

    def unpair(self) -> bool:
        """페어링을 해제합니다."""
        with self._lock:
            self.is_paired = False
            old_family = self.family_id
            self.family_id = None
            self.pairing_code = None

        if self.db and old_family:
            try:
                self.db.collection("devices").document(self.device_id).set({
                    "paired": False,
                    "family_id": None,
                    "pairing_code": None,
                }, merge=True)
            except Exception as e:
                logger.warning(f"[Pairing] 해제 Firebase 업데이트 실패: {e}")

        logger.info("[Pairing] 페어링 해제 완료")
        return True

    def get_family_fcm_tokens(self) -> list[str]:
        """매칭된 가족 그룹의 모든 FCM 토큰을 가져옵니다."""
        if not self.db or not self.family_id:
            return []
        try:
            doc = self.db.collection("families").document(self.family_id).get()
            if doc.exists:
                members = doc.to_dict().get("members", [])
                return [m["fcm_token"] for m in members if m.get("fcm_token")]
        except Exception as e:
            logger.warning(f"[Pairing] FCM 토큰 조회 실패: {e}")
        return []

    def get_status(self) -> dict:
        """현재 페어링 상태를 반환합니다."""
        code = self.get_code()
        return {
            "device_id": self.device_id,
            "is_paired": self.is_paired,
            "family_id": self.family_id,
            "pairing_code": code,
            "code_remaining_seconds": self.get_remaining_seconds() if code else 0,
        }
