import { useState, useEffect, useCallback } from 'react'
import './IdleScreen.css'

const BACKEND_API = 'http://localhost:8000'
const WEATHER_POLL_INTERVAL = 600000 // 10분마다 날씨 갱신

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

function IdleScreen({ pairing }) {
  const [now, setNow] = useState(new Date())
  const [weather, setWeather] = useState({ temperature: '--', condition: '불러오는 중', icon: '🌤️' })
  const [isNight, setIsNight] = useState(false)
  const [pairingCode, setPairingCode] = useState(null)
  const [codeRemaining, setCodeRemaining] = useState(0)

  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(timer)
  }, [])

  // 날씨 가져오기
  useEffect(() => {
    async function fetchWeather() {
      try {
        const res = await fetch(`${BACKEND_API}/api/weather`)
        const data = await res.json()
        setWeather(data)
      } catch { /* 실패 시 기본값 유지 */ }
    }
    fetchWeather()
    const timer = setInterval(fetchWeather, WEATHER_POLL_INTERVAL)
    return () => clearInterval(timer)
  }, [])

  // 야간 모드 체크
  useEffect(() => {
    async function checkNight() {
      try {
        const res = await fetch(`${BACKEND_API}/api/nightmode`)
        const data = await res.json()
        setIsNight(data.is_night)
      } catch { /* 실패 시 기본값 */ }
    }
    checkNight()
    const timer = setInterval(checkNight, 60000) // 1분마다 체크
    return () => clearInterval(timer)
  }, [])

  // 페어링 코드 요청
  const requestCode = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND_API}/api/pairing/code`, { method: 'POST' })
      const data = await res.json()
      setPairingCode(data.code)
      setCodeRemaining(data.expires_in)
    } catch {
      console.warn('[Pairing] 코드 요청 실패')
    }
  }, [])

  // 코드 카운트다운 — codeRemaining을 dependency에서 제거하여 1회만 setInterval 생성
  useEffect(() => {
    if (!pairingCode) return
    const timer = setInterval(() => {
      setCodeRemaining((s) => {
        if (s <= 1) {
          clearInterval(timer)
          return 0
        }
        return s - 1
      })
    }, 1000)
    return () => clearInterval(timer)
  }, [pairingCode])

  // 미페어링 + 코드 없으면 자동 생성
  const isPairedVal = pairing?.is_paired
  useEffect(() => {
    if (isPairedVal === false && !pairingCode && codeRemaining <= 0) {
      requestCode()
    }
    // 페어링 완료 시 PIN 상태 즉시 초기화
    if (isPairedVal === true) {
      setPairingCode(null)
      setCodeRemaining(0)
    }
  }, [isPairedVal]) // eslint-disable-line react-hooks/exhaustive-deps

  const isPaired = pairing?.is_paired
  const showPairing = pairing && !isPaired

  return (
    <div className={`idle-screen ${isNight ? 'idle-screen--night' : ''}`}>
      <div className="idle-ambient" />
      <div className="idle-content">
        <div className="idle-time-block">
          <span className="clock">{formatTime(now)}</span>
          <span className="clock-seconds">{formatSeconds(now)}</span>
        </div>
        <div className="date">{formatDate(now)}</div>

        {showPairing ? (
          <div className="pairing-card">
            <p className="pairing-title">보호자 앱에서 아래 코드를 입력하세요</p>
            {pairingCode ? (
              <>
                <div className="pairing-code">
                  {pairingCode.split('').map((digit, i) => (
                    <span key={i} className="pairing-digit">{digit}</span>
                  ))}
                </div>
                <p className="pairing-timer">
                  {Math.floor(codeRemaining / 60)}:{String(codeRemaining % 60).padStart(2, '0')} 남음
                </p>
                {codeRemaining <= 0 && (
                  <button className="pairing-refresh" onClick={requestCode}>
                    새 코드 받기
                  </button>
                )}
              </>
            ) : (
              <button className="pairing-refresh" onClick={requestCode}>
                코드 생성하기
              </button>
            )}
            <p className="pairing-device-id">기기 ID: {pairing.device_id}</p>
          </div>
        ) : (
          <div className="idle-weather-card">
            <span className="weather-icon">{weather.icon}</span>
            <span className="weather-temp">{weather.temperature}</span>
            <span className="weather-divider" />
            <span className="weather-condition">{weather.condition}</span>
          </div>
        )}
      </div>
    </div>
  )
}

export default IdleScreen
