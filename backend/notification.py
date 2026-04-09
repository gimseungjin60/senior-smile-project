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
from firebase_admin import messaging

logger = logging.getLogger(__name__)


class NotificationManager:
    def __init__(self):
        # 보호자 디바이스 FCM 토큰 목록 (등록 API 또는 페어링에서 추가)
        self._tokens: list[str] = []
        self._lock = threading.Lock()

        # 페어링 매니저 참조 (main.py에서 주입)
        self.pairing = None

        # Firebase 앱이 이미 초기화되어 있는지 확인
        self._firebase_ready = len(firebase_admin._apps) > 0
        if not self._firebase_ready:
            logger.warning("[Notification] Firebase 미초기화 상태. 푸시 알림 비활성화.")

        # 중복 알림 방지 (같은 타입의 알림을 짧은 시간 내 재전송 차단)
        self._last_sent: dict[str, float] = {}
        self._cooldown_seconds = 60  # 동일 알림 최소 간격

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

    def _send(self, title: str, body: str, event_type: str, data: dict = None):
        """FCM 멀티캐스트 전송 (스레드 안전)"""
        if not self._firebase_ready:
            logger.debug(f"[Notification] Firebase 미초기화. 알림 스킵: {title}")
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
            logger.debug(f"[Notification] 등록된 토큰 없음. 알림 스킵: {title}")
            return

        if not self._can_send(event_type):
            logger.debug(f"[Notification] 쿨다운 중. 알림 스킵: {event_type}")
            return

        notification = messaging.Notification(title=title, body=body)
        payload = data or {}
        payload["event_type"] = event_type
        payload["timestamp"] = datetime.now().isoformat()

        message = messaging.MulticastMessage(
            notification=notification,
            data={k: str(v) for k, v in payload.items()},
            tokens=tokens,
        )

        try:
            response = messaging.send_each_for_multicast(message)
            logger.info(
                f"[Notification] 전송 완료: {title} "
                f"(성공 {response.success_count}, 실패 {response.failure_count})"
            )

            # 실패한 토큰 자동 정리 (만료/삭제된 디바이스)
            if response.failure_count > 0:
                self._cleanup_failed_tokens(tokens, response.responses)

        except Exception as e:
            logger.error(f"[Notification] 전송 실패: {e}")

    def _cleanup_failed_tokens(self, tokens, responses):
        """전송 실패한 토큰을 자동 제거합니다."""
        remove_codes = {
            "NOT_FOUND",
            "UNREGISTERED",
            "INVALID_ARGUMENT",
        }
        with self._lock:
            for token, resp in zip(tokens, responses):
                if resp.exception and hasattr(resp.exception, 'code'):
                    if resp.exception.code in remove_codes:
                        if token in self._tokens:
                            self._tokens.remove(token)
                            logger.info(f"[Notification] 만료 토큰 자동 제거")

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
