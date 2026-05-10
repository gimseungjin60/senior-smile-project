import { useState, useEffect, useRef, useCallback } from 'react'
import { seniorWsUrl } from '../utils/host'
import './CognitiveGame.css'

const WS_URL = seniorWsUrl('/ws/vision')
const AI_MOVES = ['rock', 'paper', 'scissors']
const MOVE_LABEL = { rock: '바위', paper: '보', scissors: '가위' }
const MOVE_EMOJI = { rock: '✊', paper: '✋', scissors: '✌️' }
// 사용자가 져야 하므로 AI 손에 지는 답을 힌트로 표시
const LOSE_TARGET = { rock: 'scissors', paper: 'rock', scissors: 'paper' }

const ROUND_TIMER_MS = 2000
const CAPTURE_INTERVAL_MS = 250

/**
 * 인지 게임: 가위바위보 '져주기' 모드
 * - AI가 무엇을 내든 사용자는 의도적으로 져야 성공
 * - 억제 제어(inhibitory control) 훈련 → 치매 예방
 *
 * 부모(App)가 onExit prop으로 종료 처리. 카메라/웹소켓은 마운트 동안만 활성화.
 */
export default function CognitiveGame({ onExit }) {
  const [phase, setPhase] = useState('intro')      // intro | showAi | capture | result
  const [aiMove, setAiMove] = useState(null)
  const [userMove, setUserMove] = useState('unknown')
  const [judgement, setJudgement] = useState(null) // lose(=성공) | win(=실패) | draw | unknown
  const [round, setRound] = useState(1)
  const [secondsLeft, setSecondsLeft] = useState(2)
  const [wsReady, setWsReady] = useState(false)
  const [errorMsg, setErrorMsg] = useState('')

  const videoRef = useRef(null)
  const canvasRef = useRef(null)
  const wsRef = useRef(null)
  const phaseTimer = useRef(null)
  const captureTimer = useRef(null)
  const tickTimer = useRef(null)
  const lastJudgement = useRef('unknown')

  // 카메라 시작
  useEffect(() => {
    let stream
    navigator.mediaDevices
      .getUserMedia({ video: { width: 320, height: 240 }, audio: false })
      .then((s) => {
        stream = s
        if (videoRef.current) videoRef.current.srcObject = s
      })
      .catch((e) => {
        console.warn('카메라 접근 실패:', e)
        setErrorMsg('카메라를 사용할 수 없어요. 권한을 확인해주세요.')
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
        if (data.type === 'rps' && data.detected) {
          setUserMove(data.gesture)
          // 마지막으로 인식된 판정을 보존(가장 최근 프레임이 결정에 가장 가까움)
          if (data.judgement && data.judgement !== 'unknown') {
            lastJudgement.current = data.judgement
            setJudgement(data.judgement)
          }
        }
        if (data.type === 'error') {
          setErrorMsg(data.message || '비전 엔진 오류')
        }
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
    ws.send(JSON.stringify({ type: 'frame', mode: 'rps', data: dataUrl }))
  }, [])

  function startRound() {
    const move = AI_MOVES[Math.floor(Math.random() * AI_MOVES.length)]
    setAiMove(move)
    setUserMove('unknown')
    setJudgement(null)
    lastJudgement.current = 'unknown'
    setPhase('showAi')
    setSecondsLeft(2)

    // AI 손동작 1.5초간 보여준 뒤 캡처 시작
    phaseTimer.current = setTimeout(() => {
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'config', aiMove: move }))
      }
      setPhase('capture')

      const start = Date.now()
      captureTimer.current = setInterval(captureFrame, CAPTURE_INTERVAL_MS)
      tickTimer.current = setInterval(() => {
        const elapsed = Date.now() - start
        const remain = Math.max(0, Math.ceil((ROUND_TIMER_MS - elapsed) / 1000))
        setSecondsLeft(remain)
        if (elapsed >= ROUND_TIMER_MS) {
          clearInterval(captureTimer.current)
          clearInterval(tickTimer.current)
          // 결과: 마지막으로 확정된 판정 사용
          setJudgement(lastJudgement.current || 'unknown')
          setPhase('result')
        }
      }, 250)
    }, 1500)
  }

  function nextRound() {
    setRound((r) => r + 1)
    startRound()
  }

  // 언마운트 시 모든 타이머 정리
  useEffect(() => () => {
    clearTimeout(phaseTimer.current)
    clearInterval(captureTimer.current)
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
      <canvas ref={canvasRef} style={{ display: 'none' }} />

      <div className="cog-stage">
        {errorMsg && <div className="cog-error">{errorMsg}</div>}

        {phase === 'intro' && (
          <div className="cog-intro">
            <p className="cog-msg">
              손주가 내는 손에 <strong>져 주세요!</strong>
            </p>
            <p className="cog-sub">2초 안에 지는 손동작을 보여주세요 헤헤~</p>
            <button className="cog-start" onClick={startRound} disabled={!wsReady}>
              {wsReady ? '시작' : '준비 중...'}
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
