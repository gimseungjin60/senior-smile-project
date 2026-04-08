import { useState, useEffect } from 'react'
import './IdleScreen.css'

const WEATHER_MOCK = {
  temperature: '18°',
  condition: '맑음',
  icon: '☀️',
}

function formatTime(date) {
  return date.toLocaleTimeString('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function formatSeconds(date) {
  return date.toLocaleTimeString('ko-KR', {
    second: '2-digit',
  }).padStart(2, '0')
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
      <div className="idle-ambient" />
      <div className="idle-content">
        <div className="idle-time-block">
          <span className="clock">{formatTime(now)}</span>
          <span className="clock-seconds">{formatSeconds(now)}</span>
        </div>
        <div className="date">{formatDate(now)}</div>
        <div className="idle-weather-card">
          <span className="weather-icon">{WEATHER_MOCK.icon}</span>
          <span className="weather-temp">{WEATHER_MOCK.temperature}</span>
          <span className="weather-divider" />
          <span className="weather-condition">{WEATHER_MOCK.condition}</span>
        </div>
      </div>
    </div>
  )
}

export default IdleScreen
