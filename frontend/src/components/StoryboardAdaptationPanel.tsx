import { useEffect, useState } from 'react'
import { api, type StoryboardAdaptationSummary } from '../api'
import { adaptationModeLabel, adaptationPanelTitle, durationComparisonText } from '../lib/storyboardAdaptation'

/**
 * 分镜台「本集删减」面板（2026-09-23 用户拍板）：短剧节奏档声明的原文删减区间 +
 * 对白台账里的弃置台词，只读展示，不提供编辑入口。默认折叠，标题带计数，展开
 * 才看细节——分镜台本身信息已经很密，删减记录不是每次都要看。
 *
 * 数据源 GET /episodes/{id}/storyboard-adaptation 是纯读接口（不产出、不修改），
 * 老分集/忠实档没有改编留档时 recorded=false，但弃置台词仍可能非空。
 */
export default function StoryboardAdaptationPanel({ episodeId }: { episodeId: string }) {
  const [summary, setSummary] = useState<StoryboardAdaptationSummary | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setSummary(null)
    setError(null)
    api.getStoryboardAdaptation(episodeId)
      .then(res => { if (!cancelled) setSummary(res) })
      .catch(err => { if (!cancelled) setError(err instanceof Error ? err.message : String(err)) })
    return () => { cancelled = true }
  }, [episodeId])

  if (error) return <p className="query-error" role="alert">本集删减信息加载失败：{error}</p>
  if (!summary) return null

  const spanCount = summary.dropped_source_spans.length
  const lineCount = summary.dropped_lines.length
  const durationText = durationComparisonText(summary.segment_count, summary.target_duration_s)
  const hasDrops = spanCount > 0 || lineCount > 0

  return (
    <details className="storyboard-adaptation-toggle">
      <summary>{adaptationPanelTitle(spanCount, lineCount)}</summary>
      <div className="storyboard-adaptation-body">
        <p>档位：{adaptationModeLabel(summary)}</p>
        {durationText && <p>{durationText}</p>}
        {summary.over_target && <p role="status">模型多次调整后仍超出短剧上限</p>}
        {!hasDrops && <p>本集没有删减内容</p>}
        {spanCount > 0 && (
          <div>
            <b>删减原文 · {spanCount} 处</b>
            <ul className="storyboard-adaptation-drop-list">
              {summary.dropped_source_spans.map((span, index) => (
                <li key={`${span.chapter_idx}-${span.start_offset}-${index}`}>
                  <p>{span.excerpt}</p>
                  <small>第 {span.chapter_idx} 章 · {span.chars} 字 · {span.reason}</small>
                </li>
              ))}
            </ul>
          </div>
        )}
        {lineCount > 0 && (
          <div>
            <b>弃置台词 · {lineCount} 句</b>
            <ul className="storyboard-adaptation-drop-list">
              {summary.dropped_lines.map((line, index) => (
                <li key={`${line.quote_id}-${index}`}>
                  <p>{line.text}</p>
                  <small>{line.reason}</small>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </details>
  )
}
