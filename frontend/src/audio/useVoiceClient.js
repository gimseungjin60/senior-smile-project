import { useEffect, useRef, useState } from 'react'
import { seniorWsUrl, seniorHttpUrl } from '../utils/host'
// @ricky0123/vad-web(+onnxruntime-web)은 무거워서(~400KB) 동적 import로 메인 번들에서 분리.
// 페어링되어 이 훅이 실제 가동될 때만 로드된다.

/**
 * 시니어 음성 클라이언트 (브라우저 마이크 ↔ 서버 /ws/voice).
 * (docs/voice-cloud-refactor-design.md §3, §2.5 half-duplex)
 *
 * - 입력: Silero VAD(@ricky0123/vad-web)로 발화 구간 분할 → encodeWAV(16kHz) → binary 업로드
 * - 출력: 서버 {type:'speak'|'beep', url} 수신 → <audio> 재생
 * - half-duplex(서버 is_speaking과 정렬):
 *     · 재생 지시 수신 즉시 VAD 정지(캡처 OFF) → 마이크가 스피커 소리를 못 잡음(에코 방지)
 *     · 항목마다 재생 종료 시 playback_done 송신(서버는 emit마다 1:1로 대기)
 *     · 마지막 재생 후 grace 동안 새 재생 없으면 VAD 재개 → beep→speak 연속을 한 정지창으로 묶음
 *
 * ⚠️ 이 훅의 진짜 검증은 실제 갤탭 마이크로 도는 end-to-end 통합 테스트다. (스크립트로 'PASS' 못 만듦)
 */

const WS_URL = seniorWsUrl('/ws/voice')
// TTS/효과음 재생 베이스. 서버가 보내는 url은 /tts/latest, /sounds/... 같은 서버 기준 절대경로라
// 로컬 dev(프론트 5173 ↔ 백엔드 8000)에서 상대경로로 두면 5173으로 요청돼 404 → 무음.
const HTTP_BASE = seniorHttpUrl()
const VAD_VERSION = '0.0.30'        // package.json과 일치 유지
const ORT_VERSION = '1.26.0'
// 마지막 재생 후 VAD 재개 유예. beep→speak가 연달아 오므로 그 사이 캡처가 켜지지 않게 묶는다.
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
  const pausedRef = useRef(false)   // 우리 의도로 VAD 캡처를 멈춘 상태인지

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
        // 재생 대기열이 완전히 빈 경우에만 캡처 재개
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
        scheduleResume()  // 연속 재생(beep→speak)이 끝난 뒤에만 VAD 재개
        return
      }
      playingRef.current = true
      const item = queue.shift()
      // 서버 기준 절대경로(/tts/latest 등)에 백엔드 베이스를 붙여 cross-origin 재생.
      const absUrl = item.url.startsWith('http') ? item.url : HTTP_BASE + item.url
      const sep = absUrl.includes('?') ? '&' : '?'
      const audio = new Audio(absUrl + sep + 'ts=' + (item.ts || Date.now()))
      audioElRef.current = audio
      // 서버는 emit마다 playback_done을 기다림(is_speaking 1:1) → 항목마다 정확히 1회 송신.
      // 재생 실패(onerror)에도 송신해야 서버가 timeout까지 멈추지 않음.
      const onDone = () => {
        sendControl('playback_done')
        playNext()
      }
      audio.onended = onDone
      audio.onerror = onDone
      audio.play().catch(onDone)
    }

    const enqueuePlay = (item) => {
      // 재생 지시 받는 즉시 캡처 정지 — 서버 is_speaking 윈도우와 정렬(에코 방지 1차 방어)
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

    // Silero VAD — 동적 import로 ort-web(무거움)을 메인 번들에서 분리, 가동 시에만 로드
    import('@ricky0123/vad-web')
      .then(({ MicVAD, utils }) =>
        MicVAD.new({
          // wasm/worklet/모델은 CDN에서 로드(번들 부담↓, 버전 고정)
          baseAssetPath: `https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@${VAD_VERSION}/dist/`,
          onnxWASMBasePath: `https://cdn.jsdelivr.net/npm/onnxruntime-web@${ORT_VERSION}/dist/`,
          // 무음 ~1.3초면 발화 종료(노인 발화 호흡 고려). 실측 튜닝값 — design §6.
          redemptionFrames: 14,
          minSpeechFrames: 4,
          positiveSpeechThreshold: 0.6,
          negativeSpeechThreshold: 0.4,
          onSpeechEnd: (audio) => {
            // half-duplex 2차 방어: 정지/재생 중이면 업로드 금지(서버도 is_speaking로 드롭하지만
            // 네트워크로 새어 가지 않게 클라에서 먼저 차단)
            if (cancelled || pausedRef.current || playingRef.current) return
            const ws = wsRef.current
            if (!ws || ws.readyState !== WebSocket.OPEN) return
            ws.send(utils.encodeWAV(audio))  // 16kHz mono WAV ArrayBuffer → binary 프레임
          },
        }),
      )
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
