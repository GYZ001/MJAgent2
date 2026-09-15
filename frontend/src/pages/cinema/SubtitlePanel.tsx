import { extraSpeechRows, missingSubtitleRows, subtitleSummaryLine } from './subtitleSummary'

interface SubtitlePanelProps {
  /** `MixStatus.final_edit_report`；可能为 null，也可能是旧格式（无 subtitles 键）。 */
  report: unknown
  /** `MixStatus.subtitle_srt_url`；没有可下载文件时为 null/undefined。 */
  srtUrl?: string | null
  episodeNo: number
}

/**
 * 成片台字幕对齐结果面板：一行摘要 + 未出声台词列表（带出路文案）+ 模型多念
 * 语音折叠区 + srt 下载。任意一段没有数据就不渲染对应区块；summary 为 null
 * （报告里根本没有 subtitles 键，或字段畸形）时整个面板不渲染，不展示半截
 * 或编造的信息。
 */
export default function SubtitlePanel({ report, srtUrl, episodeNo }: SubtitlePanelProps) {
  const summary = subtitleSummaryLine(report)
  if (!summary) return null
  const missing = missingSubtitleRows(report)
  const extra = extraSpeechRows(report)

  return (
    <div className="cinema-subtitle-panel">
      <p className="hint" role="status">{summary}</p>
      {missing.length > 0 && (
        <div className="cinema-subtitle-missing">
          <p className="hint">
            这些句子没有烧进字幕；去生成台重新生成对应镜头后重新合成即可补上
          </p>
          <table className="ledger cinema-subtitle-table">
            <thead>
              <tr>
                <th>镜号</th>
                <th>句号</th>
                <th>原话</th>
                <th>命中率</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              {missing.map(row => (
                <tr key={`${row.shotNo}-${row.utteranceId}`}>
                  <td>第 {row.shotNo} 镜</td>
                  <td>{row.utteranceId}</td>
                  <td>{row.line}</td>
                  <td>{row.ratioText}</td>
                  <td>{row.reasonLabel}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {extra.length > 0 && (
        <details className="cinema-subtitle-extra">
          <summary>模型多念的语音（{extra.length} 段）</summary>
          <ul>
            {extra.map((row, index) => (
              <li key={index}>第 {row.shotNo} 镜 · {row.range} · {row.text}</li>
            ))}
          </ul>
        </details>
      )}
      {srtUrl && (
        <a className="btn small cinema-subtitle-download" href={srtUrl} download={`episode-${episodeNo}.srt`}>
          下载字幕 .srt
        </a>
      )}
    </div>
  )
}
