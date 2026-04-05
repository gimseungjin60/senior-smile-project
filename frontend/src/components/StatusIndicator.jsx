import { useState } from 'react'
import './StatusIndicator.css'

const VIDEO_URL = 'http://localhost:8000/video'

const STATUS_CONFIG = {
  idle:       { dot: 'si-dot--idle',    label: '대기중' },
  greeting:   { dot: 'si-dot--detect',  label: '감지됨' },
  active:     { dot: 'si-dot--active',  label: '콘텐츠' },
  offline:    { dot: 'si-dot--offline', label: '연결 끊김' },
}

function StatusIndicator({ connected, status }) {
  const [showCamera, setShowCamera] = useState(false)
  const key = connected ? status : 'offline'
  const { dot, label } = STATUS_CONFIG[key] ?? STATUS_CONFIG.offline

  return (
    <div className="si-corner">
      {showCamera && (
        <div className="si-camera">
          <img src={VIDEO_URL} alt="webcam feed" />
        </div>
      )}
      <button className="si-pill" onClick={() => setShowCamera((v) => !v)}>
        <span className={`si-dot ${dot}`} />
        <span className="si-label">{label}</span>
        <span className="si-cam-label">{showCamera ? '숨기기' : '카메라'}</span>
      </button>
    </div>
  )
}

export default StatusIndicator
