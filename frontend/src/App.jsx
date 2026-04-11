import { useState, useEffect, useRef } from 'react'
import IdleScreen from './components/IdleScreen'
import GreetScreen from './components/GreetScreen'
import ActiveScreen from './components/ActiveScreen'
import StatusIndicator from './components/StatusIndicator'
import SubtitleBar from './components/SubtitleBar'
import ReminderScreen from './components/ReminderScreen'
import './App.css'

const WS_URL = 'ws://localhost:8000/ws'
const TRANSITION_MS = 500

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
  const transitionTimer = useRef(null)
  const reminderExitTimer = useRef(null)

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

        if (data.type === 'voice') {
          setSubtitle(data.subtitle || '')
          setUserText(data.userText || '')
          setIsListening(data.isListening || false)
          setIsPillTaken(data.isPillTaken || false)
          if (data.isEmergency) setIsEmergency(true)
          if (data.newPhotoUrl) setNewPhotoUrl(data.newPhotoUrl)
          if (data.isConversationActive !== undefined) setIsConversationActive(data.isConversationActive)
          return
        }

        if (data.status) setStatus(data.status)
        if (data.subtitle !== undefined) setSubtitle(data.subtitle || '')
        if (data.isListening !== undefined) setIsListening(data.isListening || false)
        if (data.isPillTaken !== undefined) setIsPillTaken(data.isPillTaken || false)
        if (data.isConversationActive !== undefined) setIsConversationActive(data.isConversationActive || false)
        if (data.pairing) setPairing(data.pairing)
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
    if (visibleStatus === 'idle') return <IdleScreen pairing={pairing} />
    if (visibleStatus === 'greeting') return <GreetScreen />
    return <ActiveScreen newPhotoUrl={newPhotoUrl} />
  }

  return (
    <div className="app">
      {/* 메인 콘텐츠 — 호출어 인식 시 오른쪽으로 밀림 */}
      <div className={`app-main ${isConversationActive ? 'app-main--pushed' : ''}`}>
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

      {/* 음성 패널 — 호출어 인식 시 오른쪽에서 슬라이드인 */}
      <div className={`voice-side-panel ${isConversationActive ? 'voice-side-panel--open' : ''}`}>
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
          <SubtitleBar subtitle={subtitle} userText={userText} isListening={isListening} />
        </div>
      </div>
    </div>
  )
}

export default App
