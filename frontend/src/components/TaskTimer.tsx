import { useEffect, useMemo, useRef, useState } from 'react'

interface TimerRecord {
  startAt?: number
  lastMs?: number
  finishedAt?: number
  /** 心跳：运行态下定期写入。用于识别「页面被关掉后搁浅」的起点。 */
  seenAt?: number
}

/** 心跳落盘间隔；显示仍是每秒刷新，这里只是降低 localStorage 写入频率。 */
const HEARTBEAT_MS = 5_000
/** 心跳早于此阈值即视为搁浅记录。
 *  取 4 个心跳周期：足够覆盖一次页面刷新（秒级），又能把误差收敛到 20 秒内。 */
const STALE_MS = 20_000

/** 运行中刷新页面应当续算，而任务跑完后页面才被重开则必须丢弃旧起点。
 *  二者只能靠「上次心跳距今多久」区分：心跳随页面关闭而停止。 */
export function isStaleRecord(record: TimerRecord, now: number): boolean {
  if (!record.startAt) return false
  return now - (record.seenAt ?? record.startAt) > STALE_MS
}

function loadRecord(key: string): TimerRecord {
  try {
    const record = JSON.parse(window.localStorage.getItem(key) || '{}') as TimerRecord
    // 搁浅的起点会让下一个任务从旧时间累加（曾出现「已等待 1244 分」）。
    if (isStaleRecord(record, Date.now())) {
      const { startAt: _startAt, seenAt: _seenAt, ...rest } = record
      return rest
    }
    return record
  } catch {
    return {}
  }
}

/** 仅供测试：验证搁浅起点的清理契约。 */
export const loadRecordForTest = loadRecord

function saveRecord(key: string, record: TimerRecord) {
  window.localStorage.setItem(key, JSON.stringify(record))
}

function formatDuration(ms: number) {
  const total = Math.max(0, Math.floor(ms / 1000))
  const min = Math.floor(total / 60)
  const sec = total % 60
  return min ? `${min}分${String(sec).padStart(2, '0')}秒` : `${sec}秒`
}

