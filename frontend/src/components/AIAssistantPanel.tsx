import { useState } from 'react'
import { api } from '../api/client'
import type { AiAssistantAnswer } from '../api/types'

// UI 와이어프레임 v1.1 8장: 상단바 AI Assistant(🤖) 버튼 + 우측 슬라이드 패널.
// 1차 범위는 UI 뼈대 + 백엔드의 간단한 규칙 기반 응답(services/ai_assistant_service.py)이며,
// 향후 실제 AI 모델로 교체되어도 이 패널 UI는 그대로 유지하도록 설계되어 있다.
const QUICK_QUESTIONS = ['이번주 매출 요약', '저ROAS 캠페인 찾기', '반품 급증 원인']

interface Message {
  role: 'user' | 'assistant'
  text: string
}

export function AIAssistantPanel() {
  const [isOpen, setIsOpen] = useState(false)
  const [question, setQuestion] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [isLoading, setIsLoading] = useState(false)

  async function send(q: string) {
    if (!q.trim() || isLoading) return
    setMessages((prev) => [...prev, { role: 'user', text: q }])
    setQuestion('')
    setIsLoading(true)
    try {
      const res = await api.post<AiAssistantAnswer>('/api/ai-assistant/ask', { question: q })
      setMessages((prev) => [...prev, { role: 'assistant', text: res.answer }])
    } catch {
      setMessages((prev) => [...prev, { role: 'assistant', text: '오류가 발생했습니다. 잠시 후 다시 시도해주세요.' }])
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <>
      <button type="button" className="ai-assistant-trigger" onClick={() => setIsOpen((o) => !o)} title="AI Assistant">
        🤖
      </button>
      {isOpen && (
        <div className="ai-assistant-panel">
          <div className="ai-assistant-header">
            <span>ERP AI Assistant</span>
            <button type="button" onClick={() => setIsOpen(false)}>
              ✕
            </button>
          </div>
          <div className="ai-assistant-messages">
            {messages.length === 0 && <p className="form-info">궁금한 점을 물어보거나 아래 빠른 질문을 눌러보세요.</p>}
            {messages.map((m, i) => (
              <div key={i} className={`ai-message ai-message-${m.role}`}>
                {m.text}
              </div>
            ))}
            {isLoading && <div className="ai-message ai-message-assistant">답변 생성 중...</div>}
          </div>
          <div className="ai-quick-questions">
            {QUICK_QUESTIONS.map((q) => (
              <button key={q} type="button" onClick={() => send(q)} disabled={isLoading}>
                {q}
              </button>
            ))}
          </div>
          <form
            className="ai-assistant-input"
            onSubmit={(e) => {
              e.preventDefault()
              send(question)
            }}
          >
            <input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="질문을 입력하세요"
              disabled={isLoading}
            />
            <button type="submit" disabled={isLoading}>
              전송
            </button>
          </form>
        </div>
      )}
    </>
  )
}
