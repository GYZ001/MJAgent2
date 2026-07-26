import { useEffect, useMemo, useRef, useState } from 'react'

interface TimerRecord {
  startAt?: number
  lastMs?: number
  finishedAt?: number
}

function loadRecord(key: string): TimerRecord {
  try {
    return JSON.parse(window.localStorage.getItem(key) || '{}') as TimerRecord
  } catch {
    return {}
  }
}

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
  const endMs = finishedAt ? finishedAt * 1000 : clock
  const elapsedMs = Math.max(0, endMs - startedAt * 1000)
  return running
    ? <span className="task-timer"><b>{label}</b> 已执行 {formatDuration(elapsedMs)}</span>
    : <span className="task-timer done"><b>{label}</b> 本次耗时 {formatDuration(elapsedMs)}</span>
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
      const next = { startAt: Date.now() }
      saveRecord(storageKey, next)
      setRecord(next)
    }
  }, [active, record.startAt, storageKey])

  // 运行态进行中：标记「已观察到运行」，并每秒刷新计时
  useEffect(() => {
    if (!active || !record.startAt) return
    sawActive.current = true
    setNow(Date.now())
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [active, record.startAt])

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
    const next = { startAt: Date.now() }
    saveRecord(storageKey, next)
    setRecord(next)
    setNow(Date.now())
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

  const elapsedMs = record.startAt ? now - record.startAt : 0
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
