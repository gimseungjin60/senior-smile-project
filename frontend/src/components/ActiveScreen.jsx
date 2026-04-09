import { useState, useEffect, useCallback } from 'react'
import './ActiveScreen.css'

const DEFAULT_SLIDES = [
  { emoji: '🌸', message: '봄나들이' },
  { emoji: '👨‍👩‍👧‍👦', message: '가족들이 항상 응원하고 있어요' },
  { emoji: '☕', message: '따뜻한 차 한 잔 어떠세요?' },
  { emoji: '🎵', message: '좋아하는 노래를 들으며 쉬어가세요' },
]

const BACKEND_URL = 'http://localhost:8001'
const PHOTO_POLL_INTERVAL = 60000
const SLIDE_INTERVAL = 8000
const NEW_PHOTO_DISPLAY_TIME = 20000

function ActiveScreen({ newPhotoUrl }) {
  const [slideIndex, setSlideIndex] = useState(0)
  const [photos, setPhotos] = useState([])
  const [hasPhotos, setHasPhotos] = useState(false)
  const [showNewPhoto, setShowNewPhoto] = useState(false)
  const [displayedNewPhoto, setDisplayedNewPhoto] = useState(null)

  // 새 사진 도착 시 표시 + 자동 닫기
  useEffect(() => {
    if (newPhotoUrl) {
      setDisplayedNewPhoto(newPhotoUrl)
      setShowNewPhoto(true)
      const timer = setTimeout(() => setShowNewPhoto(false), NEW_PHOTO_DISPLAY_TIME)
      return () => clearTimeout(timer)
    }
  }, [newPhotoUrl])

  const fetchPhotos = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND_URL}/api/photos?limit=20`)
      if (!res.ok) return
      const data = await res.json()
      if (data.photos?.length > 0) {
        setPhotos(data.photos)
        setHasPhotos(true)
      }
    } catch { /* 기본 슬라이드 유지 */ }
  }, [])

  useEffect(() => {
    fetchPhotos()
    const pollTimer = setInterval(fetchPhotos, PHOTO_POLL_INTERVAL)
    return () => clearInterval(pollTimer)
  }, [fetchPhotos])

  const totalSlides = hasPhotos ? photos.length : DEFAULT_SLIDES.length
  useEffect(() => {
    if (showNewPhoto) return
    const timer = setInterval(() => {
      setSlideIndex((i) => (i + 1) % totalSlides)
    }, SLIDE_INTERVAL)
    return () => clearInterval(timer)
  }, [totalSlides, showNewPhoto])

  // 새 사진 전체화면
  if (showNewPhoto && displayedNewPhoto) {
    return (
      <div className="active-screen active-screen--photo">
        <div className="new-photo-display">
          <img className="photo-image" src={displayedNewPhoto} alt="새로 도착한 사진" />
          <div className="new-photo-badge">새 사진이 도착했어요!</div>
          <div className="new-photo-timer"><div className="new-photo-timer-bar" /></div>
        </div>
      </div>
    )
  }

  // 사진 모드
  if (hasPhotos) {
    const photo = photos[slideIndex % photos.length]
    return (
      <div className="active-screen active-screen--photo">
        <div className="photo-slide" key={photo.id}>
          <img className="photo-image" src={photo.uri} alt={photo.caption || '가족 사진'} />
          <div className="photo-overlay">
            <div className="photo-info">
              <span className="photo-uploader">{photo.emoji} {photo.uploaderName}</span>
              {photo.caption && <p className="photo-caption">{photo.caption}</p>}
            </div>
            <div className="slide-dots">
              {photos.map((_, i) => (
                <span key={i} className={`dot ${i === slideIndex % photos.length ? 'dot--active' : ''}`} />
              ))}
            </div>
          </div>
        </div>
      </div>
    )
  }

  // 기본 슬라이드 모드
  const slide = DEFAULT_SLIDES[slideIndex % DEFAULT_SLIDES.length]
  return (
    <div className="active-screen">
      <div className="active-content" key={slideIndex}>
        <div className="active-emoji">{slide.emoji}</div>
        <p className="active-message">{slide.message}</p>
      </div>
      <div className="slide-dots">
        {DEFAULT_SLIDES.map((_, i) => (
          <span key={i} className={`dot ${i === slideIndex % DEFAULT_SLIDES.length ? 'dot--active' : ''}`} />
        ))}
      </div>
    </div>
  )
}

export default ActiveScreen
