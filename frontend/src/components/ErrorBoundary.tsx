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

/** 动态 chunk 拉取失败的报错在各浏览器措辞不同，统一按「网络没取到代码」处理。
 *
 *  后两条是发版后老标签页的典型症状：请求的指纹文件已经不存在，服务器若把
 *  index.html 当兜底返回，浏览器就报 MIME 不对而不是 404。后端已经改成对
 *  assets/ 返回真 404（app/main.py SpaStaticFiles._NO_FALLBACK），但线上可能
 *  还有旧版本后端、也可能有中间层这么干，所以这里一并认掉。 */
export function isChunkLoadError(error: Error | null): boolean {
  if (!error) return false
  const text = `${error.name} ${error.message}`
  return /ChunkLoadError|Loading chunk|Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|is not a valid JavaScript MIME type|Failed to load module script|Expected a JavaScript(?: or WebAssembly)? module script/i.test(text)
}

/** 自动重载的节流窗口：两次自动重载之间至少隔这么久，避免坏版本把用户卡在刷新循环里。 */
const AUTO_RELOAD_COOLDOWN_MS = 60_000
const AUTO_RELOAD_KEY = 'manju:chunk-auto-reload-at'

/**
 * 分包没取到时自助重载一次。
 *
 * 发版后老标签页拿的是旧模块图，点进某页才会去拉已经不存在的 chunk——用户看到的是
 * 一屏报错，而「刷新一下就好了」。重载会重新取 index.html（后端发 no-cache，必回源
 * 校验），拿到新指纹后自然恢复。
 *
 * 只在距上次自动重载超过 CD 时才重载：如果刷完立刻又炸，说明不是版本漂移而是真坏了，
 * 这时候把报错界面留给用户，别陷进无限刷新。
 */
export function shouldAutoReload(now: number = Date.now()): boolean {
  try {
    const last = Number(window.sessionStorage.getItem(AUTO_RELOAD_KEY) || 0)
    if (now - last < AUTO_RELOAD_COOLDOWN_MS) return false
    window.sessionStorage.setItem(AUTO_RELOAD_KEY, String(now))
    return true
  } catch {
    // 隐私模式下读不到 storage：宁可不自动重载，也不冒无限刷新的风险。
    return false
  }
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
    if (isChunkLoadError(error) && shouldAutoReload()) {
      window.location.reload()
    }
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
