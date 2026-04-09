import { useState, useEffect, useRef } from 'react'
import IdleScreen from './components/IdleScreen'
import GreetScreen from './components/GreetScreen'
import ActiveScreen from './components/ActiveScreen'
import StatusIndicator from './components/StatusIndicator'
import SubtitleBar from './components/SubtitleBar'
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
  const transitionTimer = useRef(null)

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

        if (data.type === 'voice') {
          setSubtitle(data.subtitle || '')
          setUserText(data.userText || '')
          setIsListening(data.isListening || false)
          setIsPillTaken(data.isPillTaken || false)
          if (data.isEmergency) setIsEmergency(true)
          if (data.newPhotoUrl) setNewPhotoUrl(data.newPhotoUrl)
          return
        }
        if (data.status) setStatus(data.status)
        if (data.subtitle !== undefined) setSubtitle(data.subtitle || '')
        if (data.isListening !== undefined) setIsListening(data.isListening || false)
        if (data.isPillTaken !== undefined) setIsPillTaken(data.isPillTaken || false)
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

  // idle 화면: 시간/날씨만, 분할 없음
  const isIdle = visibleStatus === 'idle'
  // greeting/active 화면: 좌우 분할
  const isActive = visibleStatus === 'greeting' || visibleStatus === 'active'

  return (
    <div className="app">
      {isIdle && (
        <div className={`screen-wrap screen-wrap--${phase}`}>
          <IdleScreen pairing={pairing} />
        </div>
      )}

      {isActive && (
        <div className={`app-split screen-wrap--${phase}`}>
          {/* 왼쪽: 콘텐츠 영역 */}
          <div className="split-main">
            {visibleStatus === 'greeting'
              ? <GreetScreen />
              : <ActiveScreen newPhotoUrl={newPhotoUrl} />
            }
          </div>

          {/* 오른쪽: 음성 대화 영역 */}
          <div className="split-side">
            <StatusIndicator connected={connected} status={status} />

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
      )}
    </div>
  )
}

export default App
