import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// App.tsx 曾在 episodeId 的两处异步二次校验（进项目 useEffect / openSection）在途
// 期间，把这个占位组件渲染成终态文案（“尚未进入具体分集” + “查看分集并选择”/
// “返回项目空间”两个引导按钮），而同一屏的 EpisodeCrumb 此刻正显示“正在加载
// 分集…”——界面对用户撒了谎。resolving prop 就是为了堵住这个矛盾：为真时必须是
// 诚实的加载态，且不得出现终态话术或终态按钮；为假时（今天的默认行为）必须与
// 改造前逐字一致。

// vi.mock 会被提升到文件顶部；工厂里只能引用以 `mock` 开头的变量，这里不需要。
vi.mock('../App', () => ({
  useNav: () => ({
    projectId: 'proj_test',
    episodeId: null,
    chapterIdx: null,
    view: 'script',
    go: () => {},
    requestNavigation: () => {},
    toast: () => {},
    registerNavigationGuard: () => {},
  }),
  // EpisodeCrumb（本组件的子组件）自己也从 '../App' 取 usePoll 发起分集切换器的
  // 独立轮询；不 mock 这个会在渲染期直接打真实网络。loading:true 让它渲染出
  // “正在加载分集…”，贴近缺陷现场那种“标题栏在途、正文却是终态”的真实时序。
  usePoll: () => ({ data: null, error: null, loading: true, refresh: async () => null }),
}))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import WorkspacePlaceholder from './WorkspacePlaceholder'

/** 项目测试环境是 node（未装 jsdom）。react-test-renderer 本身不需要真实 DOM，
 *  但 EpisodeCrumb 的搜索防抖在 useEffect 里摸了 window.setTimeout 等宿主 API，
 *  效果在 act() 里会被真实 flush，所以需要一个最小 window/document 存根
 *  （照抄 ScriptPage.render.test.ts 的写法）。 */
function installHostStubs() {
  const store = new Map<string, string>()
  const passthrough = {
    addEventListener: () => {},
    removeEventListener: () => {},
  }
  ;(globalThis as { window?: unknown }).window = {
    ...passthrough,
    setTimeout: (...args: Parameters<typeof setTimeout>) => setTimeout(...args),
    clearTimeout: (id: ReturnType<typeof setTimeout>) => clearTimeout(id),
    setInterval: (...args: Parameters<typeof setInterval>) => setInterval(...args),
    clearInterval: (id: ReturnType<typeof setInterval>) => clearInterval(id),
    requestAnimationFrame: (cb: FrameRequestCallback) => setTimeout(() => cb(Date.now()), 0) as unknown as number,
    cancelAnimationFrame: (id: number) => clearTimeout(id),
    localStorage: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => { store.set(key, value) },
      removeItem: (key: string) => { store.delete(key) },
      clear: () => { store.clear() },
    },
  }
  ;(globalThis as { document?: unknown }).document = {
    ...passthrough,
    activeElement: null,
    body: { style: {} },
    visibilityState: 'visible',
  }
}

function uninstallHostStubs() {
  delete (globalThis as { window?: unknown }).window
  delete (globalThis as { document?: unknown }).document
}

describe('WorkspacePlaceholder resolving vs terminal state', () => {
  beforeEach(() => {
    installHostStubs()
  })
  afterEach(() => {
    uninstallHostStubs()
  })

  it('resolving=true renders an honest in-flight state — never the terminal copy or its guidance buttons', () => {
    let renderer: TestRenderer.ReactTestRenderer
    act(() => {
      renderer = TestRenderer.create(
        React.createElement(WorkspacePlaceholder, { label: '映射台', view: 'script', resolving: true }),
      )
    })
    const serialized = JSON.stringify(renderer!.toJSON())

    expect(serialized).toContain('正在确认要进入的分集')
    expect(serialized).not.toContain('尚未进入具体分集')
    expect(serialized).not.toContain('查看分集并选择')
    expect(serialized).not.toContain('返回项目空间')
    // 正文必须挂 role="status"，与顶部 EpisodeCrumb 的“正在加载分集…”呼应，
    // 而不是被渲染成看起来已经结束的静态终态区块。
    expect(serialized).toContain('"role":"status"')

    act(() => { renderer!.unmount() })
  })

  it('resolving=false (default) keeps today’s terminal copy and guidance buttons byte-for-byte', () => {
    let renderer: TestRenderer.ReactTestRenderer
    act(() => {
      renderer = TestRenderer.create(
        React.createElement(WorkspacePlaceholder, { label: '映射台', view: 'script', resolving: false }),
      )
    })
    const serialized = JSON.stringify(renderer!.toJSON())

    expect(serialized).toContain('尚未进入具体分集')
    expect(serialized).toContain('查看分集并选择')
    expect(serialized).toContain('返回项目空间')
    expect(serialized).not.toContain('正在确认要进入的分集')

    act(() => { renderer!.unmount() })
  })

  it('omitting resolving entirely behaves the same as resolving=false', () => {
    let renderer: TestRenderer.ReactTestRenderer
    act(() => {
      renderer = TestRenderer.create(
        React.createElement(WorkspacePlaceholder, { label: '分镜台', view: 'board' }),
      )
    })
    const serialized = JSON.stringify(renderer!.toJSON())

    expect(serialized).toContain('尚未进入具体分集')
    expect(serialized).not.toContain('正在确认要进入的分集')

    act(() => { renderer!.unmount() })
  })
})
