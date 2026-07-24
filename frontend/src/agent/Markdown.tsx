import { type ReactNode } from 'react'

/**
 * 轻量 Markdown 渲染：段落、标题、有序/无序列表、围栏代码块、行内 `代码` 与 **粗体**。
 * 全部渲染为 React 元素（不使用 dangerouslySetInnerHTML），天然防 XSS。
 * 目的只是让助手正文可读，不追求完整 CommonMark。
 */

function renderInline(text: string, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = []
  // 交替匹配 `行内代码` 与 **粗体**；其余按纯文本输出。
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*)/g
  let last = 0
  let match: RegExpExecArray | null
  let i = 0
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index))
    const token = match[0]
    if (token.startsWith('`')) {
      nodes.push(<code key={`${keyBase}-c${i}`}>{token.slice(1, -1)}</code>)
    } else {
      nodes.push(<strong key={`${keyBase}-b${i}`}>{token.slice(2, -2)}</strong>)
    }
    last = match.index + token.length
    i += 1
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

export default function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  const blocks: ReactNode[] = []
  let i = 0
  let key = 0

  while (i < lines.length) {
    const line = lines[i]

    // 空行：跳过
    if (!line.trim()) { i += 1; continue }

    // 围栏代码块
    const fence = line.match(/^\s*```(.*)$/)
    if (fence) {
      const code: string[] = []
      i += 1
      while (i < lines.length && !/^\s*```/.test(lines[i])) { code.push(lines[i]); i += 1 }
      i += 1 // 跳过收尾 ```
      blocks.push(
        <pre key={`k${key++}`} className="md-pre"><code>{code.join('\n')}</code></pre>,
      )
      continue
    }

    // 标题
    const heading = line.match(/^(#{1,6})\s+(.*)$/)
    if (heading) {
      const level = Math.min(heading[1].length, 6)
      const Tag = (`h${Math.min(level + 2, 6)}`) as 'h3' | 'h4' | 'h5' | 'h6'
      blocks.push(<Tag key={`k${key++}`} className="md-h">{renderInline(heading[2], `k${key}`)}</Tag>)
      i += 1
      continue
    }

    // 无序列表
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ''))
        i += 1
      }
      blocks.push(
        <ul key={`k${key++}`} className="md-ul">
          {items.map((it, idx) => <li key={idx}>{renderInline(it, `k${key}-${idx}`)}</li>)}
        </ul>,
      )
      continue
    }

    // 有序列表
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ''))
        i += 1
      }
      blocks.push(
        <ol key={`k${key++}`} className="md-ol">
          {items.map((it, idx) => <li key={idx}>{renderInline(it, `k${key}-${idx}`)}</li>)}
        </ol>,
      )
      continue
    }

    // 段落：把连续非空、非特殊行合并，行内以 <br/> 断行
    const para: string[] = []
    while (
      i < lines.length && lines[i].trim() &&
      !/^\s*```/.test(lines[i]) && !/^(#{1,6})\s+/.test(lines[i]) &&
      !/^\s*[-*]\s+/.test(lines[i]) && !/^\s*\d+\.\s+/.test(lines[i])
    ) {
      para.push(lines[i]); i += 1
    }
    blocks.push(
      <p key={`k${key++}`} className="md-p">
        {para.map((pl, idx) => (
          <span key={idx}>
            {renderInline(pl, `k${key}-${idx}`)}
            {idx < para.length - 1 ? <br /> : null}
          </span>
        ))}
      </p>,
    )
  }

  return <div className="md">{blocks}</div>
}
