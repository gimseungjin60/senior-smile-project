import { useEffect, useRef, useState, useCallback } from 'react'
import { seniorHttpUrl } from '../utils/host'

/**
 * OpenAI Realtime 음성 클라이언트 (브라우저 ↔ OpenAI 직접 WebRTC).
 * - 서버는 /api/realtime/token(ephemeral) 발급 + /api/realtime/weather(실데이터)만 담당.
 * - 오디오 스트림은 우리 서버를 안 거침(Render 부담↓). 7살 손주 페르소나/도구는 토큰 세션에 포함.
 * - 마이크는 이 훅이 전담 → useVoiceClient 는 playbackOnly(캔드음원 재생)로만 병행.
 * - 모바일 자동재생: start() 가 사용자 탭(제스처) 안에서 호출되어야 마이크/오디오 허용됨.
 */
const HTTP_BASE = seniorHttpUrl()
const RT_CALLS = 'https://api.openai.com/v1/realtime/calls?model=gpt-realtime'

export function useRealtimeClient() {
  const [active, setActive] = useState(false)
  const [status, setStatus] = useState('idle')   // idle | connecting | live | error
  const pcRef = useRef(null)
  const msRef = useRef(null)
  const dcRef = useRef(null)
  const audioRef = useRef(null)

  const stop = useCallback(() => {
    try { msRef.current?.getTracks().forEach((t) => t.stop()) } catch { /* 무시 */ }
    try { pcRef.current?.close() } catch { /* 무시 */ }
    try { if (audioRef.current) audioRef.current.srcObject = null } catch { /* 무시 */ }
    msRef.current = null; pcRef.current = null; dcRef.current = null
    setActive(false); setStatus('idle')
  }, [])

  // 모델이 도구 호출 → 실데이터 가져와 결과 회신 → 모델이 그 값으로 말함
  const handleTool = useCallback(async (name, callId) => {
    let out
    try {
      if (name === 'get_weather') out = await fetch(`${HTTP_BASE}/api/realtime/weather`).then((r) => r.json())
      else if (name === 'get_time') out = { now: new Date().toLocaleString('ko-KR') }
      else out = { error: 'unknown tool' }
    } catch (e) { out = { error: String(e) } }
    const dc = dcRef.current
    if (dc && dc.readyState === 'open') {
      dc.send(JSON.stringify({ type: 'conversation.item.create',
        item: { type: 'function_call_output', call_id: callId, output: JSON.stringify(out) } }))
      dc.send(JSON.stringify({ type: 'response.create' }))
    }
  }, [])

  const start = useCallback(async () => {
    if (pcRef.current) return
    setStatus('connecting')
    try {
      const t = await fetch(`${HTTP_BASE}/api/realtime/token`).then((r) => r.json())
      const ek = t.value || (t.client_secret && t.client_secret.value)
      if (!ek) { console.warn('[realtime] 토큰 실패', t); setStatus('error'); return }

      const pc = new RTCPeerConnection()
      pcRef.current = pc

      // 원격(AI) 오디오 재생용 엘리먼트 (제스처 안에서 생성 → 모바일 자동재생 허용)
      let audio = audioRef.current
      if (!audio) { audio = new Audio(); audio.autoplay = true; audioRef.current = audio }
      pc.ontrack = (e) => { audio.srcObject = e.streams[0]; audio.play().catch(() => {}) }

      const ms = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      })
      msRef.current = ms
      pc.addTrack(ms.getTracks()[0])

      const dc = pc.createDataChannel('oai-events')
      dcRef.current = dc
      dc.onmessage = (e) => {
        try {
          const o = JSON.parse(e.data)
          if (o.type === 'response.function_call_arguments.done') handleTool(o.name, o.call_id)
        } catch { /* 무시 */ }
      }

      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)
      const resp = await fetch(RT_CALLS, {
        method: 'POST', body: offer.sdp,
        headers: { Authorization: 'Bearer ' + ek, 'Content-Type': 'application/sdp' },
      })
      if (!resp.ok) { console.warn('[realtime] SDP 실패', resp.status, await resp.text()); setStatus('error'); return }
      await pc.setRemoteDescription({ type: 'answer', sdp: await resp.text() })

      pc.onconnectionstatechange = () => {
        if (['failed', 'closed', 'disconnected'].includes(pc.connectionState)) setStatus('error')
      }
      setActive(true); setStatus('live')
    } catch (e) {
      console.warn('[realtime] start 에러', e); setStatus('error'); stop()
    }
  }, [handleTool, stop])

  useEffect(() => () => stop(), [stop])  // 언마운트 시 정리

  return { active, status, start, stop }
}
