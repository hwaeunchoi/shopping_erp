import { Component, type ErrorInfo, type ReactNode } from 'react'

// 화면을 그리는 도중 예기치 못한 오류가 나도 빈 화면 대신 안전한 한국어 안내를 보여 준다.
// 개발자용 오류 내용은 사용자에게 보이지 않고 브라우저 콘솔에만 남긴다.
interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false }

  static getDerivedStateFromError(): State {
    return { hasError: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('화면 표시 중 오류', error, info.componentStack)
  }

  render() {
    if (!this.state.hasError) return this.props.children
    return (
      <div className="empty-page" role="alert">
        <h2>화면을 표시하지 못했습니다</h2>
        <p>일시적인 문제일 수 있습니다. 새로고침해도 계속되면 관리자에게 알려 주세요.</p>
        <p>
          <button type="button" onClick={() => window.location.reload()}>새로고침</button>
        </p>
      </div>
    )
  }
}
