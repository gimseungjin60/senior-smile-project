import './GreetScreen.css'

function GreetScreen() {
  return (
    <div className="greet-screen">
      <div className="greet-glow" />
      <div className="greet-content">
        <div className="greet-icon-ring">
          <div className="greet-emoji">👋</div>
        </div>
        <h1 className="greet-title">안녕하세요!</h1>
        <p className="greet-subtitle">오늘도 좋은 하루예요</p>
      </div>
    </div>
  )
}

export default GreetScreen
