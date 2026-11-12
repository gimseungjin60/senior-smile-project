const STORAGE_KEY = 'senior_device_id'

function _generate() {
  return 'frame-' + Math.random().toString(16).slice(2, 10)
}

let _cached = null

export function getDeviceId() {
  if (_cached) return _cached
  try {
    // 시연/배포: URL ?did=frame-demo 로 device_id 고정 → 새로고침·재접속에도 같은 기기 유지
    // (랜덤 device_id churn 방지 → 페어링/사진/실시간카메라 라우팅 안정)
    const fromUrl = new URLSearchParams(window.location.search).get('did')
    if (fromUrl) {
      localStorage.setItem(STORAGE_KEY, fromUrl)
      _cached = fromUrl
      return fromUrl
    }
    let id = localStorage.getItem(STORAGE_KEY)
    if (!id) {
      id = _generate()
      localStorage.setItem(STORAGE_KEY, id)
    }
    _cached = id
    return id
  } catch {
    _cached = _generate()
    return _cached
  }
}
