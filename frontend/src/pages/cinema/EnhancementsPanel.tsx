import { enhancementFeatureSummaries } from './enhancementsSummary'

interface EnhancementsPanelProps {
  /** `MixStatus.final_edit_report`；可能没有 `enhancements` 键（旧报告）。 */
  report: unknown
}

/**
 * 成片合成三项增强（统一配乐/片头预告/主角内心独白）展示区块：逐项
 * applied/skipped + 中文原因 + 计划内容，复用字幕报告面板同一套「任意一段
 * 没有数据就不渲染」约定（`SubtitlePanel.tsx`）。
 */
export default function EnhancementsPanel({ report }: EnhancementsPanelProps) {
  const summaries = enhancementFeatureSummaries(report)
  if (summaries.length === 0) return null

  return (
    <div className="cinema-enhancements-panel">
      {summaries.map(summary => (
        <div key={summary.key} className={`cinema-enhancement-item ${summary.applied ? 'applied' : 'skipped'}`}>
          <p className="hint">
            {summary.label}：{summary.applied ? '已应用' : `未应用（${summary.reason}）`}
          </p>
          {summary.details.length > 0 && (
            <ul>
              {summary.details.map((detail, index) => (
                <li key={index}>{detail}</li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  )
}
