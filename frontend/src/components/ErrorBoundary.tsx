import { Component, type ErrorInfo, type ReactNode } from 'react'

interface ErrorBoundaryProps {
  children: ReactNode
  /** 变化时自动清除错误：路由切换后不该继续停在上一页的崩溃界面。 */
  resetKey?: unknown
  title?: string
  /** 兜底文案下方的额外出口，例如「返回项目空间」。 */
  actions?: ReactNode
}

interface ErrorBoundaryState {
  error: Error | null
}

/** 动态 chunk 拉取失败的报错在各浏览器措辞不同，统一按「网络没取到代码」处理。 */
export function isChunkLoadError(error: Error | null): boolean {
  if (!error) return false
  const text = `${error.name} ${error.message}`
  return /ChunkLoadError|Loading chunk|Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed/i.test(text)
}

/**
 * 渲染期异常兜底。
 *
 * 没有它时，任何一次渲染抛错——包括弱网下 lazy() 的分包请求失败——都会让 React
 * 卸载整棵树，页面变成纯白且无任何提示，只能靠用户自己想到刷新。
 */
export default class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 控制台留栈，界面只给用户可操作的信息。
    console.error('[ErrorBoundary]', error, info.componentStack)
  }

  componentDidUpdate(prev: ErrorBoundaryProps) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null })
    }
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    const chunk = isChunkLoadError(error)
    return (
      <div className="empty query-error" role="alert">
        <strong>{chunk ? '页面资源没有加载完整' : this.props.title || '这个页面出错了'}</strong>
        <p>
          {chunk
            ? '网络中断或版本已更新，导致这一页的代码没取回来。重新加载即可恢复，已保存的数据不受影响。'
            : '页面渲染时出现异常，已停止在此处以免影响其它区域。已保存的数据不受影响。'}
        </p>
        <p className="error-boundary-detail"><code>{error.message || String(error)}</code></p>
        <div className="workspace-empty-actions">
          <button type="button" className="btn primary" onClick={() => window.location.reload()}>
            重新加载页面
          </button>
          {!chunk && (
            <button type="button" className="btn" onClick={() => this.setState({ error: null })}>
              重试渲染
            </button>
          )}
          {this.props.actions}
        </div>
      </div>
    )
  }
}
