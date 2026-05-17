import { useEffect, useRef, useState } from 'react'
import { seniorWsUrl } from '../utils/host'

const WS_URL = seniorWsUrl('/ws/media')

// 백엔드 main.py 의 MEDIA_TYPE_* 와 동일
const MEDIA_TYPE_VIDEO = 0x00
const MEDIA_TYPE_AUDIO_OUT = 0x01
// const MEDIA_TYPE_AUDIO_IN = 0x02  // Phase 3 에서 사용

const FRAME_INTERVAL_MS = 200
const FRAME_WIDTH = 640
const FRAME_HEIGHT = 480
const JPEG_QUALITY = 0.7

/**
 * 태블릿 카메라/마이크/스피커를 백엔드로 brigde.
 * - 카메라: getUserMedia(video) → canvas 캡처 → JPEG → WS binary 전송
 * - 스피커: WS 에서 받은 audio (TTS) → <audio> 재생
 * - 마이크: Phase 3 에서 추가
 */
export default function MediaBridge() {
  const videoRef = useRef(null)
  const canvasRef = useRef(null)
  const wsRef = useRef(null)
  const streamRef = useRef(null)
  const [error, setError] = useState(null)
  const [wsConnected, setWsConnected] = useState(false)

  // 카메라 시작
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video: { width: { ideal: FRAME_WIDTH }, height: { ideal: FRAME_HEIGHT } },
          audio: false,
        })
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop())
          return
        }
        streamRef.current = stream
        if (videoRef.current) {
          videoRef.current.srcObject = stream
        }
      } catch (e) {
        setError(`카메라 권한 거부 또는 접근 실패: ${e?.message || e}`)
      }
    })()
    return () => {
      cancelled = true
      if (streamRef.current) {
        streamRef.current.getTracks().forEach((t) => t.stop())
        streamRef.current = null
      }
    }
  }, [])

  // WebSocket 연결 + 자동 재연결
  useEffect(() => {
    let reconnectTimer
    let ws

    function connect() {
      ws = new WebSocket(WS_URL)
      ws.binaryType = 'arraybuffer'
      ws.onopen = () => {
        wsRef.current = ws
        setWsConnected(true)
      }
      ws.onmessage = (event) => {
        if (!(event.data instanceof ArrayBuffer)) return
        const arr = new Uint8Array(event.data)
        if (arr.length < 2) return
        const mtype = arr[0]
        const payload = arr.subarray(1)
        if (mtype === MEDIA_TYPE_AUDIO_OUT) {
          // TTS audio 재생
          const blob = new Blob([payload], { type: 'audio/mpeg' })
          const url = URL.createObjectURL(blob)
          const audio = new Audio(url)
          audio.onended = () => URL.revokeObjectURL(url)
          audio.play().catch((e) => console.warn('[MediaBridge] audio 재생 실패:', e))
        }
      }
      ws.onclose = () => {
        wsRef.current = null
        setWsConnected(false)
        reconnectTimer = setTimeout(connect, 3000)
      }
      ws.onerror = () => ws.close()
    }

    connect()
    return () => {
      clearTimeout(reconnectTimer)
      if (ws) ws.close()
    }
  }, [])

  // 카메라 frame 캡처 + WebSocket binary 전송 (200ms 주기)
  useEffect(() => {
    if (!canvasRef.current) {
      canvasRef.current = document.createElement('canvas')
      canvasRef.current.width = FRAME_WIDTH
      canvasRef.current.height = FRAME_HEIGHT
    }
    const ctx = canvasRef.current.getContext('2d')

    const id = setInterval(async () => {
      const video = videoRef.current
      const ws = wsRef.current
      if (!video || video.readyState < 2 || !ws || ws.readyState !== WebSocket.OPEN) return
      try {
        ctx.drawImage(video, 0, 0, FRAME_WIDTH, FRAME_HEIGHT)
        const blob = await new Promise((resolve) =>
          canvasRef.current.toBlob(resolve, 'image/jpeg', JPEG_QUALITY)
        )
        if (!blob) return
        const buf = await blob.arrayBuffer()
        const out = new Uint8Array(buf.byteLength + 1)
        out[0] = MEDIA_TYPE_VIDEO
        out.set(new Uint8Array(buf), 1)
        ws.send(out)
      } catch (e) {
        console.warn('[MediaBridge] frame 전송 실패:', e)
      }
    }, FRAME_INTERVAL_MS)

    return () => clearInterval(id)
  }, [])

  return (
    <div style={{ position: 'fixed', bottom: 8, right: 8, zIndex: 1000 }}>
      {error && (
        <div style={{ background: '#7f1d1d', color: '#fff', padding: '6px 10px', borderRadius: 6, fontSize: 12, maxWidth: 280 }}>
          {error}
        </div>
      )}
      {!error && !wsConnected && (
        <div style={{ background: 'rgba(0,0,0,0.6)', color: '#fff', padding: '6px 10px', borderRadius: 6, fontSize: 12 }}>
          백엔드 연결 중...
        </div>
      )}
      {/* 카메라 미리보기 — 작게 (디버그 용) */}
      <video
        ref={videoRef}
        autoPlay
        playsInline
        muted
        style={{ display: 'none' }}
      />
    </div>
  )
}
