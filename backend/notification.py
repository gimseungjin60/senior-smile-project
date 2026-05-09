"""
FCM 푸시 알림 매니저
- 보호자 앱(가족)에게 어르신 상태 알림 전송
- Firebase Cloud Messaging 사용
"""

import logging
import threading
from datetime import datetime
from pathlib import Path

import firebase_admin

logger = logging.getLogger(__name__)


class NotificationManager:
    def __init__(self):
        # 보호자 디바이스 FCM 토큰 목록 (등록 API 또는 페어링에서 추가)
        self._tokens: list[str] = []
        self._lock = threading.Lock()

        # 페어링 매니저 참조 (main.py에서 주입)
        self.pairing = None

        # 중복 알림 방지 (같은 타입의 알림을 짧은 시간 내 재전송 차단)
        self._last_sent: dict[str, float] = {}
        self._cooldown_seconds = 60  # 동일 알림 최소 간격

    def _get_db(self):
        """지연 초기화로 Firestore 클라이언트 획득"""
        if not firebase_admin._apps:
            try:
                from firebase_admin import credentials
                key_path = Path(__file__).parent / "serviceAccountKey.json"
                if key_path.exists():
                    cred = credentials.Certificate(str(key_path))
                    firebase_admin.initialize_app(cred)
                    logger.info("[Notification] Firebase 앱 지연 초기화 성공")
                else:
                    logger.error("[Notification] serviceAccountKey.json 파일을 찾을 수 없습니다.")
                    return None
            except Exception as e:
                logger.error(f"[Notification] Firebase 지연 초기화 에러: {e}")
                return None

        try:
            from firebase_admin import firestore
            return firestore.client()
        except Exception as e:
            logger.error(f"[Notification] Firestore 클라이언트 획득 실패: {e}")
            return None

    def register_token(self, token: str) -> bool:
        """보호자 디바이스 토큰을 등록합니다."""
        with self._lock:
            if token not in self._tokens:
                self._tokens.append(token)
                logger.info(f"[Notification] 토큰 등록 완료 (총 {len(self._tokens)}개)")
                return True
            return False

    def unregister_token(self, token: str) -> bool:
        """보호자 디바이스 토큰을 해제합니다."""
        with self._lock:
            if token in self._tokens:
                self._tokens.remove(token)
                logger.info(f"[Notification] 토큰 해제 완료 (총 {len(self._tokens)}개)")
                return True
            return False

    def get_token_count(self) -> int:
        with self._lock:
            return len(self._tokens)

    def _can_send(self, event_type: str) -> bool:
        """중복 알림 방지 체크"""
        now = datetime.now().timestamp()
        last = self._last_sent.get(event_type, 0)
        if now - last < self._cooldown_seconds:
            return False
        self._last_sent[event_type] = now
        return True

    def _save_to_db(self, title: str, body: str, event_type: str):
        """Firestore에 알림 기록 저장 (토큰 유무와 무관하게 항상 실행)"""
        category_map = {
            "emergency": "emergency",
            "pill_missed": "warning",
            "pill_taken": "general",
            "photo_viewed": "general",
            "voice_reply": "general",
            "session_start": "general",
            "session_end": "general",
        }
        notif_type = category_map.get(event_type, "general")

        db = self._get_db()
        if db:
            try:
                from firebase_admin import firestore
                db.collection("notifications").add({
                    "type": notif_type,
                    "title": title,
                    "body": body,
                    "event_type": event_type,
                    "createdAt": firestore.SERVER_TIMESTAMP,
                    "isRead": False
                })
                logger.info(f"[Notification] DB 저장 완료: {title}")
            except Exception as e:
                logger.error(f"[Notification] DB 저장 실패: {e}")
        else:
            logger.warning(f"[Notification] DB 인스턴스 없음, DB 저장 생략: {title}")

    def _send(self, title: str, body: str, event_type: str, data: dict = None):
        """DB 저장 후 Expo Push 알림 전송"""
        # ✅ DB 저장은 토큰 유무와 무관하게 항상 먼저 실행
        self._save_to_db(title, body, event_type)

        if not self._can_send(event_type):
            logger.debug(f"[Notification] 쿨다운 중. 푸시 알림 스킵: {event_type}")
            return

        with self._lock:
            tokens = list(self._tokens)

        # 페어링된 가족의 FCM 토큰도 포함
        if self.pairing:
            family_tokens = self.pairing.get_family_fcm_tokens()
            for t in family_tokens:
                if t not in tokens:
                    tokens.append(t)

        if not tokens:
            logger.debug(f"[Notification] 등록된 FCM 토큰 없음. 푸시 알림 스킵: {title}")
            return

        payload = data or {}
        payload["event_type"] = event_type
        payload["timestamp"] = datetime.now().isoformat()

        # Expo Push API로 전송
        import urllib.request
        import urllib.error
        import json as _json

        messages = []
        for token in tokens:
            messages.append({
                "to": token,
                "title": title,
                "body": body,
                "sound": "default",
                "data": {k: str(v) for k, v in payload.items()},
            })

        try:
            req_data = _json.dumps(messages).encode("utf-8")
            req = urllib.request.Request(
                "https://exp.host/--/api/v2/push/send",
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = _json.loads(resp.read().decode("utf-8"))
                success = sum(1 for r in result.get("data", []) if r.get("status") == "ok")
                fail = len(result.get("data", [])) - success
                logger.info(f"[Notification] 전송 완료: {title} (성공 {success}, 실패 {fail})")
        except Exception as e:
            logger.error(f"[Notification] Expo Push 전송 실패: {e}")


    # === 알림 이벤트 메서드 ===

    def notify_session_start(self):
        """어르신 감지 알림"""
        self._send(
            title="어르신 활동 감지",
            body="어르신이 액자 앞에 오셨어요. 대화를 시작합니다.",
            event_type="session_start",
        )

    def notify_session_end(self, duration_seconds: int, emotion_report: str = ""):
        """세션 종료 + 감정 리포트 알림"""
        minutes = duration_seconds // 60
        duration_text = f"{minutes}분" if minutes > 0 else f"{duration_seconds}초"

        body = f"어르신과 {duration_text}간 대화했어요."
        if emotion_report:
            body += f"\n{emotion_report}"

        self._send(
            title="대화 종료 리포트",
            body=body,
            event_type="session_end",
            data={"duration_seconds": duration_seconds},
        )

    def notify_pill_taken(self):
        """약 복용 확인 알림"""
        now = datetime.now().strftime("%H:%M")
        self._send(
            title="약 복용 완료",
            body=f"어르신이 {now}에 약을 드셨어요.",
            event_type="pill_taken",
        )

    def notify_pill_missed(self):
        """약 미복용 경고 알림"""
        self._send(
            title="약 복용 미확인",
            body="알림을 드렸지만 아직 약 복용이 확인되지 않았어요.",
            event_type="pill_missed",
        )

    def notify_new_photo_viewed(self):
        """보호자가 보낸 사진을 어르신이 확인한 알림"""
        self._send(
            title="사진 확인 완료",
            body="어르신이 보내신 사진을 보셨어요!",
            event_type="photo_viewed",
        )

    def notify_voice_reply(self, text: str = ""):
        """어르신이 음성 답장을 보낸 알림"""
        body = "어르신이 음성 답장을 보내셨어요!"
        if text:
            body += f'\n"{text}"'
        self._send(
            title="음성 답장 도착",
            body=body,
            event_type="voice_reply",
        )

    def notify_emergency(self, trigger_text: str = ""):
        """긴급 상황 알림 (쿨다운 무시, 즉시 발송)"""
        now = datetime.now().strftime("%H:%M")
        body = f"[{now}] 어르신이 도움을 요청하고 있어요!"
        if trigger_text:
            body += f'\n"{trigger_text}"'

        # 긴급 알림은 쿨다운 무시
        self._last_sent.pop("emergency", None)
        self._send(
            title="🚨 긴급 상황 발생",
            body=body,
            event_type="emergency",
            data={"trigger_text": trigger_text, "priority": "high"},
        )
