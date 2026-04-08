import { useEffect, useState } from 'react'
import './UserSpeechBubble.css'

function UserSpeechBubble({ userText }) {
  const [isVisible, setIsVisible] = useState(false)

  useEffect(() => {
    if (userText) {
      setIsVisible(true)
    } else {
      setIsVisible(false)
    }
  }, [userText])

  if (!isVisible && !userText) return null

  return (
    <div className={`user-speech-wrap ${isVisible ? 'user-speech-wrap--visible' : ''}`}>
        <div className="user-speech-bubble">
            <span className="user-speech-label">👤 내 말:</span>
            <span className="user-speech-text">{userText}</span>
        </div>
    </div>
  )
}

export default UserSpeechBubble
