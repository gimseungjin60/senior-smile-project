import { useState, useEffect, useRef, useCallback } from 'react'
import './StretchingGuide.css'

const WS_URL = 'ws://localhost:8000/ws/vision'
const CAPTURE_INTERVAL_MS = 200

// 동작별 목표 각도/조건. 각도는 도(°) 단위.
const EXERCISES = [
  {
    key: 'arms_extend',
    title: '앉아서 팔 쭉 펴기',
    description: '양팔을 어깨 높이로 앞으로 쭉 펴주세요',
    holdSeconds: 3,
    isAchieved: (angles) =>
      angles && angles.l_arm > 155 && angles.r_arm > 155,
    progress: (angles) => {
      if (!angles) return 0
      const avg = ((angles.l_arm || 0) + (angles.r_arm || 0)) / 2
      // 90도(접힘) ~ 170도(완전히 펴짐) 구간을 0~100%로 매핑
      return Math.max(0, Math.min(100, ((avg - 90) / 80) * 100))
    },
  },
  {
    key: 'shoulder_lift',
    title: '어깨 으쓱하기',
    description: '어깨를 천천히 위로 올렸다 내려주세요',
    holdSeconds: 2,
    // shoulder_lift 값이 작을수록 어깨가 올라간 상태
    isAchieved: (angles) => angles && angles.shoulder_lift < 0.18,
    progress: (angles) => {
      if (!angles) return 0
      // 0.30(평소) ~ 0.10(많이 으쓱) 구간을 0~100%로 매핑
      const v = angles.shoulder_lift ?? 0.3
      return Math.max(0, Math.min(100, ((0.3 - v) / 0.2) * 100))
    },
  },
]

/**
 * 스트레칭 가이드: MediaPipe Pose 랜드마크를 캔버스에 실시간 드로잉.
 * 동작 가이드 + 진행 게이지 표시. 목표 각도 도달 후 holdSeconds 동안 유지하면 완료.
 */
