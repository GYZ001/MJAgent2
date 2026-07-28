import { useMemo, useState } from 'react'

/** 递归渲染 JSON：对象/数组可折叠，支持语法高亮与一键复制。 */
export default function JsonViewer({
  data,
  raw,
  collapsed = false,
  maxHeight = '45vh',
}: {
  data?: unknown
  raw?: string
  collapsed?: boolean
  maxHeight?: string
}) {
  const [expanded, setExpanded] = useState(!collapsed)
  const parsed = useMemo(() => {
    if (data !== undefined) return data
    if (!raw) return null
    try {
      return JSON.parse(raw)
    } catch {
      return undefined
    }
  }, [data, raw])

  const rawText = useMemo(() => {
    if (raw) return raw
    try {
      return JSON.stringify(data, null, 2)
    } catch {
      return String(data)
    }
  }, [data, raw])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(rawText)
    } catch {
      // ignore
    }
  }

  if (parsed === undefined) {
    return (
      <div className="json-viewer">
        <div className="json-viewer-toolbar">
          <span className="json-viewer-badge">原始文本</span>
          <button type="button" onClick={copy}>复制</button>
        </div>
        <pre style={{ maxHeight }}>{rawText}</pre>
      </div>
    )
  }

  return (
    <div className="json-viewer">
      <div className="json-viewer-toolbar">
        <button
          type="button"
          className="json-viewer-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded(v => !v)}
        >
          {expanded ? '收起' : '展开'}
        </button>
        <button type="button" onClick={copy}>复制</button>
      </div>
      {expanded && (
        <div className="json-viewer-body" style={{ maxHeight }}>
          <JsonNode value={parsed} depth={0} />
        </div>
      )}
    </div>
  )
}

function JsonNode({ value, depth }: { value: unknown; depth: number }) {
  if (value === null) return <span className="json-null">null</span>
  if (typeof value === 'boolean') return <span className="json-bool">{String(value)}</span>
  if (typeof value === 'number') return <span className="json-number">{String(value)}</span>
  if (typeof value === 'string') return <span className="json-string">"{value}"</span>

  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="json-punctuation">[]</span>
    return <JsonCollapsible label={`Array(${value.length})`} depth={depth} open={depth < 2}>
      {value.map((item, i) => (
        <div className="json-row" key={i}>
          <span className="json-key">{i}</span>
          <span className="json-punctuation">: </span>
          <JsonNode value={item} depth={depth + 1} />
        </div>
      ))}
    </JsonCollapsible>
  }

  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    if (entries.length === 0) return <span className="json-punctuation">{'{}'}</span>
    return (
      <JsonCollapsible label={`Object(${entries.length})`} depth={depth} open={depth < 2}>
        {entries.map(([k, v]) => (
          <div className="json-row" key={k}>
            <span className="json-key">"{k}"</span>
            <span className="json-punctuation">: </span>
            <JsonNode value={v} depth={depth + 1} />
          </div>
        ))}
      </JsonCollapsible>
    )
  }

  return <span className="json-string">{String(value)}</span>
}

function JsonCollapsible({
  label,
  depth,
  open,
  children,
}: {
  label: string
  depth: number
  open: boolean
  children: React.ReactNode
}) {
  const [isOpen, setIsOpen] = useState(open)
  const indent = { paddingLeft: `${depth * 16}px` }
  return (
    <div className="json-collapsible">
      <button
        type="button"
        className="json-node-toggle"
        style={indent}
        aria-expanded={isOpen}
        onClick={() => setIsOpen(v => !v)}
      >
        <span className="json-punctuation">{isOpen ? '▼' : '▶'}</span>
        <span className="json-node-label">{label}</span>
      </button>
      {isOpen && <div className="json-children">{children}</div>}
    </div>
  )
}
