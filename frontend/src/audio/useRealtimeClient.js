import { useEffect, useRef, useState, useCallback } from 'react'
import { seniorHttpUrl } from '../utils/host'

/**
 * OpenAI Realtime 음성 클라이언트 (브라우저 ↔ OpenAI 직접 WebRTC).
 * - 서버는 /api/realtime/token(ephemeral) 발급 + /api/realtime/weather 만 담당. 오디오는 서버 안 거침.
 * - 마이크는 이 훅 전담 → useVoiceClient 는 playbackOnly(캔드음원 재생)로만 병행.
 * - 도구: get_weather/get_time/start_game/start_stretch/play_media/stop_media (콜백으로 App 연동).
 * - 30초 음성 미감지 → idle(대기, "앨범아 불러주세요"). 발화 감지되면 해제(앨범아로 깨움). 마이크는 계속 ON.
 * - 캔드음원 재생 중 duck/unduck. 미디어 재생 중 AI음성 음소거(restoreOutput로 복구).
 */
const HTTP_BASE = seniorHttpUrl()
const RT_CALLS = 'https://api.openai.com/v1/realtime/calls?model=gpt-realtime'
const IDLE_MS = 30000   // 음성 미감지 30초 → 대기

export function useRealtimeClient(opts = {}) {
  const [active, setActive] = useState(false)
  const [status, setStatus] = useState('idle')   // idle | connecting | live | error
  const [idle, setIdle] = useState(false)         // 30초 음성 미감지 = 대기("앨범아 불러주세요")
  const optsRef = useRef(opts)
  optsRef.current = opts

  const pcRef = useRef(null)
  const msRef = useRef(null)
  const dcRef = useRef(null)
  const audioRef = useRef(null)
  const armedRef = useRef(false)
  const reconnectRef = useRef(null)
  const connectRef = useRef(null)
  const lastSpeechRef = useRef(0)

  const cleanupPeer = () => {
    try { msRef.current?.getTracks().forEach((t) => t.stop()) } catch { /* 무시 */ }
    try { pcRef.current?.close() } catch { /* 무시 */ }
    msRef.current = null; pcRef.current = null; dcRef.current = null
  }

  const handleTool = useCallback(async (name, callId, argsJson) => {
    let args = {}
    try { args = JSON.parse(argsJson || '{}') } catch { /* 무시 */ }
    let out
    try {
      if (name === 'get_weather') out = await fetch(`${HTTP_BASE}/api/realtime/weather`).then((r) => r.json())
      else if (name === 'get_time') out = { now: new Date().toLocaleString('ko-KR') }
      else if (name === 'start_game') { optsRef.current.onStartGame?.(); out = { ok: true, started: 'cognitive_game' } }
      else if (name === 'start_stretch') { optsRef.current.onStartStretch?.(); out = { ok: true, started: 'stretching' } }
      else if (name === 'play_media') {
        optsRef.current.onPlayMedia?.(args.query || '')
        try { if (audioRef.current) audioRef.current.muted = true } catch { /* 무시 */ }  // 음악 중 AI음성 음소거(겹침 방지)
        out = { ok: true, playing: args.query || '' }
      } else if (name === 'stop_media') {
        optsRef.current.onStopMedia?.()
        try { if (audioRef.current) audioRef.current.muted = false } catch { /* 무시 */ }
        out = { ok: true, stopped: true }
      } else out = { error: 'unknown tool' }
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
          if (o.type === 'response.function_call_arguments.done') handleTool(o.name, o.call_id, o.arguments)
          else if (o.type === 'input_audio_buffer.speech_started') { lastSpeechRef.current = Date.now(); setIdle(false) }
        } catch { /* 무시 */ }
      }

      pc.onconnectionstatechange = () => {
        if (['failed', 'disconnected', 'closed'].includes(pc.connectionState)) {
          setActive(false); setStatus('error'); cleanupPeer(); scheduleReconnect()
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
      lastSpeechRef.current = Date.now()
      setActive(true); setStatus('live')
    } catch (e) {
      console.warn('[realtime] connect 에러', e); cleanupPeer(); setStatus('error'); scheduleReconnect()
    }
  }, [handleTool, scheduleReconnect])

  connectRef.current = connect

  const start = useCallback(() => { armedRef.current = true; connect() }, [connect])

  const stop = useCallback(() => {
    armedRef.current = false
    clearTimeout(reconnectRef.current)
    cleanupPeer()
    try { if (audioRef.current) audioRef.current.srcObject = null } catch { /* 무시 */ }
    setActive(false); setStatus('idle'); setIdle(false)
  }, [])

  // 캔드음원(pill_remind 등) 재생 중 덕킹 — 마이크 off + AI음성 음소거 + 진행응답 취소
  const duck = useCallback(() => {
    try { msRef.current?.getAudioTracks().forEach((t) => { t.enabled = false }) } catch { /* 무시 */ }
    try { if (audioRef.current) audioRef.current.muted = true } catch { /* 무시 */ }
    try { const dc = dcRef.current; if (dc?.readyState === 'open') dc.send(JSON.stringify({ type: 'response.cancel' })) } catch { /* 무시 */ }
  }, [])
  const unduck = useCallback(() => {
    try { msRef.current?.getAudioTracks().forEach((t) => { t.enabled = true }) } catch { /* 무시 */ }
    try { if (audioRef.current) audioRef.current.muted = false } catch { /* 무시 */ }
  }, [])
  // 미디어 X버튼 등 수동 종료 시 AI음성 복구
  const restoreOutput = useCallback(() => {
    try { if (audioRef.current) audioRef.current.muted = false } catch { /* 무시 */ }
  }, [])
  // 게임/스트레칭/미디어 종료를 모델에 알려 컨텍스트가 거기 머무르지 않게 함
  const notifyEvent = useCallback((text) => {
    const dc = dcRef.current
    if (dc?.readyState === 'open') {
      dc.send(JSON.stringify({ type: 'conversation.item.create',
        item: { type: 'message', role: 'user', content: [{ type: 'input_text', text }] } }))
    }
  }, [])

  // 음성 30초 미감지 → 대기. 발화 감지 시 위 onmessage 에서 해제됨.
  useEffect(() => {
    if (!active) { setIdle(false); return }
    const id = setInterval(() => {
      if (Date.now() - lastSpeechRef.current > IDLE_MS) setIdle(true)
    }, 3000)
    return () => clearInterval(id)
  }, [active])

  useEffect(() => () => { armedRef.current = false; clearTimeout(reconnectRef.current); cleanupPeer() }, [])

  return { active, status, idle, start, stop, duck, unduck, restoreOutput, notifyEvent }
}
