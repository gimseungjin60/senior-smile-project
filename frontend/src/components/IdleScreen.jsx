import { useState, useEffect } from 'react'
import './IdleScreen.css'

const WEATHER_MOCK = {
  temperature: '18°C',
  condition: '맑음',
  icon: '☀️',
}

function formatTime(date) {
  return date.toLocaleTimeString('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function formatDate(date) {
  return date.toLocaleDateString('ko-KR', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    weekday: 'long',
  })
}

function IdleScreen() {
  const [now, setNow] = useState(new Date())

  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(timer)
  }, [])

  return (
    <div className="idle-screen">
      <div className="idle-content">
        <div className="clock">{formatTime(now)}</div>
        <div className="date">{formatDate(now)}</div>
        <div className="divider" />
        <div className="weather">
          <span className="weather-icon">{WEATHER_MOCK.icon}</span>
          <span className="weather-temp">{WEATHER_MOCK.temperature}</span>
          <span className="weather-condition">{WEATHER_MOCK.condition}</span>
        </div>
      </div>
    </div>
  )
}

export default IdleScreen