export default function StretchingGuide({ onExit }) {
  const [exerciseIdx, setExerciseIdx] = useState(0)
  const [progress, setProgress] = useState(0)
  const [holdMs, setHoldMs] = useState(0)
  const [completed, setCompleted] = useState(false)
  const [wsReady, setWsReady] = useState(false)
  const [errorMsg, setErrorMsg] = useState('')
  const [poseLandmarks, setPoseLandmarks] = useState(null)

  const videoRef = useRef(null)
  const canvasRef = useRef(null)
  const overlayRef = useRef(null)
  const wsRef = useRef(null)
  const captureTimer = useRef(null)
  const lastAngles = useRef(null)
  const holdStartRef = useRef(null)

  const exercise = EXERCISES[exerciseIdx]

  // 카메라
  useEffect(() => {
    let stream
    navigator.mediaDevices
      .getUserMedia({ video: { width: 480, height: 360 }, audio: false })
      .then((s) => {
        stream = s
        if (videoRef.current) videoRef.current.srcObject = s
      })
      .catch((e) => {
        console.warn('카메라 접근 실패:', e)
        setErrorMsg('카메라를 사용할 수 없어요.')
      })
    return () => {
      if (stream) stream.getTracks().forEach((t) => t.stop())
    }
  }, [])

  // WebSocket
  useEffect(() => {
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws
    ws.onopen = () => setWsReady(true)
    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data)
        if (data.type === 'pose' && data.detected) {
          lastAngles.current = data.angles
          setPoseLandmarks(data.landmarks)
        }
        if (data.type === 'error') setErrorMsg(data.message)
      } catch {}
    }
    ws.onclose = () => setWsReady(false)
    ws.onerror = () => setErrorMsg('서버에 연결할 수 없어요')
    return () => {
      try { ws.close() } catch {}
    }
  }, [])

  const captureFrame = useCallback(() => {
    const video = videoRef.current
    const canvas = canvasRef.current
    const ws = wsRef.current
    if (!video || !canvas || !ws) return
    if (ws.readyState !== WebSocket.OPEN) return
    if (!video.videoWidth) return

    canvas.width = 320
    canvas.height = 240
    const ctx = canvas.getContext('2d')
    ctx.drawImage(video, 0, 0, 320, 240)
    const dataUrl = canvas.toDataURL('image/jpeg', 0.55)
    ws.send(JSON.stringify({ type: 'frame', mode: 'pose', data: dataUrl }))
  }, [])

  // 캡처 루프
  useEffect(() => {
    if (!wsReady) return
    captureTimer.current = setInterval(captureFrame, CAPTURE_INTERVAL_MS)
    return () => clearInterval(captureTimer.current)
  }, [wsReady, captureFrame])

  // 진행률 + hold 판정
  useEffect(() => {
    const tick = setInterval(() => {
      const angles = lastAngles.current
      const p = exercise.progress(angles)
      setProgress(p)

      if (exercise.isAchieved(angles)) {
        if (!holdStartRef.current) holdStartRef.current = Date.now()
        const heldFor = Date.now() - holdStartRef.current
        setHoldMs(heldFor)
        if (heldFor >= exercise.holdSeconds * 1000) {
          setCompleted(true)
        }
      } else {
        holdStartRef.current = null
        setHoldMs(0)
      }
    }, 200)
    return () => clearInterval(tick)
  }, [exercise])

  // 랜드마크를 오버레이 캔버스에 그리기
  useEffect(() => {
    if (!poseLandmarks || !overlayRef.current) return
    const canvas = overlayRef.current
    const video = videoRef.current
    if (!video || !video.videoWidth) return
    canvas.width = video.clientWidth
    canvas.height = video.clientHeight
    const ctx = canvas.getContext('2d')
    ctx.clearRect(0, 0, canvas.width, canvas.height)

    // 주요 연결선
    const CONNECTIONS = [
      [11, 13], [13, 15],   // 왼팔
      [12, 14], [14, 16],   // 오른팔
      [11, 12],             // 어깨선
      [11, 23], [12, 24],   // 몸통
      [23, 24],             // 골반선
    ]

    ctx.strokeStyle = '#FFD700'
    ctx.lineWidth = 4
    CONNECTIONS.forEach(([a, b]) => {
      const la = poseLandmarks[a]
      const lb = poseLandmarks[b]
      if (!la || !lb) return
      if (la[2] < 0.3 || lb[2] < 0.3) return // visibility 낮으면 스킵
      ctx.beginPath()
      ctx.moveTo(la[0] * canvas.width, la[1] * canvas.height)
      ctx.lineTo(lb[0] * canvas.width, lb[1] * canvas.height)
      ctx.stroke()
    })

    // 관절점
    ctx.fillStyle = '#16A34A'
    poseLandmarks.forEach((lm) => {
      if (lm[2] < 0.3) return
      ctx.beginPath()
      ctx.arc(lm[0] * canvas.width, lm[1] * canvas.height, 6, 0, 2 * Math.PI)
      ctx.fill()
    })
  }, [poseLandmarks])

  function nextExercise() {
    if (exerciseIdx + 1 < EXERCISES.length) {
      setExerciseIdx((i) => i + 1)
      setCompleted(false)
      setProgress(0)
      setHoldMs(0)
      holdStartRef.current = null
    } else {
      onExit?.()
    }
  }

  return (
    <div className="stretch-guide">
      <div className="stretch-header">
        <h1>스트레칭 — {exerciseIdx + 1}/{EXERCISES.length}</h1>
        <button className="stretch-exit" onClick={onExit}>나가기</button>
      </div>

      {errorMsg && <div className="stretch-error">{errorMsg}</div>}

      <div className="stretch-body">
        <div className="stretch-info">
          <h2 className="stretch-title">{exercise.title}</h2>
          <p className="stretch-desc">{exercise.description}</p>

          <div className="stretch-progress-wrap">
            <div className="stretch-progress-label">
              {completed ? '완료!' : '자세 정확도'}
            </div>
            <div className="stretch-progress-bar">
              <div
                className={`stretch-progress-fill ${completed ? 'stretch-progress-fill--done' : ''}`}
                style={{ width: `${Math.max(progress, completed ? 100 : 0)}%` }}
              />
            </div>
            <div className="stretch-progress-text">
              {completed
                ? `잘하셨어요! ${exercise.holdSeconds}초 유지 성공`
                : `${Math.round(progress)}% · 유지 ${(holdMs / 1000).toFixed(1)}초`}
            </div>
          </div>

          {completed && (
            <button className="stretch-next" onClick={nextExercise}>
              {exerciseIdx + 1 < EXERCISES.length ? '다음 동작' : '마치기'}
            </button>
          )}
        </div>

        <div className="stretch-camera-wrap">
          <video ref={videoRef} autoPlay playsInline muted className="stretch-video" />
          <canvas ref={overlayRef} className="stretch-overlay" />
          <canvas ref={canvasRef} style={{ display: 'none' }} />
        </div>
      </div>
    </div>
  )
}
