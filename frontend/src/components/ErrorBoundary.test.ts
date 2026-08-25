import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { isChunkLoadError, shouldAutoReload } from './ErrorBoundary'

describe('分包加载失败识别', () => {
  it('认出各浏览器的动态 import 失败措辞', () => {
    const chunkErrors = [
      Object.assign(new Error('Loading chunk 42 failed.'), { name: 'ChunkLoadError' }),
      new Error('Failed to fetch dynamically imported module: /assets/WallPage-CJvYpOUT.js'),
      new Error('error loading dynamically imported module'),
      new Error('Importing a module script failed.'),
    ]
    for (const error of chunkErrors) {
      expect(isChunkLoadError(error)).toBe(true)
    }
  })

  it('认出发版后老标签页的 MIME 报错', () => {
    // 2026-08-25 线上实况：老标签页去拉已被新版本替换掉的指纹文件，
    // 服务器把 index.html 当兜底返回，浏览器报的是 MIME 不对而不是 404。
    const staleDeployErrors = [
      new Error("'text/html' is not a valid JavaScript MIME type for module script 'https://automanju.com/assets/MonitorPage-BW2UtCWr.js'."),
      new Error("Failed to load module script: Expected a JavaScript module script but the server responded with a MIME type of \"text/html\"."),
    ]
    for (const error of staleDeployErrors) {
      expect(isChunkLoadError(error)).toBe(true)
    }
  })

  it('不把普通渲染异常误判为分包失败', () => {
    expect(isChunkLoadError(new TypeError("Cannot read properties of undefined (reading 'shots')"))).toBe(false)
    expect(isChunkLoadError(new Error('无法连接本机后端服务，请等待服务恢复后重试'))).toBe(false)
    expect(isChunkLoadError(null)).toBe(false)
  })
})

describe('分包失败后的自助重载', () => {
  const store = new Map<string, string>()
  beforeEach(() => {
    store.clear()
    vi.stubGlobal('window', {
      sessionStorage: {
        getItem: (k: string) => store.get(k) ?? null,
        setItem: (k: string, v: string) => { store.set(k, v) },
      },
    })
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('首次遇到就重载，冷却窗口内不再重载', () => {
    const t0 = 1_000_000
    expect(shouldAutoReload(t0)).toBe(true)
    // 刷完立刻又炸 —— 说明不是版本漂移而是真坏了，把报错界面留给用户
    expect(shouldAutoReload(t0 + 5_000)).toBe(false)
    expect(shouldAutoReload(t0 + 59_999)).toBe(false)
    // 隔得够久说明上次重载确实救回来了，再遇到可以再自助一次
    expect(shouldAutoReload(t0 + 60_001)).toBe(true)
  })

  it('读不到 sessionStorage 时不自动重载，宁可不救也不无限刷', () => {
    vi.stubGlobal('window', {
      sessionStorage: { getItem: () => { throw new Error('denied') }, setItem: () => {} },
    })
    expect(shouldAutoReload()).toBe(false)
  })
})
