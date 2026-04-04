import { useState, useEffect } from 'react'
import IdleScreen from './components/IdleScreen'
import GreetScreen from './components/GreetScreen'
import ActiveScreen from './components/ActiveScreen'
import StatusIndicator from './components/StatusIndicator'
import './App.css'

const WS_URL = 'ws://localhost:8000/ws'

function App() {
  const [status, setStatus] = useState('idle')
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    let ws
    let reconnectTimer

    function connect() {
      ws = new WebSocket(WS_URL)

      ws.onopen = () => setConnected(true)

      ws.onmessage = (event) => {
        const data = JSON.parse(event.data)
        setStatus(data.status)
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
    if (status === 'greeting') return <GreetScreen />
    if (status === 'active') return <ActiveScreen />
    return <IdleScreen />
  }

  return (
    <div className="app">
      <StatusIndicator connected={connected} status={status} />
      {renderScreen()}
    </div>
  )
}

export default App
