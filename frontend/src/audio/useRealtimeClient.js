import { useEffect, useRef, useState, useCallback } from 'react'
import { seniorHttpUrl } from '../utils/host'

/**
 * OpenAI Realtime 음성 클라이언트 (브라우저 ↔ OpenAI 직접 WebRTC).
 * - 서버는 /api/realtime/token(ephemeral) 발급 + /api/realtime/weather(실데이터)만 담당.
 * - 오디오 스트림은 우리 서버를 안 거침(Render 부담↓). 7살 손주 페르소나/도구/호출어규칙은 토큰 세션에 포함.
 * - 마이크는 이 훅이 전담 → useVoiceClient 는 playbackOnly(캔드음원 재생)로만 병행.
 * - 모바일 자동재생: arm()(=start) 는 사용자 제스처(셋업 1회 탭) 안에서 호출돼야 마이크/오디오 허용.
 * - 한 번 켜지면 끊겨도 자동 재연결(시연 중 끊김 방지).
 */
const HTTP_BASE = seniorHttpUrl()
const RT_CALLS = 'https://api.openai.com/v1/realtime/calls?model=gpt-realtime'

export function useRealtimeClient(opts = {}) {
  const [active, setActive] = useState(false)
  const [status, setStatus] = useState('idle')   // idle | connecting | live | error
  const optsRef = useRef(opts)
  optsRef.current = opts   // onStartGame/onStartStretch 등 콜백 최신값 유지(stale 방지)
  const pcRef = useRef(null)
  const msRef = useRef(null)
  const dcRef = useRef(null)
  const audioRef = useRef(null)
  const armedRef = useRef(false)          // 셋업 탭으로 켜진 상태(=유지+자동재연결 대상)
  const reconnectRef = useRef(null)
  const connectRef = useRef(null)

  const cleanupPeer = () => {
    try { msRef.current?.getTracks().forEach((t) => t.stop()) } catch { /* 무시 */ }
    try { pcRef.current?.close() } catch { /* 무시 */ }
    msRef.current = null; pcRef.current = null; dcRef.current = null
  }

  const handleTool = useCallback(async (name, callId) => {
    let out
    try {
      if (name === 'get_weather') out = await fetch(`${HTTP_BASE}/api/realtime/weather`).then((r) => r.json())
      else if (name === 'get_time') out = { now: new Date().toLocaleString('ko-KR') }
      else if (name === 'start_game') { optsRef.current.onStartGame?.(); out = { ok: true, started: 'cognitive_game' } }
      else if (name === 'start_stretch') { optsRef.current.onStartStretch?.(); out = { ok: true, started: 'stretching' } }
      else out = { error: 'unknown tool' }
    } catch (e) { out = { error: String(e) } }
    const dc = dcRef.current
    if (dc && dc.readyState === 'open') {
      dc.send(JSON.stringify({ type: 'conversation.item.create',
        item: { type: 'function_call_output', call_id: callId, output: JSON.stringify(out) } }))
      dc.send(JSON.stringify({ type: 'response.create' }))
    }
  }, [])

  const scheduleReconnect = useCallback(() => {
    if (!armedRef.current) return
    clearTimeout(reconnectRef.current)
    reconnectRef.current = setTimeout(() => { if (armedRef.current) connectRef.current?.() }, 2000)
  }, [])

  const connect = useCallback(async () => {
    if (pcRef.current) return
    setStatus('connecting')
    try {
      const t = await fetch(`${HTTP_BASE}/api/realtime/token`).then((r) => r.json())
      const ek = t.value || (t.client_secret && t.client_secret.value)
      if (!ek) { console.warn('[realtime] 토큰 실패', t); setStatus('error'); scheduleReconnect(); return }

      const pc = new RTCPeerConnection()
      pcRef.current = pc

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

      pc.onconnectionstatechange = () => {
        const st = pc.connectionState
        if (['failed', 'disconnected', 'closed'].includes(st)) {
          setActive(false); setStatus('error')
          cleanupPeer()
          scheduleReconnect()   // armed면 자동 재연결
        }
      }

      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)
      const resp = await fetch(RT_CALLS, {
        method: 'POST', body: offer.sdp,
        headers: { Authorization: 'Bearer ' + ek, 'Content-Type': 'application/sdp' },
      })
      if (!resp.ok) { console.warn('[realtime] SDP 실패', resp.status, await resp.text()); cleanupPeer(); setStatus('error'); scheduleReconnect(); return }
      await pc.setRemoteDescription({ type: 'answer', sdp: await resp.text() })
      setActive(true); setStatus('live')
    } catch (e) {
      console.warn('[realtime] connect 에러', e); cleanupPeer(); setStatus('error'); scheduleReconnect()
    }
  }, [handleTool, scheduleReconnect])

  connectRef.current = connect

  // 셋업 1회 탭에서 호출(제스처) → 이후 armed 유지 + 자동재연결
  const start = useCallback(() => { armedRef.current = true; connect() }, [connect])

  const stop = useCallback(() => {
    armedRef.current = false
    clearTimeout(reconnectRef.current)
    cleanupPeer()
    try { if (audioRef.current) audioRef.current.srcObject = null } catch { /* 무시 */ }
    setActive(false); setStatus('idle')
  }, [])

  useEffect(() => () => { armedRef.current = false; clearTimeout(reconnectRef.current); cleanupPeer() }, [])

  return { active, status, start, stop }
}
