import { useState, useEffect, useRef, useCallback } from 'react'
import { createHandLandmarker, classifyRPS, judgeLoseGame } from '../utils/vision'
import './CognitiveGame.css'

const AI_MOVES = ['rock', 'paper', 'scissors']
const MOVE_LABEL = { rock: '바위', paper: '보', scissors: '가위' }
const MOVE_EMOJI = { rock: '✊', paper: '✋', scissors: '✌️' }
// 사용자가 져야 하므로 AI 손에 지는 답을 힌트로 표시
const LOSE_TARGET = { rock: 'scissors', paper: 'rock', scissors: 'paper' }

const ROUND_TIMER_MS = 2000
const DETECT_INTERVAL_MS = 120   // 브라우저 추론 throttle (rAF 과다 setState 방지)

/**
 * 인지 게임: 가위바위보 '져주기' 모드
 * - AI가 무엇을 내든 사용자는 의도적으로 져야 성공
 * - 억제 제어(inhibitory control) 훈련 → 치매 예방
 *
 * 비전은 브라우저 MediaPipe Web(HandLandmarker)으로 직접 추론. (서버 /ws/vision 제거)
 * 부모(App)가 onExit prop으로 종료 처리. 카메라/모델은 마운트 동안만 활성화.
 */
export default function CognitiveGame({ onExit }) {
  const [phase, setPhase] = useState('intro')      // intro | showAi | capture | result
  const [aiMove, setAiMove] = useState(null)
  const [judgement, setJudgement] = useState(null) // lose(=성공) | win(=실패) | draw | unknown
  const [round, setRound] = useState(1)
  const [secondsLeft, setSecondsLeft] = useState(2)
  const [ready, setReady] = useState(false)        // 카메라+모델 준비됨
  const [errorMsg, setErrorMsg] = useState('')
  const [debugInfo, setDebugInfo] = useState({ rxCount: 0, lastGesture: '-', lastDetected: false })

  const videoRef = useRef(null)
  const landmarkerRef = useRef(null)
  const rafRef = useRef(null)
  const lastDetectTs = useRef(0)
  const phaseTimer = useRef(null)
  const tickTimer = useRef(null)
  const lastJudgement = useRef('unknown')
  // 추론 루프가 stale closure 없이 현재 라운드 상태를 읽도록 ref로 보관
  const phaseRef = useRef('intro')
  const aiMoveRef = useRef(null)

  useEffect(() => { phaseRef.current = phase }, [phase])

  // 카메라 + HandLandmarker 초기화
  useEffect(() => {
    let stream
    let cancelled = false

    async function init() {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { width: 320, height: 240 },
          audio: false,
        })
        if (cancelled) return
        if (videoRef.current) videoRef.current.srcObject = stream
      } catch (e) {
        console.warn('카메라 접근 실패:', e)
        setErrorMsg('카메라를 사용할 수 없어요. 권한을 확인해주세요.')
        return
      }
      try {
        landmarkerRef.current = await createHandLandmarker()
        if (cancelled) return
        setReady(true)
        setErrorMsg('')
      } catch (e) {
        console.warn('비전 모델 로드 실패:', e)
        setErrorMsg('비전 모델을 불러오지 못했어요. 인터넷 연결을 확인해주세요.')
      }
    }
    init()

    return () => {
      cancelled = true
      cancelAnimationFrame(rafRef.current)
      if (stream) stream.getTracks().forEach((t) => t.stop())
      try { landmarkerRef.current?.close() } catch { /* 무시 */ }
      landmarkerRef.current = null
    }
  }, [])

  // 손동작 추론 루프 — 준비되면 항상 실행, capture 단계에서만 판정 확정
  useEffect(() => {
    if (!ready) return
    let stopped = false

    const loop = () => {
      if (stopped) return
      const video = videoRef.current
      const landmarker = landmarkerRef.current
      const now = performance.now()
      if (video && landmarker && video.readyState >= 2 && now - lastDetectTs.current >= DETECT_INTERVAL_MS) {
        lastDetectTs.current = now
        let detected = false
        let gesture = 'unknown'
        try {
          const res = landmarker.detectForVideo(video, now)
          if (res.landmarks && res.landmarks.length > 0) {
            detected = true
            gesture = classifyRPS(res.landmarks[0]).gesture
          }
        } catch { /* 추론 실패 프레임은 무시 */ }

        setDebugInfo((d) => ({
          rxCount: d.rxCount + 1,
          lastGesture: gesture,
          lastDetected: detected,
        }))

        if (detected && phaseRef.current === 'capture') {
          const j = aiMoveRef.current ? judgeLoseGame(aiMoveRef.current, gesture) : 'unknown'
          if (j !== 'unknown') {
            lastJudgement.current = j
            setJudgement(j)
          }
        }
      }
      rafRef.current = requestAnimationFrame(loop)
    }
    rafRef.current = requestAnimationFrame(loop)

    return () => {
      stopped = true
      cancelAnimationFrame(rafRef.current)
    }
  }, [ready])

  const startRound = useCallback(() => {
    const move = AI_MOVES[Math.floor(Math.random() * AI_MOVES.length)]
    setAiMove(move)
    aiMoveRef.current = move
    setJudgement(null)
    lastJudgement.current = 'unknown'
    setPhase('showAi')
    setSecondsLeft(2)

    // AI 손동작 1.5초간 보여준 뒤 캡처 시작
    phaseTimer.current = setTimeout(() => {
      setPhase('capture')
      const start = performance.now()
      tickTimer.current = setInterval(() => {
        const elapsed = performance.now() - start
        const remain = Math.max(0, Math.ceil((ROUND_TIMER_MS - elapsed) / 1000))
        setSecondsLeft(remain)
        if (elapsed >= ROUND_TIMER_MS) {
          clearInterval(tickTimer.current)
          // 결과: 마지막으로 확정된 판정 사용
          setJudgement(lastJudgement.current || 'unknown')
          setPhase('result')
        }
      }, 250)
    }, 1500)
  }, [])

  function nextRound() {
    setRound((r) => r + 1)
    startRound()
  }

  // 언마운트 시 모든 타이머 정리
  useEffect(() => () => {
    clearTimeout(phaseTimer.current)
    clearInterval(tickTimer.current)
  }, [])

  const target = aiMove ? LOSE_TARGET[aiMove] : null

  return (
    <div className="cog-game">
      <div className="cog-header">
        <h1>져주기 게임 — {round}회</h1>
        <button className="cog-exit" onClick={onExit}>나가기</button>
      </div>

      <video ref={videoRef} autoPlay playsInline muted className="cog-video" />
      <div className="cog-detect-label" style={{
        position: 'absolute', bottom: 180, right: 24, width: 200,
        background: debugInfo.lastDetected ? 'rgba(34,197,94,0.95)' : 'rgba(0,0,0,0.7)',
        color: '#fff', padding: '8px 12px', borderRadius: 8, textAlign: 'center',
        fontSize: 16, fontWeight: 700, zIndex: 11, border: '2px solid #FFD700',
      }}>
        {debugInfo.rxCount === 0
          ? '연결 대기 중...'
          : debugInfo.lastDetected
            ? `인식됨: ${MOVE_EMOJI[debugInfo.lastGesture] || '?'} ${MOVE_LABEL[debugInfo.lastGesture] || debugInfo.lastGesture}`
            : '손이 안 보여요'}
      </div>

      <div className="cog-stage">
        {errorMsg && <div className="cog-error">{errorMsg}</div>}
        {(phase === 'capture' || phase === 'showAi') && (
          <div style={{ position: 'absolute', top: 8, right: 8, fontSize: 12, color: '#888', background: 'rgba(0,0,0,0.4)', padding: '4px 8px', borderRadius: 4, zIndex: 10 }}>
            모델:{ready ? 'OK' : 'X'} · 추론:{debugInfo.rxCount} · 마지막:{debugInfo.lastDetected ? '✓' : '×'}{debugInfo.lastGesture}
          </div>
        )}

        {phase === 'intro' && (
          <div className="cog-intro">
            <p className="cog-msg">
              손주가 내는 손에 <strong>져 주세요!</strong>
            </p>
            <p className="cog-sub">2초 안에 지는 손동작을 보여주세요 헤헤~</p>
            <button className="cog-start" onClick={startRound} disabled={!ready}>
              {ready ? '시작' : '준비 중...'}
            </button>
          </div>
        )}

        {(phase === 'showAi' || phase === 'capture') && aiMove && (
          <>
            <div className="cog-round">
              <div className="cog-card">
                <div className="cog-card-label">손주는</div>
                <div className="cog-emoji">{MOVE_EMOJI[aiMove]}</div>
                <div className="cog-card-name">{MOVE_LABEL[aiMove]}</div>
              </div>
              <div className="cog-arrow">→</div>
              <div className="cog-card cog-card--target">
                <div className="cog-card-label">할머니는</div>
                <div className="cog-emoji">{MOVE_EMOJI[target]}</div>
                <div className="cog-card-name cog-card-name--target">
                  {MOVE_LABEL[target]}
                </div>
              </div>
            </div>
            <div className="cog-timer">
              {phase === 'capture' ? `${secondsLeft}초` : '준비...'}
            </div>
          </>
        )}

        {phase === 'result' && (
          <div className={`cog-result cog-result--${judgement}`}>
            {judgement === 'lose' && (
              <p>
                잘하셨어요!<br />
                제대로 <strong>져주셨네요</strong> 헤헤~
              </p>
            )}
            {judgement === 'draw' && (
              <p>어? <strong>비겼네요</strong>!<br />다시 해볼까요?</p>
            )}
            {judgement === 'win' && (
              <p>
                이런, <strong>이기셨어요</strong>.<br />
                다음엔 져주세요~
              </p>
            )}
            {(!judgement || judgement === 'unknown') && (
              <p>손이 잘 안 보였어요.<br />다시 해볼까요?</p>
            )}
            <div className="cog-actions">
              <button className="cog-btn cog-btn--primary" onClick={nextRound}>
                다음 게임
              </button>
              <button className="cog-btn cog-btn--ghost" onClick={onExit}>
                그만하기
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
