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
        setError(err instanceof ApiError ? `${err.message} (HTTP ${err.status})` : '알 수 없는 오류가 발생했습니다.')
      })
      .finally(() => setIsLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, reloadToken])

  useEffect(() => {
    load()
  }, [load])

  return { data, error, isLoading, reload: () => setReloadToken((n) => n + 1) }
}
