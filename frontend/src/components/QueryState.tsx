import type { ReactNode } from 'react'

export type QueryKind =
  | 'loading'
  | 'empty'
  | 'forbidden'
  | 'network'
  | 'server'
  | 'stale'
  | 'ready'

/** 前期准备共用查询态：页面只注入对象名与允许动作。 */
export default function QueryState({
  loading,
  error,
  hasData,
  kind,
  objectName = '内容',
  loadingText,
  emptyText,
  onRetry,
  stale,
  children,
}: {
  loading?: boolean
  error?: string | null
  hasData: boolean
  kind?: QueryKind
  objectName?: string
  loadingText?: string
  emptyText?: string
  onRetry?: () => void
  stale?: boolean
  children: ReactNode
}) {
  const resolved: QueryKind = kind
    || (error && !hasData
      ? (/网络|fetch|Failed to fetch|timeout/i.test(error) ? 'network' : 'server')
      : loading && !hasData
        ? 'loading'
        : !hasData
          ? 'empty'
          : 'ready')

  if (resolved === 'loading') {
    return (
      <div className="empty query-loading" role="status">
        <strong>正在加载{objectName}</strong>
        <p>{loadingText || '请稍候，正在拉取最新状态……'}</p>
      </div>
    )
  }
  if (resolved === 'forbidden') {
    return (
      <div className="empty query-error" role="alert">
        <strong>无权限查看{objectName}</strong>
        <p>当前会话不能访问该项目。请确认后重试。</p>
        {onRetry && <button type="button" className="btn" onClick={onRetry}>重试</button>}
      </div>
    )
  }
  if (resolved === 'network' || resolved === 'server' || (error && !hasData)) {
    return (
      <div className="empty query-error" role="alert">
        <strong>{resolved === 'network' ? '网络异常' : '服务异常'}</strong>
        <p>{error || `暂时无法加载${objectName}`}</p>
        {onRetry && <button type="button" className="btn" onClick={onRetry}>重试</button>}
      </div>
    )
  }
  if (resolved === 'empty' || !hasData) {
    return (
      <div className="empty query-empty" role="status">
        <strong>暂无{objectName}</strong>
        <p>{emptyText || '还没有可展示的数据。'}</p>
        {onRetry && <button type="button" className="btn" onClick={onRetry}>刷新</button>}
      </div>
    )
  }
  return (
    <>
      {stale && (
        <div className="query-stale-banner" role="status">
          当前展示可能已过期
          {onRetry && <> · <button type="button" className="btn small" onClick={onRetry}>刷新</button></>}
        </div>
      )}
      {children}
    </>
  )
}
