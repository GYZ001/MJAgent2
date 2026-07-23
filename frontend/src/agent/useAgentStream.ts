import { useCallback, useEffect, useRef, useState } from 'react'
import type { AgentStreamEvent } from './types'

/** SSE 消费：支持 Last-Event-ID 续传，断线不自动重发命令。 */
export function useAgentStream(turnId: string | null, enabled: boolean) {
  const [events, setEvents] = useState<AgentStreamEvent[]>([])
  const [status, setStatus] = useState<'idle' | 'connecting' | 'open' | 'closed' | 'error'>('idle')
  const lastIdRef = useRef(0)
  const esRef = useRef<EventSource | null>(null)

  const reset = useCallback(() => {
    esRef.current?.close()
    esRef.current = null
    setEvents([])
    lastIdRef.current = 0
    setStatus('idle')
  }, [])

  useEffect(() => {
    if (!turnId || !enabled) return
    setStatus('connecting')
    const url = `/api/agent/turns/${encodeURIComponent(turnId)}/events`
    const es = new EventSource(url)
    esRef.current = es

    es.onopen = () => setStatus('open')
    es.onerror = () => {
      // 浏览器会自动重连；用 Last-Event-ID 续传由服务端处理
      setStatus(prev => (prev === 'closed' ? prev : 'connecting'))
    }

    const onAny = (ev: MessageEvent) => {
      try {
        const data = JSON.parse(ev.data) as AgentStreamEvent
        if (typeof data.event_id === 'number' && data.event_id <= lastIdRef.current) return
        lastIdRef.current = data.event_id
        setEvents(prev => [...prev, data])
        if (data.event_type === 'turn.completed' || data.event_type === 'turn.cancelled') {
          setStatus('closed')
          es.close()
        }
      } catch {
        /* ignore malformed */
      }
    }

    // 默认 message 与具名事件都接入
    es.onmessage = onAny
    for (const name of [
      'turn.started', 'assistant.delta', 'plan.updated', 'tool.proposed',
      'approval.required', 'tool.started', 'tool.progress', 'run.linked',
      'tool.completed', 'tool.failed', 'ui.intent', 'turn.completed', 'turn.cancelled',
    ]) {
      es.addEventListener(name, onAny as EventListener)
    }

    return () => {
      es.close()
      esRef.current = null
    }
  }, [turnId, enabled])

  return { events, status, reset, lastEventId: lastIdRef.current }
}
