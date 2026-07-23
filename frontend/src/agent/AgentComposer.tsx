import { QUICK_PROMPTS } from './types'

export default function AgentComposer({
  value,
  onChange,
  onSend,
  onStop,
  disabled,
  stopping,
}: {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  onStop: () => void
  disabled?: boolean
  stopping?: boolean
}) {
  return (
    <div className="agent-composer">
      <div className="agent-quick">
        {QUICK_PROMPTS.map(prompt => (
          <button
            key={prompt}
            type="button"
            className="agent-chip"
            disabled={disabled}
            onClick={() => onChange(prompt)}
          >
            {prompt}
          </button>
        ))}
      </div>
      <textarea
        className="agent-input"
        rows={3}
        placeholder="描述你想做的制作动作…（勿发送 API Key）"
        value={value}
        disabled={disabled}
        onChange={e => onChange(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault()
            onSend()
          }
        }}
      />
      <div className="agent-composer-actions">
        <button type="button" className="btn" disabled={!stopping && !disabled} onClick={onStop}>
          停止本轮
        </button>
        <button type="button" className="btn primary" disabled={disabled || !value.trim()} onClick={onSend}>
          发送 ⌘↵
        </button>
      </div>
    </div>
  )
}
