import { useState, useEffect, useRef } from 'react'
import './SubtitleBar.css'

function SubtitleBar({ subtitle, userText, isListening }) {
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

  const isProcessing = (isListening || userText) && !subtitle
  const isEmpty = !subtitle && !isListening && !userText && !displayText

  return (
    <div className={`voice-panel ${isEmpty ? 'voice-panel--idle' : 'voice-panel--active'}`} style={{ flexDirection: 'column', alignItems: 'flex-start', padding: (userText || displayText) ? '2rem' : '1.5rem' }}>
      
      {/* 1. 사용자 텍스트 영역 (팝업처럼 위에 존재) */}
      {userText && (
        <div className="vp-user-text" style={{ marginBottom: (displayText || isProcessing) ? '1.5rem' : '0', width: '100%' }}>
          <span className="vp-user-text-label">👤 내 말 : </span>
          {userText}
        </div>
      )}

      {/* 2. AI 상태 영역 (듣는 중, 생각하는 중, 혹은 답변) */}
      {isProcessing ? (
        <div className="vp-listening" style={{ padding: '0' }}>
          <span className="vp-listening-label">{isListening ? '듣는 중' : '생각하는 중...'}</span>
          <div className="vp-dots">
            <span className="vp-dot" />
            <span className="vp-dot" />
            <span className="vp-dot" />
          </div>
        </div>
      ) : displayText ? (
        <div className="vp-speech" style={{ padding: '0' }}>
          <div className="vp-speaker">
            <span className="vp-speaker-dot" />
            <span className="vp-speaker-label">AI 손주</span>
          </div>
          <p className="vp-text">{displayText}</p>
        </div>
      ) : !userText && (
        <div className="vp-idle" style={{ width: '100%', alignItems: 'center' }}>
          <span className="vp-idle-icon">💬</span>
          <span className="vp-idle-text">말씀해 주세요</span>
        </div>
      )}
    </div>
  )
}

export default SubtitleBar
