import { useCallback, useEffect, useState } from 'react'
import { ApiError } from './client'

interface ApiDataState<T> {
  data: T | null
  error: string | null
  isLoading: boolean
  reload: () => void
}

// 목록/상세 조회 페이지에서 반복되는 loading/error 상태 관리를 감싼 훅.
// deps가 바뀌면 fetcher를 다시 호출한다.
export function useApiData<T>(fetcher: () => Promise<T>, deps: unknown[]): ApiDataState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [reloadToken, setReloadToken] = useState(0)

  const load = useCallback(() => {
    setIsLoading(true)
    setError(null)
    fetcher()
      .then((result) => setData(result))
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : '네트워크 연결을 확인한 뒤 다시 시도해 주세요.')
      })
      .finally(() => setIsLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, reloadToken])

  useEffect(() => {
    load()
  }, [load])

  return { data, error, isLoading, reload: () => setReloadToken((n) => n + 1) }
}
