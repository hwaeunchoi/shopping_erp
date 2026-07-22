import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, clearToken, getToken, setToken } from '../api/client'
import type { TokenResponse, UserProfile } from '../api/types'

interface AuthContextValue {
  user: UserProfile | null
  isLoading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => void
  toggleTheme: () => void
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined)

function applyTheme(theme: string): void {
  document.documentElement.setAttribute('data-theme', theme === 'DARK' ? 'dark' : 'light')
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserProfile | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  const loadMe = useCallback(async () => {
    if (!getToken()) {
      setUser(null)
      setIsLoading(false)
      return
    }
    try {
      const me = await api.get<UserProfile>('/api/auth/me')
      setUser(me)
      applyTheme(me.theme_preference)
    } catch {
      clearToken()
      setUser(null)
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    loadMe()
  }, [loadMe])

  const login = useCallback(async (username: string, password: string) => {
    const form = new URLSearchParams()
    form.set('username', username)
    form.set('password', password)
    const token = await api.postForm<TokenResponse>('/api/auth/login', form)
    setToken(token.access_token)
    await loadMe()
  }, [loadMe])

  const logout = useCallback(() => {
    clearToken()
    setUser(null)
  }, [])

  // UI 와이어프레임 v1.1 1장: 다크모드 토글. 즉시 화면에 반영하고 users.theme_preference에 저장한다.
  const toggleTheme = useCallback(() => {
    setUser((prev) => {
      if (!prev) return prev
      const next = prev.theme_preference === 'DARK' ? 'LIGHT' : 'DARK'
      applyTheme(next)
      api.patch<UserProfile>('/api/auth/me/theme', { theme_preference: next }).catch(() => {
        // 저장 실패해도 화면 전환 자체는 유지한다.
      })
      return { ...prev, theme_preference: next }
    })
  }, [])

  return (
    <AuthContext.Provider value={{ user, isLoading, login, logout, toggleTheme }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth는 AuthProvider 내부에서만 사용할 수 있습니다.')
  }
  return ctx
}
