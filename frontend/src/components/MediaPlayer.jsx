/**
 * 미디어 플레이어 오버레이 — Realtime play_media 도구로 켜짐.
 * 유튜브 검색 임베드(첫 결과 자동재생) + 큰 X 버튼(수동 종료). 음성 "꺼줘"(stop_media)로도 종료됨.
 * (검색 임베드가 막히면 query→videoId 매핑 방식으로 교체 가능)
 */
export default function MediaPlayer({ query, onClose }) {
  if (!query) return null
  const src = `https://www.youtube.com/embed?listType=search&list=${encodeURIComponent(query)}&autoplay=1&rel=0`
  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 50,
      background: 'rgba(0,0,0,0.92)', display: 'flex',
      flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 16,
    }}>
      <button
        onClick={onClose}
        style={{
          position: 'absolute', top: 20, right: 20,
          fontSize: 22, fontWeight: 800, color: '#fff',
          background: '#e11d48', border: 'none', borderRadius: 12,
          padding: '14px 22px', cursor: 'pointer', zIndex: 51,
        }}
      >
        ✕ 끄기
      </button>
      <div style={{ color: '#fff', fontSize: 18, fontWeight: 700, opacity: 0.85 }}>
        🎵 {query}
      </div>
      <iframe
        title="media"
        src={src}
        allow="autoplay; encrypted-media"
        allowFullScreen
        style={{ width: '90vw', height: '70vh', maxWidth: 1100, border: 'none', borderRadius: 12 }}
      />
    </div>
  )
}
