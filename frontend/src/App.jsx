import { useState, useEffect, useRef } from 'react'
import IdleScreen from './components/IdleScreen'
import GreetScreen from './components/GreetScreen'
import ActiveScreen from './components/ActiveScreen'
import StatusIndicator from './components/StatusIndicator'
import './App.css'

const WS_URL = 'ws://localhost:8000/ws'
const TRANSITION_MS = 500

function App() {
  const [status, setStatus] = useState('idle')
  const [visibleStatus, setVisibleStatus] = useState('idle') // 실제 렌더되는 화면
  const [phase, setPhase] = useState('idle') // 'idle' | 'exit' | 'enter'
  const [connected, setConnected] = useState(false)
  const [subtitle, setSubtitle] = useState('')
  const [userText, setUserText] = useState('')
  const [isListening, setIsListening] = useState(false)
  const [isPillTaken, setIsPillTaken] = useState(false)
  const [newPhotoUrl, setNewPhotoUrl] = useState(null)
  const transitionTimer = useRef(null)

  // status가 바뀌면 크로스페이드 전환 시작
  useEffect(() => {
    if (status === visibleStatus) return

    clearTimeout(transitionTimer.current)
    setPhase('exit')

    transitionTimer.current = setTimeout(() => {
      setVisibleStatus(status)
      setPhase('enter')

      transitionTimer.current = setTimeout(() => {
        setPhase('idle')
      }, TRANSITION_MS)
    }, TRANSITION_MS)

    if (status === 'idle') {
      setNewPhotoUrl(null)
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
        const data = JSON.parse(event.data)

        if (data.type === 'voice') {
          setSubtitle(data.subtitle || '')
          setUserText(data.userText || '')
          setIsListening(data.isListening || false)
          setIsPillTaken(data.isPillTaken || false)
          
          if (data.newPhotoUrl) {
            setNewPhotoUrl(data.newPhotoUrl)
          }
          return
        }

        setStatus(data.status)
        if (data.subtitle !== undefined) setSubtitle(data.subtitle || '')
        if (data.isListening !== undefined) setIsListening(data.isListening || false)
        if (data.isPillTaken !== undefined) setIsPillTaken(data.isPillTaken || false)
      }

      ws.onclose = () => {
        setConnected(false)
        reconnectTimer = setTimeout(connect, 3000)
      }

      ws.onerror = () => ws.close()
    }

    connect()

    return () => {
      clearTimeout(reconnectTimer)
      if (ws) ws.close()
    }
  }, [])

  function renderScreen() {
    const voiceProps = { subtitle, userText, isListening, isPillTaken, newPhotoUrl }
    if (visibleStatus === 'greeting') return <GreetScreen {...voiceProps} />
    if (visibleStatus === 'active') return <ActiveScreen {...voiceProps} />
    return <IdleScreen />
  }

  return (
    <div className="app">
      <StatusIndicator connected={connected} status={status} />
      <div className={`screen-wrap screen-wrap--${phase}`}>
        {renderScreen()}
      </div>
    </div>
  )
}

export default App
