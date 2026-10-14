import { Link } from 'react-router-dom'

// 존재하지 않는 주소로 들어왔을 때 보여 주는 화면. 로그인한 사용자의 레이아웃 안에서 표시한다.
export function NotFoundPage() {
  return (
    <div className="empty-page" role="alert">
      <h2>페이지를 찾을 수 없습니다</h2>
      <p>주소가 잘못되었거나 이동·삭제된 화면입니다. 왼쪽 메뉴에서 원하는 화면을 선택해 주세요.</p>
      <p>
        <Link to="/" className="button-link">대시보드로 이동</Link>
      </p>
    </div>
  )
}
