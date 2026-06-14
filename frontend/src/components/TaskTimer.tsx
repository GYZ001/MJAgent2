import { useEffect, useMemo, useState } from 'react'

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

export function useTaskTimer(key: string, active: boolean) {
  const storageKey = useMemo(() => `mjagent.timer.${key}`, [key])
  const [record, setRecord] = useState<TimerRecord>(() => loadRecord(storageKey))
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    setRecord(loadRecord(storageKey))
  }, [storageKey])

  useEffect(() => {
    if (!active || !record.startAt) return
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [active, record.startAt])

  useEffect(() => {
    if (!active && record.startAt) {
      const next = { lastMs: Date.now() - record.startAt, finishedAt: Date.now() }
      saveRecord(storageKey, next)
      setRecord(next)
    }
  }, [active, record.startAt, storageKey])

  const start = () => {
    const next = { startAt: Date.now() }
    saveRecord(storageKey, next)
    setRecord(next)
    setNow(Date.now())
  }

  const clear = () => {
    const next = {}
    saveRecord(storageKey, next)
    setRecord(next)
  }

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
