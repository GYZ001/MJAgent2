import { useEffect, useRef, useState } from 'react'
import Markdown from './Markdown'

/**
 * 助手思考过程：默认折叠；流式进行中自动展开并显示打字光标，本轮结束后自动折叠。
 * 用户手动展开/收起后以用户意图为准，不再被自动切换。
 */
export default function ThinkingBlock({
  text,
  streaming,
}: {
  text: string
  streaming: boolean
}) {
  const [open, setOpen] = useState(streaming)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const touched = useRef(false)
  const wasStreaming = useRef(streaming)
  const startedAt = useRef(Date.now())

  useEffect(() => {
    if (touched.current) return
    // 未被用户干预时：开始流式→展开；本轮结束（streaming 由 true 变 false）→折叠。
    if (streaming) setOpen(true)
    else if (wasStreaming.current) {
      setOpen(false)
      setElapsedSeconds(Math.max(1, Math.round((Date.now() - startedAt.current) / 1000)))
    }
    wasStreaming.current = streaming
  }, [streaming])

  useEffect(() => {
    if (!streaming) return
    const update = () => setElapsedSeconds(Math.floor((Date.now() - startedAt.current) / 1000))
    update()
    const timer = window.setInterval(update, 1000)
    return () => window.clearInterval(timer)
  }, [streaming])

  if (!text.trim() && !streaming) return null

  return (
    <div className={`agent-thinking ${open ? 'open' : ''}`}>
      <button
        type="button"
        className="agent-thinking-toggle"
        aria-expanded={open}
        onClick={() => { touched.current = true; setOpen(o => !o) }}
      >
        <span>{streaming ? '正在思考' : `已思考${elapsedSeconds ? ` ${elapsedSeconds} 秒` : ''}`}</span>
        {streaming && <span className="agent-thinking-dots" aria-hidden="true"><i /><i /><i /></span>}
        <svg className="agent-thinking-chevron" viewBox="0 0 16 16" aria-hidden="true">
          <path d="m4 6 4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && Boolean(text.trim()) && (
        <div className="agent-thinking-body">
          <Markdown text={text} />
          {streaming && <span className="agent-caret" aria-hidden="true" />}
        </div>
      )}
    </div>
  )
}
