import { useCallback, useEffect, useRef, useState } from 'react'
import type { AgentStreamEvent } from './types'

let cachedSessionToken: string | null = null

async function sessionQuery(): Promise<string> {
  if (cachedSessionToken) return cachedSessionToken
  const resp = await fetch('/api/session')
  if (!resp.ok) throw new Error('无法领取本机会话凭证')
  const body = await resp.json() as { session_token?: string }
  cachedSessionToken = body.session_token || ''
  return cachedSessionToken
}

/** SSE 消费：支持 Last-Event-ID 续传，断线不自动重发命令。 */
export function useAgentStream(turnId: string | null, enabled: boolean) {
  const [stream, setStream] = useState<{ turnId: string | null; events: AgentStreamEvent[] }>({
    turnId: null,
    events: [],
  })
  const [status, setStatus] = useState<'idle' | 'connecting' | 'open' | 'closed' | 'error'>('idle')
  const lastIdRef = useRef(0)
  const esRef = useRef<EventSource | null>(null)
  const generationRef = useRef(0)

  const reset = useCallback(() => {
    generationRef.current += 1
    esRef.current?.close()
    esRef.current = null
    setStream({ turnId: null, events: [] })
    lastIdRef.current = 0
    setStatus('idle')
  }, [])

  useEffect(() => {
    if (!turnId || !enabled) return
    let cancelled = false
    const generation = ++generationRef.current
    lastIdRef.current = 0
    setStream({ turnId, events: [] })
    setStatus('connecting')

    ;(async () => {
      try {
        const token = await sessionQuery()
        if (cancelled) return
        const params = new URLSearchParams()
        if (token) params.set('session', token)
        if (lastIdRef.current) params.set('last_event_id', String(lastIdRef.current))
        const qs = params.toString()
        const url = `/api/agent/turns/${encodeURIComponent(turnId)}/events${qs ? `?${qs}` : ''}`
        const es = new EventSource(url)
        esRef.current = es

        es.onopen = () => setStatus('open')
        es.onerror = () => {
          setStatus(prev => (prev === 'closed' ? prev : 'connecting'))
        }

        const onAny = (ev: MessageEvent) => {
          if (cancelled || generation !== generationRef.current) return
          try {
            const data = JSON.parse(ev.data) as AgentStreamEvent
            if (data.turn_id && data.turn_id !== turnId) return
            // 事件序号在 SSE 的 `id:` 字段里（ev.lastEventId），不在 data 内。
            // 用它去重，防止断线重连时重复计入 token（否则流式文本会翻倍）。
            const eid = ev.lastEventId ? Number(ev.lastEventId) : NaN
            if (Number.isFinite(eid)) {
              if (eid <= lastIdRef.current) return
              lastIdRef.current = eid
              data.event_id = eid
            }
            setStream(prev => prev.turnId === turnId
              ? { turnId, events: [...prev.events, data] }
              : { turnId, events: [data] })
            if (data.event_type === 'turn.completed' || data.event_type === 'turn.cancelled') {
              setStatus('closed')
              es.close()
            }
          } catch {
            /* ignore malformed */
          }
        }

        es.onmessage = onAny
        for (const name of [
          'turn.started', 'assistant.delta', 'thinking.delta', 'plan.updated', 'tool.proposed',
          'approval.required', 'tool.started', 'tool.progress', 'run.linked',
          'tool.completed', 'tool.failed', 'ui.intent', 'turn.completed', 'turn.cancelled',
        ]) {
          es.addEventListener(name, onAny as EventListener)
        }
      } catch {
        if (!cancelled) setStatus('error')
      }
    })()

    return () => {
      cancelled = true
      esRef.current?.close()
      esRef.current = null
    }
  }, [turnId, enabled])

  return {
    events: stream.events,
    streamTurnId: stream.turnId,
    status,
    reset,
    lastEventId: lastIdRef.current,
  }
}
