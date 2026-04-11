import { useEffect, useRef, useState } from 'react'
import './ReminderScreen.css'

const DISMISS_SECONDS = 15

const REMINDER_CONFIG = {
  pill:     { accent: '#3182F6', label: '복약 알림' },
  morning:  { accent: '#3182F6', label: '아침 루틴' },
  lunch:    { accent: '#00C471', label: '점심 시간' },
  activity: { accent: '#FF8B00', label: '활동 시간' },
  dinner:   { accent: '#9B72CF', label: '저녁 시간' },
  night:    { accent: '#4E5968', label: '취침 준비' },
}

function ReminderScreen({ reminder, exiting, onDismiss }) {
  const [remaining, setRemaining] = useState(DISMISS_SECONDS)
  const dismissTimerRef = useRef(null)
  const config = REMINDER_CONFIG[reminder.reminderType] || REMINDER_CONFIG.pill

  useEffect(() => {
    setRemaining(DISMISS_SECONDS)
    clearTimeout(dismissTimerRef.current)
    const interval = setInterval(() => {
      setRemaining((s) => {
        if (s <= 1) {
          clearInterval(interval)
          // 바가 0%까지 애니메이션 완료 후 dismiss (CSS transition: 1s 에 맞춤)
          dismissTimerRef.current = setTimeout(onDismiss, 1000)
          return 0
        }
        return s - 1
      })
    }, 1000)
    return () => {
      clearInterval(interval)
      clearTimeout(dismissTimerRef.current)
    }
  }, [reminder]) // eslint-disable-line react-hooks/exhaustive-deps

  const progress = (remaining / DISMISS_SECONDS) * 100

  return (
    <div
      className={`reminder-screen ${exiting ? 'reminder-screen--exiting' : ''}`}
      style={{ '--r-accent': config.accent }}
    >
      <div className="reminder-glow reminder-glow--top" />
      <div className="reminder-glow reminder-glow--bottom" />

      <div className="reminder-body">
        <p className="reminder-label">{config.label}</p>
        <h1 className="reminder-title">{reminder.title}</h1>
        {reminder.message && (
          <p className="reminder-message">{reminder.message}</p>
        )}
        {reminder.time && (
          <p className="reminder-time">{reminder.time}</p>
        )}
      </div>

      <div className="reminder-footer">
        <div className="reminder-bar-track">
          <div
            className="reminder-bar-fill"
            style={{ width: `${progress}%` }}
          />
        </div>
        <p className="reminder-countdown">{remaining > 0 ? `${remaining}초 후 사라집니다` : '잠시 후 사라집니다'}</p>
      </div>
    </div>
  )
}

export default ReminderScreen
