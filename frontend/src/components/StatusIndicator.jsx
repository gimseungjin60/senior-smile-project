import { useState } from 'react'
import './StatusIndicator.css'

const VIDEO_URL = 'http://localhost:8000/video'

const STATUS_CONFIG = {
  idle:       { dot: 'dot--connected', label: '대기중' },
  greeting:   { dot: 'dot--detected',  label: '감지됨' },
  active:     { dot: 'dot--active',    label: '콘텐츠' },
  offline:    { dot: 'dot--disconnected', label: '연결 끊김' },
}

function StatusIndicator({ connected, status }) {
  const [showCamera, setShowCamera] = useState(false)
  const key = connected ? status : 'offline'
  const { dot, label } = STATUS_CONFIG[key] ?? STATUS_CONFIG.offline

  return (
    <div className="status-corner">
      {showCamera && (
        <div className="camera-preview">
          <img src={VIDEO_URL} alt="webcam feed" />
        </div>
      )}
      <div className="status-indicator" onClick={() => setShowCamera((v) => !v)}>
        <span className={`dot ${dot}`} />
        <span className="status-label">{label}</span>
        <span className="camera-toggle">{showCamera ? '📷 숨기기' : '📷 카메라'}</span>
      </div>
    </div>
  )
}

export default StatusIndicator
