import { api } from './client'

// 여러 화면(주문/교환/반품/취소/보고서)의 "엑셀 다운로드" 버튼이 공통으로 쓰는
// blob 다운로드 로직. 중복 코드를 피하기 위해 한 곳에 모은다.
export async function downloadBlob(path: string, filename: string): Promise<void> {
  const blob = await api.getBlob(path)
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
