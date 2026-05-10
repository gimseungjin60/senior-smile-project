"""
Vision Engine — MediaPipe Hands(가위바위보) + Pose(스트레칭) 통합 엔진

- 싱글톤 패턴으로 모델 1회 로드 (라즈베리5 부하 최소화)
- 입력: numpy BGR 이미지 (OpenCV 형식)
- 출력: 손동작 분류 / 포즈 랜드마크 / 관절 각도

라즈베리5 환경 가정:
- 입력 프레임은 호출 측에서 미리 리사이징(권장 320x240)
- model_complexity=0 으로 가벼운 모델 사용
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

try:
    import mediapipe as mp
    _MP_AVAILABLE = True
except ImportError:
    _MP_AVAILABLE = False
    mp = None


# 손가락 끝/중간 랜드마크 인덱스 (MediaPipe Hands)
TIP_IDS = [4, 8, 12, 16, 20]   # thumb_tip, index_tip, middle_tip, ring_tip, pinky_tip
PIP_IDS = [3, 6, 10, 14, 18]   # 각 손가락 PIP(중간 관절)

# Pose 랜드마크 인덱스 (BlazePose 33-keypoint)
POSE_LANDMARK = {
    "nose": 0,
    "l_shoulder": 11, "r_shoulder": 12,
    "l_elbow": 13,    "r_elbow": 14,
    "l_wrist": 15,    "r_wrist": 16,
    "l_hip": 23,      "r_hip": 24,
    "l_knee": 25,     "r_knee": 26,
}


@dataclass
class HandResult:
    """손 인식 결과"""
    detected: bool
    gesture: str          # "rock" | "paper" | "scissors" | "unknown"
    landmarks: list       # [(x, y), ...] 정규화 좌표 0~1
    confidence: float


@dataclass
class PoseResult:
    """포즈 인식 결과"""
    detected: bool
    landmarks: list                      # [(x, y, visibility), ...]
    angles: dict                         # {"l_arm": deg, "r_arm": deg, ...}


class VisionEngine:
    """MediaPipe Hands + Pose 싱글톤. 스레드 세이프하게 1회 초기화."""

    _instance: Optional["VisionEngine"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        if not _MP_AVAILABLE:
            print("[VisionEngine] mediapipe 미설치 — pip install mediapipe 필요")
            self.hands = None
            self.pose = None
            return

        # Hands: max 2손, complexity 0(가벼움), 정적 이미지 모드 끔(영상 스트리밍)
        self.hands = mp.solutions.hands.Hands(
            static_image_mode=False,
            max_num_hands=2,
            model_complexity=0,
            min_detection_confidence=0.6,
            min_tracking_confidence=0.5,
        )
        # Pose: 가벼운 모델, segment 안 함
        self.pose = mp.solutions.pose.Pose(
            static_image_mode=False,
            model_complexity=0,
            enable_segmentation=False,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        print("[VisionEngine] MediaPipe Hands + Pose 초기화 완료 (complexity=0)")

    def is_ready(self) -> bool:
        return self.hands is not None and self.pose is not None

    # ─────────────────────────────────────────────────────────
    # 손동작 분류 (가위바위보)
    # ─────────────────────────────────────────────────────────
    def detect_hand(self, frame_bgr: np.ndarray) -> HandResult:
        """BGR 이미지에서 손동작을 분류합니다."""
        if not self.is_ready():
            return HandResult(False, "unknown", [], 0.0)

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        result = self.hands.process(rgb)
        if not result.multi_hand_landmarks:
            return HandResult(False, "unknown", [], 0.0)

        # 첫 번째 손만 사용 (정면 인터랙션 가정)
        hand = result.multi_hand_landmarks[0]
        lms = [(lm.x, lm.y) for lm in hand.landmark]

        gesture, conf = self._classify_rps(hand.landmark)
        return HandResult(True, gesture, lms, conf)

    def _classify_rps(self, landmarks) -> tuple[str, float]:
        """
        가위/바위/보 분류.
        - 펴진 손가락 개수로 1차 분류:
          0~1개 = 바위(rock)
          2개(검지+중지) = 가위(scissors)
          4~5개 = 보(paper)
        - 엄지는 손목 기준 좌우 좌표로 판별
        """
        # 검지~새끼: TIP.y < PIP.y 면 펴진 것 (이미지 좌표는 위쪽이 작은 y)
        extended = []
        for tip_id, pip_id in zip(TIP_IDS[1:], PIP_IDS[1:]):
            extended.append(landmarks[tip_id].y < landmarks[pip_id].y - 0.02)

        # 엄지: 손목(0번)과 엄지 끝(4번)의 x 비교 — 손이 어느 쪽인지 판단해 양방향 처리
        thumb_tip = landmarks[4]
        thumb_ip = landmarks[3]
        wrist = landmarks[0]
        # 엄지가 손바닥에서 멀리 떨어져있으면 펴진 것으로 간주 (x 거리)
        thumb_extended = abs(thumb_tip.x - wrist.x) > abs(thumb_ip.x - wrist.x) + 0.02
        extended_count = sum(extended) + (1 if thumb_extended else 0)

        index_up = extended[0]
        middle_up = extended[1]
        ring_up = extended[2]
        pinky_up = extended[3]

        # 가위: 검지+중지만 펴고 약지·새끼는 접힘
        if index_up and middle_up and not ring_up and not pinky_up:
            return "scissors", 0.9

        # 보: 4~5개 모두 펴짐
        if extended_count >= 4:
            return "paper", 0.9

        # 바위: 0~1개만 펴짐
        if extended_count <= 1:
            return "rock", 0.9

        # 모호한 상태 — 검지만 1개면 가위로 보기엔 부족
        return "unknown", 0.5

    # ─────────────────────────────────────────────────────────
    # 포즈 분석 (스트레칭)
    # ─────────────────────────────────────────────────────────
    def detect_pose(self, frame_bgr: np.ndarray) -> PoseResult:
        """BGR 이미지에서 포즈 랜드마크 + 주요 관절 각도를 계산합니다."""
        if not self.is_ready():
            return PoseResult(False, [], {})

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        result = self.pose.process(rgb)
        if not result.pose_landmarks:
            return PoseResult(False, [], {})

        lm = result.pose_landmarks.landmark
        landmarks = [(p.x, p.y, p.visibility) for p in lm]

        angles = {
            # 팔: 어깨-팔꿈치-손목
            "l_arm": _angle(lm, "l_shoulder", "l_elbow", "l_wrist"),
            "r_arm": _angle(lm, "r_shoulder", "r_elbow", "r_wrist"),
            # 어깨 으쓱: 어깨-귀(코를 근사)-반대 어깨 — 단순 어깨 높이로 표현
            "shoulder_lift": _shoulder_lift(lm),
            # 몸통 기울기: 양 어깨 라인의 수평도 (라디안)
            "torso_tilt": _torso_tilt(lm),
        }
        return PoseResult(True, landmarks, angles)


# ─────────────────────────────────────────────────────────
# 각도 계산 유틸 (모듈 함수)
# ─────────────────────────────────────────────────────────
def _angle(lm, a_key: str, b_key: str, c_key: str) -> float:
    """A-B-C 세 점이 이루는 각도(도). B가 꼭짓점."""
    a = lm[POSE_LANDMARK[a_key]]
    b = lm[POSE_LANDMARK[b_key]]
    c = lm[POSE_LANDMARK[c_key]]
    ba = (a.x - b.x, a.y - b.y)
    bc = (c.x - b.x, c.y - b.y)
    dot = ba[0] * bc[0] + ba[1] * bc[1]
    mag_ba = math.hypot(*ba)
    mag_bc = math.hypot(*bc)
    if mag_ba < 1e-6 or mag_bc < 1e-6:
        return 0.0
    cos_v = max(-1.0, min(1.0, dot / (mag_ba * mag_bc)))
    return math.degrees(math.acos(cos_v))


def _shoulder_lift(lm) -> float:
    """어깨 으쓱 정도 — 어깨와 코의 y 차이 (값이 작을수록 으쓱)."""
    nose = lm[POSE_LANDMARK["nose"]]
    l_sh = lm[POSE_LANDMARK["l_shoulder"]]
    r_sh = lm[POSE_LANDMARK["r_shoulder"]]
    avg_sh_y = (l_sh.y + r_sh.y) / 2
    return float(avg_sh_y - nose.y)  # 클수록 어깨가 아래(보통 자세), 작을수록 으쓱


def _torso_tilt(lm) -> float:
    """양 어깨 라인의 기울기(도). 0이면 수평."""
    l_sh = lm[POSE_LANDMARK["l_shoulder"]]
    r_sh = lm[POSE_LANDMARK["r_shoulder"]]
    return math.degrees(math.atan2(r_sh.y - l_sh.y, r_sh.x - l_sh.x))


# ─────────────────────────────────────────────────────────
# 게임 판정 (져주기 RPS)
# ─────────────────────────────────────────────────────────
LOSE_MAP = {
    "rock": "scissors",     # AI가 바위면 → 사용자는 가위(져야 함)
    "paper": "rock",
    "scissors": "paper",
}

def judge_lose_game(ai_move: str, user_move: str) -> str:
    """
    '져주기' 게임 판정.
    - 사용자가 AI에게 져야 성공 (lose)
    - 비기면 draw, 사용자가 이기면 win(=실패)
    """
    if user_move == "unknown":
        return "unknown"
    if ai_move == user_move:
        return "draw"
    if LOSE_MAP.get(ai_move) == user_move:
        return "lose"   # 의도대로 짐 = 성공
    return "win"        # 사용자가 이김 = 실패
