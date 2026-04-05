import { useState, useEffect } from 'react'
import SubtitleBar from './SubtitleBar'
import './ActiveScreen.css'

// 나중에 실제 가족 사진으로 교체
const SLIDES = [
  { emoji: '👨‍👩‍👧‍👦', label: '가족 사진' },
  { emoji: '🌸', label: '봄나들이' },
  { emoji: '☕', label: '오후 한때' },
  { emoji: '🎵', label: '즐거운 시간' },
]

function ActiveScreen({ subtitle, userText, isListening, isPillTaken, newPhotoUrl }) {
  const [slideIndex, setSlideIndex] = useState(0)

  useEffect(() => {
    const timer = setInterval(() => {
      setSlideIndex((i) => (i + 1) % SLIDES.length)
    }, 6000)
    return () => clearInterval(timer)
  }, [])

  return (
    <div className="active-screen">
      {/* 왼쪽: 사진 영역 */}
      <div className="active-photo-area" key={slideIndex}>
        <div className="active-photo-placeholder">
          <span className="photo-emoji">{SLIDES[slideIndex].emoji}</span>
          <span className="photo-label">{SLIDES[slideIndex].label}</span>
        </div>
        <div className="slide-progress">
          {SLIDES.map((_, i) => (
            <span
              key={i}
              className={`progress-dot ${i === slideIndex ? 'progress-dot--active' : ''}`}
            />
          ))}
        </div>
      </div>

      {/* 오른쪽: AI 대화 패널 */}
      <div className="active-voice-area">
        <SubtitleBar subtitle={subtitle} userText={userText} isListening={isListening} />
      </div>

      {/* 새 사진 오버레이 (전체 화면 덮기 또는 중앙 팝업) */}
      {newPhotoUrl && (
        <div className="new-photo-overlay">
          <img src={newPhotoUrl} alt="도착한 사진" className="new-photo-img" />
          <div className="new-photo-caption">손주 사진이 도착했습니다! 💝</div>
        </div>
      )}
    </div>
  )
}

export default ActiveScreen