export function ServerTaskTimer({ label, startedAt, finishedAt, running }: {
  label: string
  startedAt?: number | null
  finishedAt?: number | null
  running: boolean
}) {
  const [clock, setClock] = useState(Date.now())
  useEffect(() => {
    if (!running || !startedAt) return
    setClock(Date.now())
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [running, startedAt])
  if (!startedAt) return null
  // 已停止却没有服务端结束时间时，耗时无从得知：用挂载时刻当终点会让数字每次刷新都变大，
  // 宁可不显示，也不给一个会漂移的数。
  if (!running && !finishedAt) return null
  const endMs = finishedAt ? finishedAt * 1000 : clock
  const elapsedMs = Math.max(0, endMs - startedAt * 1000)
  return running
    ? <span className="task-timer"><b>{label}</b> 已执行 {formatDuration(elapsedMs)}</span>
    : <span className="task-timer done"><b>{label}</b> 本次耗时 {formatDuration(elapsedMs)}</span>
}

/** 单项计时：累计已完成耗时，若仍在跑则叠加实时增量。
 *
 *  用于逐镜分镜与逐条视频——它们的耗时由多次重试累加而成，语义与整模块的
 *  起止时间不同，不能复用 ServerTaskTimer。
 */
export function ItemTaskTimer({ elapsedMs, runningSince, iterations, compact = false }: {
  elapsedMs: number
  runningSince?: number | null
  iterations?: number
  compact?: boolean
}) {
  const [clock, setClock] = useState(Date.now())
  useEffect(() => {
    if (!runningSince) return
    setClock(Date.now())
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [runningSince])
  const running = Boolean(runningSince)
  const liveMs = running ? Math.max(0, clock - (runningSince as number) * 1000) : 0
  const totalMs = Math.max(0, elapsedMs) + liveMs
  if (!running && totalMs <= 0) return null
  const retries = Math.max(0, (iterations ?? 0) - 1)
  return (
    <span className={`task-timer item-timer${running ? '' : ' done'}`}>
      {running ? '已执行 ' : ''}{formatDuration(totalMs)}
      {!compact && retries > 0 && <small> · 重试 {retries} 次</small>}
    </span>
  )
}

export function useTaskTimer(key: string, active: boolean) {
  const storageKey = useMemo(() => `mjagent.timer.${key}`, [key])
  const [record, setRecord] = useState<TimerRecord>(() => loadRecord(storageKey))
  const [now, setNow] = useState(Date.now())
  // 是否真正观察到过运行态。只有从「运行中→结束」才记录耗时，避免点完 start() 但服务端状态
  // 还没翻成 running 的空窗期里，结束副作用立刻把计时清成 0（这正是「本次耗时 0 秒」的成因）。
  const sawActive = useRef(false)

  useEffect(() => {
    setRecord(loadRecord(storageKey))
    sawActive.current = false
  }, [storageKey])

  // 进入运行态：若还没开始计时则自动开始（人工 start() 只是提前给反馈，可有可无）
  useEffect(() => {
    if (active && !record.startAt) {
      const startAt = Date.now()
      const next = { startAt, seenAt: startAt }
      saveRecord(storageKey, next)
      setRecord(next)
    }
  }, [active, record.startAt, storageKey])

  // 运行态进行中：标记「已观察到运行」，每秒刷新计时，并定期落盘心跳
  useEffect(() => {
    if (!active || !record.startAt) return
    sawActive.current = true
    setNow(Date.now())
    let lastBeat = Date.now()
    saveRecord(storageKey, { ...record, seenAt: lastBeat })
    const t = window.setInterval(() => {
      const tick = Date.now()
      setNow(tick)
      if (tick - lastBeat >= HEARTBEAT_MS) {
        lastBeat = tick
        saveRecord(storageKey, { ...record, seenAt: tick })
      }
    }, 1000)
    return () => window.clearInterval(t)
    // record 只在 startAt 变化时需要重建心跳；其余字段变动不应重启计时器。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, record.startAt, storageKey])

  // 结束：只有真正运行过（sawActive）才记录本次耗时
  useEffect(() => {
    if (!active && record.startAt && sawActive.current) {
      const next = { lastMs: Date.now() - record.startAt, finishedAt: Date.now() }
      saveRecord(storageKey, next)
      setRecord(next)
      sawActive.current = false
    }
  }, [active, record.startAt, storageKey])

  const start = () => {
    sawActive.current = false
    const startAt = Date.now()
    const next = { startAt, seenAt: startAt }
    saveRecord(storageKey, next)
    setRecord(next)
    setNow(startAt)
  }

  const clear = () => {
    const next = {}
    saveRecord(storageKey, next)
    setRecord(next)
    sawActive.current = false
  }

  // 点了 start() 但服务端从未进入运行态（请求失败）：超时后清掉，避免下次耗时从旧时间累计
  useEffect(() => {
    if (active || !record.startAt || sawActive.current) return
    const t = window.setTimeout(() => {
      if (!sawActive.current) {
        const next = {}
        saveRecord(storageKey, next)
        setRecord(next)
      }
    }, 12000)
    return () => window.clearTimeout(t)
  }, [active, record.startAt, storageKey])

  const elapsedMs = record.startAt ? Math.max(0, now - record.startAt) : 0
  return {
    start,
    clear,
    running: active && !!record.startAt,
    elapsedMs,
    lastMs: record.lastMs,
  }
}

export function TaskTimer({ label, timer }: {
  label: string
  timer: ReturnType<typeof useTaskTimer>
}) {
  if (timer.running) {
    return <span className="task-timer"><b>{label}</b> 已等待 {formatDuration(timer.elapsedMs)}</span>
  }
  if (timer.lastMs !== undefined) {
    return <span className="task-timer done"><b>{label}</b> 本次耗时 {formatDuration(timer.lastMs)}</span>
  }
  return null
}
