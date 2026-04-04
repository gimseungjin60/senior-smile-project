import { useState, useEffect } from 'react'
import './ActiveScreen.css'

// MVP: placeholder 슬라이드. 나중에 실제 가족 사진으로 교체
const SLIDES = [
  { emoji: '👨‍👩‍👧‍👦', message: '가족들이 항상 응원하고 있어요' },
  { emoji: '🌸', message: '오늘도 건강하고 행복한 하루 되세요' },
  { emoji: '☕', message: '따뜻한 차 한 잔 어떠세요?' },
  { emoji: '🎵', message: '좋아하는 노래를 들으며 쉬어가세요' },
]

function ActiveScreen() {
  const [slideIndex, setSlideIndex] = useState(0)

  useEffect(() => {
    const timer = setInterval(() => {
      setSlideIndex((i) => (i + 1) % SLIDES.length)
    }, 6000)
    return () => clearInterval(timer)
  }, [])

  const slide = SLIDES[slideIndex]

  return (
    <div className="active-screen">
      <div className="active-content" key={slideIndex}>
        <div className="active-emoji">{slide.emoji}</div>
        <p className="active-message">{slide.message}</p>
        <div className="slide-dots">
          {SLIDES.map((_, i) => (
            <span
              key={i}
              className={`dot ${i === slideIndex ? 'dot--active' : ''}`}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

export default ActiveScreen
