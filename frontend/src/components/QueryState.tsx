import type { ReactNode } from 'react'
import OperationError from './OperationError'

export type QueryKind =
  | 'loading'
  | 'empty'
  | 'forbidden'
  | 'network'
  | 'server'
  | 'stale'
  | 'ready'

/** 世界书共用查询态：页面只注入对象名与允许动作。 */
export default function QueryState({
  loading,
  error,
  status,
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
  /** ApiError.status：403 渲染「无权访问」，跨账号 404 渲染「资源不存在」。 */
  status?: number | null
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
    || (status === 403 || status === 404
      ? 'forbidden'
      : error && !hasData
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
    const notFound = status === 404
    return (
      <div className="empty query-error" role="alert">
        <strong>{notFound ? '资源不存在或不属于当前账号' : '无权访问'}</strong>
        <p>
          {notFound
            ? '请确认链接是否正确，或改用拥有该项目的账号登录；如果你确认应该看到它，请联系系统管理员协助排查。'
            : `当前账号没有权限查看${objectName}，请联系系统管理员协助处理。`}
        </p>
        {onRetry && <button type="button" className="btn" onClick={onRetry}>重试</button>}
      </div>
    )
  }
  if (resolved === 'network' || resolved === 'server' || (error && !hasData)) {
    return (
      <div className="empty query-error">
        <OperationError
          title={resolved === 'network' ? '网络连接异常' : `${objectName}加载失败`}
          message={error}
          guidance={resolved === 'network'
            ? '请检查本机服务和网络连接后重试；当前不会用空数据覆盖已有内容。'
            : `暂时无法取得${objectName}，当前不会改选其他对象或把失败误报为空。`}
        >
          {onRetry && <button type="button" className="btn" onClick={onRetry}>重试加载</button>}
        </OperationError>
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
