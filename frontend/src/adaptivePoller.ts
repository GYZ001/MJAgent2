export type PollInterval<T> = number | ((data: T | null) => number)

type PollClock = {
  setTimeout: (callback: () => void, delayMs: number) => unknown
  clearTimeout: (handle: unknown) => void
}

type PollCallbacks<T> = {
  onData: (data: T) => void
  onError: (error: unknown) => void | boolean
}

const defaultClock: PollClock = {
  setTimeout: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  clearTimeout: handle => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
}

/**
 * 单飞、自适应轮询循环。
 *
 * 关键行为：即使当前数据让动态间隔返回 0，手动 refresh 后也会重新计算间隔。
 * 因此资源从 idle 变成 running 时，无需刷新页面就能唤醒后续轮询。
 */
export class AdaptivePoller<T> {
  private fetcher: () => Promise<T>
  private interval: PollInterval<T>
  private callbacks: PollCallbacks<T>
  private readonly clock: PollClock
  private data: T | null = null
  private started = false
  private active = false
  private generation = 0
  private timer: unknown
  private inFlight: Promise<T | null> | null = null

  constructor(
    fetcher: () => Promise<T>,
    interval: PollInterval<T>,
    callbacks: PollCallbacks<T>,
    clock: PollClock = defaultClock,
  ) {
    this.fetcher = fetcher
    this.interval = interval
    this.callbacks = callbacks
    this.clock = clock
  }

  update(
    fetcher: () => Promise<T>,
    interval: PollInterval<T>,
    callbacks: PollCallbacks<T>,
  ) {
    this.fetcher = fetcher
    this.interval = interval
    this.callbacks = callbacks
  }

  start(): Promise<T | null> {
    this.started = true
    this.active = true
    const staleRequest = this.inFlight
    if (staleRequest) {
      return staleRequest.then(() => {
        if (!this.started) return this.data
        return this.refresh()
      })
    }
    return this.refresh()
  }

  stop() {
    this.started = false
    this.active = false
    this.generation += 1
    this.clearTimer()
  }

  refresh(): Promise<T | null> {
    if (!this.started) return Promise.resolve(this.data)
    this.active = true
    if (this.inFlight) return this.inFlight

    const generation = this.generation
    const request = Promise.resolve()
      .then(() => this.fetcher())
      .then(data => {
        if (!this.active || generation !== this.generation) return this.data
        this.data = data
        this.callbacks.onData(data)
        return data
      })
      .catch((error: unknown) => {
        if (this.active && generation === this.generation) {
          const keepPolling = this.callbacks.onError(error)
          if (keepPolling === false) {
            this.active = false
            this.clearTimer()
          }
        }
        return null
      })
      .finally(() => {
        if (this.inFlight !== request) return
        this.inFlight = null
        if (this.active) this.scheduleNext()
      })

    this.inFlight = request
    return request
  }

  private scheduleNext() {
    this.clearTimer()
    const delayMs = typeof this.interval === 'function'
      ? this.interval(this.data)
      : this.interval
    if (delayMs <= 0) return
    this.timer = this.clock.setTimeout(() => {
      this.timer = undefined
      void this.refresh()
    }, delayMs)
  }

  private clearTimer() {
    if (this.timer === undefined) return
    this.clock.clearTimeout(this.timer)
    this.timer = undefined
  }
}
