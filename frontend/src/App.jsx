import { useState, useEffect, useRef } from 'react'
import IdleScreen from './components/IdleScreen'
import GreetScreen from './components/GreetScreen'
import ActiveScreen from './components/ActiveScreen'
import StatusIndicator from './components/StatusIndicator'
import SubtitleBar from './components/SubtitleBar'
import ReminderScreen from './components/ReminderScreen'
import CognitiveGame from './components/CognitiveGame'
import StretchingGuide from './components/StretchingGuide'
import MediaBridge from './components/MediaBridge'
import { seniorWsUrl } from './utils/host'
import { getDeviceId } from './utils/deviceId'
import { useVoiceClient } from './audio/useVoiceClient'
import { useRealtimeClient } from './audio/useRealtimeClient'
import './App.css'

// 이 태블릿의 고유 device_id 로 서버에 연결 (멀티기기: 한 백엔드가 기기별 독립 처리)
const WS_URL = seniorWsUrl(`/ws/${getDeviceId()}`)
const TRANSITION_MS = 500
const PIN_VOICE_PANEL = true  // 시연: 음성 패널 항상 고정 표시(마이크/듣는 상태 가시화)

function App() {
  const [status, setStatus] = useState('idle')
  const [visibleStatus, setVisibleStatus] = useState('idle')
  const [phase, setPhase] = useState('idle')
  const [connected, setConnected] = useState(false)
  const [subtitle, setSubtitle] = useState('')
  const [userText, setUserText] = useState('')
  const [isListening, setIsListening] = useState(false)
  const [isPillTaken, setIsPillTaken] = useState(false)
  const [newPhotoUrl, setNewPhotoUrl] = useState(null)
  const [isEmergency, setIsEmergency] = useState(false)
  const [pairing, setPairing] = useState(null)
  const [isConversationActive, setIsConversationActive] = useState(false)
  const [activeReminder, setActiveReminder] = useState(null)
  const [reminderExiting, setReminderExiting] = useState(false)
  const [activity, setActivity] = useState(null)  // 'cognitive_game' | 'stretching' | null
  const transitionTimer = useRef(null)
  const reminderExitTimer = useRef(null)

  // 캔드 음원(pill_remind/greet 등) 재생 전용 — 마이크는 Realtime이 전담하므로 playbackOnly.
  useVoiceClient(pairing?.is_paired === true, { playbackOnly: true })
  // 대화 음성 = OpenAI Realtime (마이크 버튼으로 시작/정지). 7살 손주 페르소나+실데이터 도구는 토큰 세션에 포함.
  const rt = useRealtimeClient()

  // 화면 전환 애니메이션
  useEffect(() => {
    if (status === visibleStatus) return
    clearTimeout(transitionTimer.current)
    setPhase('exit')
    transitionTimer.current = setTimeout(() => {
      setVisibleStatus(status)
      setPhase('enter')
      transitionTimer.current = setTimeout(() => setPhase('idle'), TRANSITION_MS)
    }, TRANSITION_MS)
    if (status === 'idle') {
      setNewPhotoUrl(null)
      setIsEmergency(false)
    }
    return () => clearTimeout(transitionTimer.current)
  }, [status]) // eslint-disable-line react-hooks/exhaustive-deps

  // WebSocket 연결
  useEffect(() => {
    let ws
    let reconnectTimer

    function connect() {
      ws = new WebSocket(WS_URL)
      ws.onopen = () => setConnected(true)
      ws.onmessage = (event) => {
        let data
        try { data = JSON.parse(event.data) }
        catch { return }

        if (data.type === 'reminder') {
          setActiveReminder({
            reminderType: data.reminderType,
            title: data.title,
            message: data.message,
            time: data.time,
          })
          return
        }

        // pairing 키가 들어오면 (type==='pairing' 이든 초기 메시지든) 동일값 가드로 update
        if (data.pairing) {
          setPairing((prev) => {
            const next = data.pairing
            if (prev
                && prev.is_paired === next.is_paired
                && prev.pairing_code === next.pairing_code
                && prev.device_id === next.device_id) {
              return prev  // 동일 reference 유지 → 불필요한 re-render 방지
            }
            return next
          })
        }
        if (data.type === 'pairing') return

        // photos 리스너: 보호자가 사진 업로드 시 {newPhotoUrl} 단독 메시지 (type 없음)
        if (data.newPhotoUrl) setNewPhotoUrl(data.newPhotoUrl)

        if (data.type === 'voice') {
          setSubtitle(data.subtitle || '')
          setUserText(data.userText || '')
          setIsListening(data.isListening || false)
          setIsPillTaken(data.isPillTaken || false)
          if (data.isEmergency) setIsEmergency(true)
          if (data.newPhotoUrl) setNewPhotoUrl(data.newPhotoUrl)
          if (data.isConversationActive !== undefined) setIsConversationActive(data.isConversationActive)
          if (data.activity) setActivity(data.activity)
          return
        }

        if (data.status) setStatus(data.status)
        if (data.activity !== undefined) setActivity(data.activity || null)
        if (data.subtitle !== undefined) setSubtitle(data.subtitle || '')
        if (data.isListening !== undefined) setIsListening(data.isListening || false)
        if (data.isPillTaken !== undefined) setIsPillTaken(data.isPillTaken || false)
        if (data.isConversationActive !== undefined) setIsConversationActive(data.isConversationActive || false)
        // pairing 키는 위의 동일값 가드 setPairing 에서 이미 처리됨 (catch-all 불필요).
      }
      ws.onclose = () => {
        setConnected(false)
        reconnectTimer = setTimeout(connect, 3000)
      }
      ws.onerror = () => ws.close()
    }

    connect()
    return () => { clearTimeout(reconnectTimer); if (ws) ws.close() }
  }, [])

  // 리마인더는 카운트다운으로만 자동 dismiss (voice 패널과 공존 가능)

  // 테스트용 키보드 단축키 (개발 환경에서만)
  useEffect(() => {
    function handleKey(e) {
      if (e.key === '1') setStatus('idle')
      if (e.key === '2') setStatus('greeting')
      if (e.key === '3') setStatus('active')
      if (e.key === 'c') setIsConversationActive((v) => !v)
      if (e.key === 'r') setActiveReminder({
        reminderType: 'pill', title: '약 드실 시간이에요!',
        message: '잊지 말고 꼭 챙겨 드세요!', time: '09:00',
      })
      if (e.key === 'l') { setIsListening(true); setSubtitle('네, 말씀하세요!'); setUserText('앨범아') }
      if (e.key === 'g') setActivity('cognitive_game')
      if (e.key === 's') setActivity('stretching')
      if (e.key === 'x') setActivity(null)
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [])

  function dismissReminder() {
    setReminderExiting(true)
    clearTimeout(reminderExitTimer.current)
    reminderExitTimer.current = setTimeout(() => {
      setActiveReminder(null)
      setReminderExiting(false)
    }, 400)
  }

  function renderBaseContent() {
    // 페어링 안 됐거나 연결 중이거나 idle 이면 IdleScreen 단일 분기 (React reconciliation 보장)
    const showIdle = !pairing || pairing.is_paired !== true || visibleStatus === 'idle'
    if (showIdle) return <IdleScreen pairing={pairing} />
    if (visibleStatus === 'greeting') return <GreetScreen />
    return <ActiveScreen newPhotoUrl={newPhotoUrl} />
  }

  // 응급 상황은 게임/스트레칭 중이라도 강제로 종료 — App 레벨에서 컴포넌트 언마운트
  useEffect(() => {
    if (isEmergency && activity) {
      console.log('[EMERGENCY] 게임/스트레칭 강제 종료')
      setActivity(null)
    }
  }, [isEmergency, activity])

  // 활동(게임/스트레칭)이 활성이고 응급이 아니면 메인 화면을 덮음
  if (activity === 'cognitive_game' && !isEmergency) {
    return <CognitiveGame onExit={() => setActivity(null)} />
  }
  if (activity === 'stretching' && !isEmergency) {
    return <StretchingGuide onExit={() => setActivity(null)} />
  }

  return (
    <div className="app">
      {/* 태블릿 카메라 → 백엔드(/ws/media/{device_id}) 송신. 페어링 후에만 마운트(카메라 권한 1회). */}
      {pairing?.is_paired === true && <MediaBridge />}

      {/* 메인 콘텐츠 — 음성 패널 고정 시 항상 오른쪽으로 밀림 */}
      <div className={`app-main ${(isConversationActive || PIN_VOICE_PANEL) ? 'app-main--pushed' : ''}`}>
        {/* 기본 화면 — 항상 마운트 상태 유지 */}
        <div key={visibleStatus} className={`screen-anim screen-anim--${phase}`}>
          {renderBaseContent()}
        </div>

        {/* 리마인더 — 기본 화면 위 오버레이 */}
        {activeReminder && (
          <div className="reminder-overlay">
            <ReminderScreen reminder={activeReminder} exiting={reminderExiting} onDismiss={dismissReminder} />
          </div>
        )}
      </div>

      {/* 음성 패널 — 시연용 항상 고정(PIN_VOICE_PANEL) */}
      <div className={`voice-side-panel ${(isConversationActive || PIN_VOICE_PANEL) ? 'voice-side-panel--open' : ''}`}>
        <div className="side-status">
          <StatusIndicator connected={connected} status={status} />
        </div>
        {isEmergency && (
          <div className="side-emergency">
            <span>🚨</span>
            <span>보호자에게 알림 전송됨</span>
          </div>
        )}
        <div className="side-voice-panel">
          <button
            onClick={() => (rt.active ? rt.stop() : rt.start())}
            disabled={rt.status === 'connecting'}
            style={{
              width: '100%', padding: '16px', fontSize: '20px', fontWeight: 700,
              borderRadius: '12px', border: 'none', marginBottom: '12px', cursor: 'pointer',
              color: '#fff', background: rt.active ? '#e11d48' : '#2563eb',
            }}
          >
            {rt.status === 'connecting' ? '연결 중…' : rt.active ? '● 대화 중 — 끝내기' : '🎤 대화하기'}
          </button>
          {rt.status === 'error' && (
            <div style={{ color: '#c00', fontSize: 13, marginBottom: 8 }}>연결 오류 — 버튼 다시 누르세요</div>
          )}
          <SubtitleBar subtitle={subtitle} userText={userText} isListening={isListening}
            isConversationActive={rt.active || isConversationActive} micOpen={rt.active} />
        </div>
      </div>
    </div>
  )
}

export default App
