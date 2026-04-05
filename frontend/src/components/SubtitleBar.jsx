import { useState, useEffect, useRef } from 'react'
import './SubtitleBar.css'

function SubtitleBar({ subtitle, isListening }) {
  const [displayText, setDisplayText] = useState('')
  const prevSubtitle = useRef('')
  const typingTimer = useRef(null)

  useEffect(() => {
    if (subtitle && subtitle !== prevSubtitle.current) {
      clearInterval(typingTimer.current)
      prevSubtitle.current = subtitle
      setDisplayText('')

      let i = 0
      typingTimer.current = setInterval(() => {
        i++
        setDisplayText(subtitle.slice(0, i))
        if (i >= subtitle.length) clearInterval(typingTimer.current)
      }, 35)
    }

    if (!subtitle && !isListening) {
      prevSubtitle.current = ''
      setDisplayText('')
    }

    return () => clearInterval(typingTimer.current)
  }, [subtitle, isListening])

  const isEmpty = !subtitle && !isListening && !displayText

  return (
    <div className={`voice-panel ${isEmpty ? 'voice-panel--idle' : 'voice-panel--active'}`}>
      {isListening && !subtitle ? (
        <div className="vp-listening">
          <span className="vp-listening-label">듣는 중</span>
          <div className="vp-dots">
            <span className="vp-dot" />
            <span className="vp-dot" />
            <span className="vp-dot" />
          </div>
        </div>
      ) : displayText ? (
        <div className="vp-speech">
          <div className="vp-speaker">
            <span className="vp-speaker-dot" />
            <span className="vp-speaker-label">AI 손주</span>
          </div>
          <p className="vp-text">{displayText}</p>
        </div>
      ) : (
        <div className="vp-idle">
          <span className="vp-idle-icon">💬</span>
          <span className="vp-idle-text">말씀해 주세요</span>
        </div>
      )}
    </div>
  )
}

export default SubtitleBar
