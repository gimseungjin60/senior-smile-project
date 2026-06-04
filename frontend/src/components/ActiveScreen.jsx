import { useState, useEffect, useRef, useCallback } from 'react'
import { seniorHttpUrl } from '../utils/host'
import { getDeviceId } from '../utils/deviceId'
import './ActiveScreen.css'

const DEFAULT_SLIDES = [
  { emoji: '🌸', message: '봄나들이' },
  { emoji: '👨‍👩‍👧‍👦', message: '가족들이 항상 응원하고 있어요' },
  { emoji: '☕', message: '따뜻한 차 한 잔 어떠세요?' },
  { emoji: '🎵', message: '좋아하는 노래를 들으며 쉬어가세요' },
]

const BACKEND_URL = seniorHttpUrl()
const PHOTO_POLL_INTERVAL = 60000
const SLIDE_INTERVAL = 8000
const CROSSFADE_MS = 700

// 원격 사진(Firebase Storage http URL)은 백엔드 프록시 경유 → 아이폰 HEIC를 JPEG로 변환받음.
// 백엔드 로컬 상대경로(/api/photos/...)는 그대로 사용.
function imgSrc(uri) {
  if (!uri) return uri
  if (uri.startsWith('http')) {
    return `${BACKEND_URL}/api/photo-proxy?url=${encodeURIComponent(uri)}`
  }
  return uri
}

function ActiveScreen({ newPhotoUrl }) {
  const [photos, setPhotos] = useState([])
  const [hasPhotos, setHasPhotos] = useState(false)
  const [loading, setLoading] = useState(true)  // 첫 사진 fetch 완료 전 로딩 표시

  const [curIdx, setCurIdx] = useState(0)
  const [prevIdx, setPrevIdx] = useState(null)
  const [crossfading, setCrossfading] = useState(false)

  const [slideIndex, setSlideIndex] = useState(0)

  const pendingPhotoRef = useRef(null)
  const curIdxRef = useRef(0)
  curIdxRef.current = curIdx

  // 새 사진 도착 → photos 배열에 추가
  useEffect(() => {
    if (!newPhotoUrl) return
    pendingPhotoRef.current = newPhotoUrl
    setPhotos((prev) => {
      if (prev.some((p) => p.uri === newPhotoUrl)) return prev
      return [...prev, { uri: newPhotoUrl, uploaderName: '가족', emoji: '📸', caption: '' }]
    })
    setHasPhotos(true)
  }, [newPhotoUrl])

  // photos 배열 업데이트 후 pending 사진으로 바로 전환
  useEffect(() => {
    const url = pendingPhotoRef.current
    if (!url) return
    const idx = photos.findIndex((p) => p.uri === url)
    if (idx === -1) return
    pendingPhotoRef.current = null
    setPrevIdx(curIdxRef.current)
    setCrossfading(true)
    setCurIdx(idx)
  }, [photos])

  const fetchPhotos = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND_URL}/api/photos?device_id=${getDeviceId()}&limit=20`)
      if (!res.ok) return
      const data = await res.json()
      if (data.photos?.length > 0) {
        setPhotos(data.photos)
        setHasPhotos(true)
      }
    } catch { /* 기본 슬라이드 유지 */ }
    finally { setLoading(false) }  // 성공/실패/0개 모두 로딩 종료
  }, [])

  useEffect(() => {
    fetchPhotos()
    const pollTimer = setInterval(fetchPhotos, PHOTO_POLL_INTERVAL)
    return () => clearInterval(pollTimer)
  }, [fetchPhotos])

  // 모든 사진을 백그라운드 프리로드 → 슬라이드 전환 시 즉시 표시 (이전 사진 잔류 방지)
  useEffect(() => {
    photos.forEach((p) => {
      if (!p?.uri) return
      const img = new Image()
      img.src = imgSrc(p.uri)
    })
  }, [photos])

  useEffect(() => {
    if (!hasPhotos || photos.length <= 1) return
    const timer = setInterval(() => {
      setCurIdx((cur) => {
        const next = (cur + 1) % photos.length
        setPrevIdx(cur)
        setCrossfading(true)
        return next
      })
    }, SLIDE_INTERVAL)
    return () => clearInterval(timer)
  }, [hasPhotos, photos.length])

  useEffect(() => {
    if (!crossfading) return
    const t = setTimeout(() => {
      setPrevIdx(null)
      setCrossfading(false)
    }, CROSSFADE_MS)
    return () => clearTimeout(t)
  }, [crossfading, curIdx])

  useEffect(() => {
    if (hasPhotos) return
    const timer = setInterval(() => {
      setSlideIndex((i) => (i + 1) % DEFAULT_SLIDES.length)
    }, SLIDE_INTERVAL)
    return () => clearInterval(timer)
  }, [hasPhotos])

  // 사진 슬라이드쇼
  if (hasPhotos && photos.length > 0) {
    const photo = photos[curIdx % photos.length]
    const prevPhoto = prevIdx !== null ? photos[prevIdx % photos.length] : null
    return (
      <div className="active-screen">
        <div className="photo-card-layout">
          <div className="photo-card">
            {prevPhoto && (
              <div className="photo-layer" style={{ zIndex: 1 }}>
                <img className="photo-image" src={imgSrc(prevPhoto.uri)} alt="" aria-hidden="true" />
              </div>
            )}
            <div
              className={`photo-layer ${crossfading ? 'photo-layer--entering' : ''}`}
              style={{ zIndex: 2 }}
            >
              <img className="photo-image" src={imgSrc(photo.uri)} alt={photo.caption || '가족 사진'} />
            </div>
          </div>
          <div className="photo-info-bar">
            <div className="photo-info">
              <span className="photo-uploader">{photo.emoji} {photo.uploaderName}</span>
              {photo.caption && <p className="photo-caption">{photo.caption}</p>}
            </div>
            <div className="slide-dots">
              {photos.map((_, i) => (
                <span key={i} className={`dot ${i === curIdx % photos.length ? 'dot--active' : ''}`} />
              ))}
            </div>
          </div>
        </div>
      </div>
    )
  }

  // 첫 사진 로딩 중 — dot 3개 애니메이션 (기본 슬라이드 깜빡임 방지)
  if (loading) {
    return (
      <div className="active-screen">
        <div className="photo-loading" role="status" aria-label="사진 불러오는 중">
          <span className="loading-dot" />
          <span className="loading-dot" />
          <span className="loading-dot" />
        </div>
      </div>
    )
  }

  // 기본 슬라이드
  const slide = DEFAULT_SLIDES[slideIndex % DEFAULT_SLIDES.length]
  return (
    <div className="active-screen">
      <div className="active-content" key={slideIndex}>
        <div className="active-emoji">{slide.emoji}</div>
        <p className="active-message">{slide.message}</p>
      </div>
      <div className="slide-dots slide-dots--center">
        {DEFAULT_SLIDES.map((_, i) => (
          <span key={i} className={`dot ${i === slideIndex % DEFAULT_SLIDES.length ? 'dot--active' : ''}`} />
        ))}
      </div>
    </div>
  )
}

export default ActiveScreen
