import SubtitleBar from './SubtitleBar'
import './GreetScreen.css'

function GreetScreen({ subtitle, isListening }) {
  return (
    <div className="greet-screen">
      <div className="greet-glow" />

      {/* 위: 인사 */}
      <div className="greet-content">
        <div className="greet-icon-ring">
          <div className="greet-emoji">👋</div>
        </div>
        <h1 className="greet-title">안녕하세요!</h1>
        <p className="greet-subtitle">오늘도 좋은 하루예요</p>
      </div>

      {/* 아래: AI 음성 패널 */}
      <div className="greet-voice-area">
        <SubtitleBar subtitle={subtitle} isListening={isListening} />
      </div>
    </div>
  )
}

export default GreetScreen
