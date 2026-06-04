import { useEffect, useRef, useState } from 'react'
import { seniorWsUrl } from '../utils/host'
import { getDeviceId } from '../utils/deviceId'

const WS_URL = seniorWsUrl(`/ws/voice/${getDeviceId()}`)
const VAD_VERSION = '0.0.30'
const ORT_VERSION = '1.26.0'
const RESUME_GRACE_MS = 500

export function useVoiceClient(enabled) {
  const [connected, setConnected] = useState(false)

  const wsRef = useRef(null)
  const vadRef = useRef(null)
  const reconnectTimerRef = useRef(null)
  const resumeTimerRef = useRef(null)
  const playQueueRef = useRef([])
  const playingRef = useRef(false)
  const audioElRef = useRef(null)
  const pausedRef = useRef(false)

  useEffect(() => {
    if (!enabled) return
    let cancelled = false

    const sendControl = (action) => {
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'control', action }))
      }
    }

    const pauseCapture = () => {
      clearTimeout(resumeTimerRef.current)
      pausedRef.current = true
      try { vadRef.current?.pause() } catch { /* VAD 미초기화 무시 */ }
    }

    const scheduleResume = () => {
      clearTimeout(resumeTimerRef.current)
      resumeTimerRef.current = setTimeout(() => {
        if (!playingRef.current && playQueueRef.current.length === 0) {
          pausedRef.current = false
          try { vadRef.current?.start() } catch { /* 무시 */ }
        }
      }, RESUME_GRACE_MS)
    }

    const playNext = () => {
      const queue = playQueueRef.current
      if (queue.length === 0) {
        playingRef.current = false
        scheduleResume()
        return
      }
      playingRef.current = true
      const item = queue.shift()
      const sep = item.url.includes('?') ? '&' : '?'
      const audio = new Audio(item.url + sep + 'ts=' + (item.ts || Date.now()))
      audioElRef.current = audio
      const onDone = () => {
        sendControl('playback_done')
        playNext()
      }
      audio.onended = onDone
      audio.onerror = onDone
      audio.play().catch(onDone)
    }

    const enqueuePlay = (item) => {
      pauseCapture()
      playQueueRef.current.push(item)
      if (!playingRef.current) playNext()
    }

    const connect = () => {
      const ws = new WebSocket(WS_URL)
      ws.binaryType = 'arraybuffer'
      wsRef.current = ws
      ws.onopen = () => { if (!cancelled) setConnected(true) }
      ws.onmessage = (e) => {
        let data
        try { data = JSON.parse(e.data) } catch { return }
        if (data.type === 'speak' || data.type === 'beep') {
          enqueuePlay({ url: data.url, ts: data.ts })
        }
      }
      ws.onclose = () => {
        if (cancelled) return
        setConnected(false)
        reconnectTimerRef.current = setTimeout(connect, 3000)
      }
      ws.onerror = () => { try { ws.close() } catch { /* 무시 */ } }
    }
    connect()

    // AGC·노이즈 억제 활성화 스트림 — 원거리(액자형) 수음 개선
    const micStreamPromise = navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
        sampleRate: 16000,
      },
    }).catch(() => undefined)  // 실패 시 VAD 내부 기본 스트림 사용

    import('@ricky0123/vad-web')
      .then(async ({ MicVAD, utils }) => {
        const stream = await micStreamPromise
        return MicVAD.new({
          baseAssetPath: `https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@${VAD_VERSION}/dist/`,
          onnxWASMBasePath: `https://cdn.jsdelivr.net/npm/onnxruntime-web@${ORT_VERSION}/dist/`,
          ...(stream ? { stream } : {}),
          redemptionFrames: 14,
          minSpeechFrames: 6,            // 순간 잡음(딸깍/기침) 오탐 차단. "앨범아"(~0.5s)는 충분히 넘김
          positiveSpeechThreshold: 0.55, // 원거리 수음(AGC)과 무음 오탐의 절충
          negativeSpeechThreshold: 0.35,
          onSpeechEnd: (audio) => {
            if (cancelled || pausedRef.current || playingRef.current) return
            const ws = wsRef.current
            if (!ws || ws.readyState !== WebSocket.OPEN) return
            ws.send(utils.encodeWAV(audio))
          },
        })
      })
      .then((vad) => {
        if (cancelled) { try { vad.destroy() } catch { /* 무시 */ } return }
        vadRef.current = vad
        if (!pausedRef.current) vad.start()
      })
      .catch((err) => {
        console.warn('[voice] VAD 초기화 실패 (마이크 권한/네트워크 확인):', err)
      })

    return () => {
      cancelled = true
      clearTimeout(resumeTimerRef.current)
      clearTimeout(reconnectTimerRef.current)
      try { vadRef.current?.destroy() } catch { /* 무시 */ }
      vadRef.current = null
      try { audioElRef.current?.pause() } catch { /* 무시 */ }
      try { wsRef.current?.close() } catch { /* 무시 */ }
      wsRef.current = null
    }
  }, [enabled])

  return { voiceConnected: connected }
}
