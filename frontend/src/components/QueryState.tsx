import type { ReactNode } from 'react'

/** 统一查询态：失败优先于加载，避免错误被「展卷中」永久遮住。 */
export default function QueryState({
  loading,
  error,
  hasData,
  loadingText = '展卷中……',
  children,
}: {
  loading?: boolean
  error?: string | null
  hasData: boolean
  loadingText?: string
  children: ReactNode
}) {
  if (error && !hasData) return <div className="empty query-error" role="alert">{error}</div>
  if (loading && !hasData) return <div className="empty query-loading">{loadingText}</div>
  if (!hasData) return <div className="empty query-loading">{loadingText}</div>
  return <>{children}</>
}
