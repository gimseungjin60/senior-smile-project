const STORAGE_KEY = 'senior_device_id'

function _generate() {
  return 'frame-' + Math.random().toString(16).slice(2, 10)
}

let _cached = null

export function getDeviceId() {
  if (_cached) return _cached
  try {
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
