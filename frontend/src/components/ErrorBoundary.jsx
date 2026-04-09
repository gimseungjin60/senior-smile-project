import { Component } from 'react'

class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false }
  }

  static getDerivedStateFromError() {
    return { hasError: true }
  }

  componentDidCatch(error, errorInfo) {
    console.error('[ErrorBoundary] 앱 오류 발생:', error, errorInfo)
  }

  handleRetry = () => {
    this.setState({ hasError: false })
  }

  render() {
    if (this.state.hasError) {
      return (
        <div style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100vh',
          background: 'var(--color-bg-warm, #FAF9F7)',
          fontFamily: 'var(--font-family, sans-serif)',
          gap: '1.5rem',
          padding: '2rem',
          textAlign: 'center',
        }}>
          <div style={{ fontSize: '4rem' }}>😊</div>
          <h1 style={{
            fontSize: '2.4rem',
            fontWeight: 700,
            color: 'var(--color-text-primary, #191F28)',
          }}>
            잠시 문제가 생겼어요
          </h1>
          <p style={{
            fontSize: '1.4rem',
            color: 'var(--color-text-secondary, #4E5968)',
            lineHeight: 1.6,
          }}>
            걱정 마세요! 아래 버튼을 눌러주세요.
          </p>
          <button
            onClick={this.handleRetry}
            style={{
              marginTop: '1rem',
              padding: '1rem 3rem',
              fontSize: '1.6rem',
              fontWeight: 700,
              color: '#fff',
              background: 'var(--color-primary, #0064FF)',
              border: 'none',
              borderRadius: '16px',
              cursor: 'pointer',
            }}
          >
            다시 시작하기
          </button>
        </div>
      )
    }

    return this.props.children
  }
}

export default ErrorBoundary
