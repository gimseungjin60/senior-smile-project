import { useState, useEffect, useCallback } from 'react'
import './ActiveScreen.css'

// 사진이 없을 때 기본 슬라이드
const DEFAULT_SLIDES = [
  { emoji: '👨‍👩‍👧‍👦', message: '가족들이 항상 응원하고 있어요' },
  { emoji: '🌸', message: '오늘도 건강하고 행복한 하루 되세요' },
  { emoji: '☕', message: '따뜻한 차 한 잔 어떠세요?' },
  { emoji: '🎵', message: '좋아하는 노래를 들으며 쉬어가세요' },
]

const BACKEND_URL = 'http://localhost:8001'
const PHOTO_POLL_INTERVAL = 60000 // 60초마다 새 사진 확인
const SLIDE_INTERVAL = 8000       // 8초마다 사진 전환

function ActiveScreen() {
  const [slideIndex, setSlideIndex] = useState(0)
  const [photos, setPhotos] = useState([])
  const [hasPhotos, setHasPhotos] = useState(false)

  // 백엔드에서 사진 목록 가져오기
  const fetchPhotos = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND_URL}/api/photos?limit=20`)
      if (!res.ok) return
      const data = await res.json()
      if (data.photos && data.photos.length > 0) {
        setPhotos(data.photos)
        setHasPhotos(true)
      }
    } catch {
      // 백엔드 연결 실패 시 기본 슬라이드 유지
    }
  }, [])

  // 최초 로드 + 주기적 폴링
  useEffect(() => {
    fetchPhotos()
    const pollTimer = setInterval(fetchPhotos, PHOTO_POLL_INTERVAL)
    return () => clearInterval(pollTimer)
  }, [fetchPhotos])

  // 슬라이드 자동 전환
  const totalSlides = hasPhotos ? photos.length : DEFAULT_SLIDES.length
  useEffect(() => {
    const timer = setInterval(() => {
      setSlideIndex((i) => (i + 1) % totalSlides)
    }, SLIDE_INTERVAL)
    return () => clearInterval(timer)
  }, [totalSlides])

  // 사진 모드
  if (hasPhotos) {
    const photo = photos[slideIndex % photos.length]
    return (
      <div className="active-screen active-screen--photo">
        <div className="photo-slide" key={photo.id}>
          <img
            className="photo-image"
            src={photo.uri}
            alt={photo.caption || '가족 사진'}
          />
          <div className="photo-overlay">
            <div className="photo-info">
              <span className="photo-uploader">
                {photo.emoji} {photo.uploaderName}
              </span>
              {photo.caption && (
                <p className="photo-caption">{photo.caption}</p>
              )}
            </div>
            <div className="slide-dots">
              {photos.map((_, i) => (
                <span
                  key={i}
                  className={`dot ${i === slideIndex % photos.length ? 'dot--active' : ''}`}
                />
              ))}
            </div>
          </div>
        </div>
      </div>
    )
  }

  // 기본 슬라이드 모드 (사진 없을 때)
  const slide = DEFAULT_SLIDES[slideIndex % DEFAULT_SLIDES.length]
  return (
    <div className="active-screen">
      <div className="active-content" key={slideIndex}>
        <div className="active-emoji">{slide.emoji}</div>
        <p className="active-message">{slide.message}</p>
        <div className="slide-dots">
          {DEFAULT_SLIDES.map((_, i) => (
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
