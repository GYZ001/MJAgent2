import { useLayoutEffect, useRef } from 'react'

export default function AgentComposer({
  value,
  onChange,
  onSend,
  onStop,
  disabled,
  stopping,
  statusMessage,
}: {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  onStop: () => void
  disabled?: boolean
  stopping?: boolean
  statusMessage?: string
}) {
  const inputRef = useRef<HTMLTextAreaElement | null>(null)

  useLayoutEffect(() => {
    const input = inputRef.current
    if (!input) return
    input.style.height = 'auto'
    const nextHeight = Math.min(input.scrollHeight, 160)
    input.style.height = `${nextHeight}px`
    input.style.overflowY = input.scrollHeight > 160 ? 'auto' : 'hidden'
  }, [value])

  const actionDisabled = !stopping && (Boolean(disabled) || !value.trim())
  const disabledReason = stopping
    ? ''
    : disabled
      ? statusMessage || '正在发送上一条消息'
      : !value.trim()
        ? '请先输入消息'
        : ''

  return (
    <div className="agent-composer">
      <div className="agent-composer-box">
        <textarea
          ref={inputRef}
          className="agent-input"
          rows={1}
          placeholder={statusMessage || '给案头助手发消息'}
          aria-label="消息内容"
          aria-describedby={statusMessage ? 'agent-composer-status' : undefined}
          value={value}
          disabled={disabled}
          onChange={e => onChange(e.target.value)}
          onKeyDown={e => {
            if (
              e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing &&
              !disabled && !stopping && value.trim()
            ) {
              e.preventDefault()
              onSend()
            }
          }}
        />
        <button
          type="button"
          className={`agent-send-button ${stopping ? 'stopping' : ''}`}
          aria-label={stopping ? '停止生成' : disabledReason ? `发送消息，暂不可用：${disabledReason}` : '发送消息'}
          title={stopping ? '停止生成' : disabledReason || '发送消息'}
          disabled={actionDisabled}
          onClick={stopping ? onStop : onSend}
        >
          {stopping ? (
            <span className="agent-stop-icon" aria-hidden="true" />
          ) : (
            <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
              <path d="M10 15V5m0 0L6 9m4-4 4 4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          )}
        </button>
      </div>
      {statusMessage && (
        <p id="agent-composer-status" className="agent-composer-status" role="status">
          {statusMessage}
        </p>
      )}
    </div>
  )
}
