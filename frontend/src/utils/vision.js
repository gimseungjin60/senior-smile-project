/**
 * Vision — 브라우저 MediaPipe Web(@mediapipe/tasks-vision) 래퍼 + 분류/각도/판정 로직.
 *
 * 기존 백엔드 core/vision_engine.py(서버 비전)를 클라이언트로 이전한 것.
 * - 손동작(가위바위보) 분류 / 포즈 관절 각도 계산 / '져주기' 게임 판정
 * - 프레임을 WebSocket으로 서버에 보내지 않고 브라우저에서 직접 추론 → 지연·서버부하 감소
 *
 * 랜드마크 인덱스는 MediaPipe 규약이라 Python(solutions) 시절과 동일.
 */
import { FilesetResolver, HandLandmarker, PoseLandmarker } from '@mediapipe/tasks-vision'

// tasks-vision 버전과 동일하게 맞춰야 wasm 로더가 호환됨 (package.json: 0.10.35)
const WASM_BASE = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35/wasm'
const HAND_MODEL =
  'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task'
// lite 모델: 갤럭시탭 등 모바일 GPU에서 가벼움 (구계획 complexity=0 대응)
const POSE_MODEL =
  'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task'

// 손가락 끝/중간(PIP) 랜드마크 인덱스
const TIP_IDS = [4, 8, 12, 16, 20] // thumb, index, middle, ring, pinky
const PIP_IDS = [3, 6, 10, 14, 18]

// Pose 랜드마크 인덱스 (BlazePose 33-keypoint)
export const POSE_LANDMARK = {
  nose: 0,
  l_shoulder: 11, r_shoulder: 12,
  l_elbow: 13, r_elbow: 14,
  l_wrist: 15, r_wrist: 16,
  l_hip: 23, r_hip: 24,
  l_knee: 25, r_knee: 26,
}

let _fileset = null
async function getFileset() {
  if (!_fileset) _fileset = await FilesetResolver.forVisionTasks(WASM_BASE)
  return _fileset
}

/**
 * HandLandmarker 생성 (VIDEO 모드). 시연 환경 조명 가변성 대응 위해 confidence를 낮게.
 * 정면 인터랙션 가정이라 numHands=1.
 */
export async function createHandLandmarker() {
  const vision = await getFileset()
  return HandLandmarker.createFromOptions(vision, {
    baseOptions: { modelAssetPath: HAND_MODEL, delegate: 'GPU' },
    runningMode: 'VIDEO',
    numHands: 1,
    minHandDetectionConfidence: 0.3,
    minHandPresenceConfidence: 0.3,
    minTrackingConfidence: 0.3,
  })
}

/** PoseLandmarker 생성 (VIDEO 모드). 단일 인물. */
export async function createPoseLandmarker() {
  const vision = await getFileset()
  return PoseLandmarker.createFromOptions(vision, {
    baseOptions: { modelAssetPath: POSE_MODEL, delegate: 'GPU' },
    runningMode: 'VIDEO',
    numPoses: 1,
    minPoseDetectionConfidence: 0.5,
    minPosePresenceConfidence: 0.5,
    minTrackingConfidence: 0.5,
  })
}

/**
 * 가위/바위/보 분류. (Python _classify_rps 포팅)
 * @param {Array<{x:number,y:number}>} lm - 손 랜드마크 21개 (정규화 0~1)
 * @returns {{gesture: 'rock'|'paper'|'scissors'|'unknown', confidence: number}}
 */
