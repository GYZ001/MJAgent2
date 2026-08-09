import { describe, expect, it } from 'vitest'
import { AdaptivePoller } from './adaptivePoller'

const flush = () => new Promise<void>(resolve => setTimeout(resolve, 0))

describe('AdaptivePoller', () => {
  it('wakes polling when a manual refresh changes idle data to running', async () => {
    type State = { status: 'idle' | 'running' | 'done'; shots: number }
    const responses: State[] = [
      { status: 'idle', shots: 0 },
      { status: 'running', shots: 0 },
      { status: 'running', shots: 1 },
      { status: 'done', shots: 2 },
    ]
    const received: State[] = []
    const timers: Array<() => void> = []
    const clock = {
      setTimeout: (callback: () => void) => {
        timers.push(callback)
        return callback
      },
      clearTimeout: (handle: unknown) => {
        const index = timers.indexOf(handle as () => void)
        if (index >= 0) timers.splice(index, 1)
      },
    }
    const poller = new AdaptivePoller(
      async () => responses.shift()!,
      state => state?.status === 'running' ? 2000 : 0,
      {
        onData: state => received.push(state),
        onError: error => { throw error },
      },
      clock,
    )

    await poller.start()
    expect(timers).toHaveLength(0)

    // 模拟用户点击“生成分镜”后的 refresh：旧实现会停在这里，不再继续轮询。
    await poller.refresh()
    expect(timers).toHaveLength(1)

    timers.shift()!()
    await flush()
    expect(received.at(-1)).toEqual({ status: 'running', shots: 1 })
    expect(timers).toHaveLength(1)

    timers.shift()!()
    await flush()
    expect(received.at(-1)).toEqual({ status: 'done', shots: 2 })
    expect(timers).toHaveLength(0)
  })

  it('does not publish or reschedule a response after stop', async () => {
    let resolveRequest!: (value: { status: string }) => void
    const received: Array<{ status: string }> = []
    const timers: Array<() => void> = []
    const poller = new AdaptivePoller(
      () => new Promise(resolve => { resolveRequest = resolve }),
      1000,
      {
        onData: state => received.push(state),
        onError: error => { throw error },
      },
      {
        setTimeout: callback => {
          timers.push(callback)
          return callback
        },
        clearTimeout: () => undefined,
      },
    )

    const request = poller.start()
    await flush()
    poller.stop()
    resolveRequest({ status: 'running' })
    await request

    expect(received).toEqual([])
    expect(timers).toEqual([])
  })

  it('resumes with one catch-up request after a hidden-tab stop', async () => {
    let calls = 0
    const timers: Array<() => void> = []
    const poller = new AdaptivePoller(
      async () => ({ calls: ++calls }),
      1000,
      {
        onData: () => undefined,
        onError: error => { throw error },
      },
      {
        setTimeout: callback => {
          timers.push(callback)
          return callback
        },
        clearTimeout: handle => {
          const index = timers.indexOf(handle as () => void)
          if (index >= 0) timers.splice(index, 1)
        },
      },
    )

    await poller.start()
    expect(calls).toBe(1)
    expect(timers).toHaveLength(1)

    poller.stop()
    expect(timers).toHaveLength(0)

    await poller.start()
    expect(calls).toBe(2)
    expect(timers).toHaveLength(1)
  })

  it('lets manual refresh restart polling after the error callback marks it inactive', async () => {
    const timers: Array<() => void> = []
    let calls = 0
    const received: Array<{ status: string }> = []
    const poller = new AdaptivePoller(
      async () => {
        calls += 1
        if (calls === 1) throw Object.assign(new Error('资源不存在'), { status: 404 })
        return { status: 'running' }
      },
      1000,
      {
        onData: value => received.push(value),
        onError: () => false,
      },
      {
        setTimeout: callback => {
          timers.push(callback)
          return callback
        },
        clearTimeout: () => undefined,
      },
    )

    await poller.start()

    expect(calls).toBe(1)
    expect(timers).toEqual([])

    await poller.refresh()

    expect(calls).toBe(2)
    expect(received).toEqual([{ status: 'running' }])
    expect(timers).toHaveLength(1)
  })

  it('does not let refresh reactivate a poller after an explicit stop', async () => {
    let calls = 0
    const poller = new AdaptivePoller(
      async () => ({ calls: ++calls }),
      1000,
      {
        onData: () => undefined,
        onError: error => { throw error },
      },
    )

    await poller.start()
    poller.stop()
    await poller.refresh()

    expect(calls).toBe(1)
  })
})
