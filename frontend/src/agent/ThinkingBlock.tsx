import { useEffect, useRef, useState } from 'react'

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
  const touched = useRef(false)
  const wasStreaming = useRef(streaming)

  useEffect(() => {
    if (touched.current) return
    // 未被用户干预时：开始流式→展开；本轮结束（streaming 由 true 变 false）→折叠。
    if (streaming) setOpen(true)
    else if (wasStreaming.current) setOpen(false)
    wasStreaming.current = streaming
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
        <span className="agent-thinking-caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span>💭 思考过程{streaming ? '…' : ''}</span>
      </button>
      {open && (
        <div className="agent-thinking-body">
          {text}
          {streaming && <span className="agent-caret" aria-hidden="true" />}
        </div>
      )}
    </div>
  )
}