export function classifyRPS(lm) {
  // 검지~새끼: TIP.y < PIP.y 면 펴진 것 (이미지 좌표는 위쪽이 작은 y)
  const extended = []
  for (let i = 1; i < TIP_IDS.length; i++) {
    extended.push(lm[TIP_IDS[i]].y < lm[PIP_IDS[i]].y - 0.02)
  }

  // 엄지: 손목(0)과 비교해 손바닥에서 멀리 떨어졌으면 펴진 것으로 간주
  const thumbTip = lm[4]
  const thumbIp = lm[3]
  const wrist = lm[0]
  const thumbExtended =
    Math.abs(thumbTip.x - wrist.x) > Math.abs(thumbIp.x - wrist.x) + 0.02
  const extendedCount = extended.filter(Boolean).length + (thumbExtended ? 1 : 0)

  const [indexUp, middleUp, ringUp, pinkyUp] = extended

  // 가위: 검지+중지만 펴고 약지·새끼는 접힘
  if (indexUp && middleUp && !ringUp && !pinkyUp) return { gesture: 'scissors', confidence: 0.9 }
  // 보: 4~5개 모두 펴짐
  if (extendedCount >= 4) return { gesture: 'paper', confidence: 0.9 }
  // 바위: 0~1개만 펴짐
  if (extendedCount <= 1) return { gesture: 'rock', confidence: 0.9 }

  return { gesture: 'unknown', confidence: 0.5 }
}

// '져주기' 게임 판정 (Python LOSE_MAP / judge_lose_game 포팅)
const LOSE_MAP = { rock: 'scissors', paper: 'rock', scissors: 'paper' }

/**
 * 사용자가 AI에게 져야 성공.
 * @returns {'lose'|'draw'|'win'|'unknown'} lose=성공, win=실패
 */
export function judgeLoseGame(aiMove, userMove) {
  if (userMove === 'unknown') return 'unknown'
  if (aiMove === userMove) return 'draw'
  if (LOSE_MAP[aiMove] === userMove) return 'lose'
  return 'win'
}

/** A-B-C 세 점이 이루는 각도(도). B가 꼭짓점. (Python _angle 포팅) */
function angle(lm, aKey, bKey, cKey) {
  const a = lm[POSE_LANDMARK[aKey]]
  const b = lm[POSE_LANDMARK[bKey]]
  const c = lm[POSE_LANDMARK[cKey]]
  const ba = [a.x - b.x, a.y - b.y]
  const bc = [c.x - b.x, c.y - b.y]
  const dot = ba[0] * bc[0] + ba[1] * bc[1]
  const magBa = Math.hypot(ba[0], ba[1])
  const magBc = Math.hypot(bc[0], bc[1])
  if (magBa < 1e-6 || magBc < 1e-6) return 0
  const cosV = Math.max(-1, Math.min(1, dot / (magBa * magBc)))
  return (Math.acos(cosV) * 180) / Math.PI
}

/** 어깨 으쓱 정도 — 어깨와 코의 y 차이 (작을수록 으쓱). */
function shoulderLift(lm) {
  const nose = lm[POSE_LANDMARK.nose]
  const lSh = lm[POSE_LANDMARK.l_shoulder]
  const rSh = lm[POSE_LANDMARK.r_shoulder]
  const avgShY = (lSh.y + rSh.y) / 2
  return avgShY - nose.y
}

/** 양 어깨 라인의 기울기(도). 0이면 수평. */
function torsoTilt(lm) {
  const lSh = lm[POSE_LANDMARK.l_shoulder]
  const rSh = lm[POSE_LANDMARK.r_shoulder]
  return (Math.atan2(rSh.y - lSh.y, rSh.x - lSh.x) * 180) / Math.PI
}

/**
 * 포즈 랜드마크에서 주요 관절 각도 계산. (Python detect_pose의 angles 포팅)
 * @param {Array<{x,y,z,visibility}>} lm - 33-keypoint
 */
export function computePoseAngles(lm) {
  return {
    l_arm: angle(lm, 'l_shoulder', 'l_elbow', 'l_wrist'),
    r_arm: angle(lm, 'r_shoulder', 'r_elbow', 'r_wrist'),
    shoulder_lift: shoulderLift(lm),
    torso_tilt: torsoTilt(lm),
  }
}
