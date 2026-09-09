import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from './client'

interface AutoRefreshState<T> {
  data: T | null
  error: string | null
  isLoading: boolean
  isRefreshing: boolean
  lastUpdatedAt: Date | null
  reload: () => void
  autoRefreshEnabled: boolean
  setAutoRefreshEnabled: (enabled: boolean) => void
}

// 상용 ERP 확장(6단계) - 운영 대시보드 전용 자동 새로고침 훅.
// useApiData와 별개로 만든 이유: 기존 훅을 건드리면 이미 그 훅을 쓰는 다른 모든
// 화면의 동작이 바뀔 위험이 있다(무관한 화면 개편 금지). 이 훅만의 추가 보장:
// - requestIdRef로 "이 요청이 아직 최신 요청인지"를 확인해, 자동 새로고침 중
//   이전 요청의 응답이 그 사이 시작된 더 최근 요청보다 늦게 도착해도 화면을
//   덮어쓰지 않는다(느린 응답이 최신 상태를 되돌리는 경합 방지).
// - mountedRef로 언마운트 후 늦게 도착한 응답이 setState를 호출하지 않게 막는다.
// - setInterval은 컴포넌트가 언마운트되거나 autoRefreshEnabled가 꺼지면 정리된다.
export function useAutoRefreshData<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: unknown[],
): AutoRefreshState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [lastUpdatedAt, setLastUpdatedAt] = useState<Date | null>(null)
  const [autoRefreshEnabled, setAutoRefreshEnabled] = useState(true)
  const [reloadToken, setReloadToken] = useState(0)

  const requestIdRef = useRef(0)
  const mountedRef = useRef(true)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const load = useCallback((isBackground: boolean) => {
    const myRequestId = ++requestIdRef.current
    if (isBackground) setIsRefreshing(true)
    else setIsLoading(true)
    setError(null)

    fetcherRef
      .current()
      .then((result) => {
        if (!mountedRef.current || myRequestId !== requestIdRef.current) return
        setData(result)
        setLastUpdatedAt(new Date())
      })
      .catch((err: unknown) => {
        if (!mountedRef.current || myRequestId !== requestIdRef.current) return
        setError(err instanceof ApiError ? `${err.message} (HTTP ${err.status})` : '알 수 없는 오류가 발생했습니다.')
      })
      .finally(() => {
        if (!mountedRef.current || myRequestId !== requestIdRef.current) return
        setIsLoading(false)
        setIsRefreshing(false)
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    mountedRef.current = true
    load(false)
    return () => {
      mountedRef.current = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, reloadToken])

  useEffect(() => {
    if (!autoRefreshEnabled) return undefined
    const id = window.setInterval(() => load(true), intervalMs)
    return () => window.clearInterval(id)
  }, [autoRefreshEnabled, intervalMs, load])

  return {
    data,
    error,
    isLoading,
    isRefreshing,
    lastUpdatedAt,
    reload: () => setReloadToken((n) => n + 1),
    autoRefreshEnabled,
    setAutoRefreshEnabled,
  }
}
