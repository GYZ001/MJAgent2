import type { SeriesExport } from '../../api'
import { formatGB } from './seriesTaskText'

/** 导出包列表：不是 zip——每个包落地为硬链接目录（零拷贝零占用），可逐个下载
 *  单个文件，也可以下载该包的「下载清单.txt」交给下载工具批量拉取。 */
export default function SeriesExportPanel({
  exports,
  loading,
  error,
}: {
  exports: SeriesExport[]
  loading: boolean
  error: string | null
}) {
  return (
    <section className="series-export-panel card">
      <h3>导出包</h3>
      <p className="series-export-hint">
        导出不会打包成压缩包：每个包落地为一份硬链接目录，下方按包展示文件数与合计大小，
        可逐个下载单个文件，也可以下载该包的「下载清单.txt」交给下载工具（如迅雷 /
        aria2 / wget -i）批量拉取。
      </p>
      {loading && <p>正在加载导出包…</p>}
      {error && <p role="alert" className="series-range-invalid">{error}</p>}
      {!loading && !exports.length && (
        <p className="series-empty">还没有导出包，勾选已完成的任务后点击「打包导出选中」。</p>
      )}
      <ul className="series-export-list">
        {exports.map(exp => (
          <li key={exp.export_id} className="series-export-item">
            <div className="series-export-item-head">
              <b>{new Date(exp.created_at * 1000).toLocaleString()}</b>
              <span>共 {exp.item_count} 个文件，合计 {formatGB(exp.total_size_bytes)}</span>
            </div>
            <div className="series-export-links">
              {exp.items.map(item => (
                <a key={item.task_id} className="btn small" href={item.url} download>{item.file_name}</a>
              ))}
              <a className="btn small" href={exp.list_url} download>下载清单.txt</a>
            </div>
            {exp.skipped.length > 0 && (
              <p className="series-export-skipped">
                未导出：{exp.skipped.map(s => `${s.task_id}（${s.reason}）`).join('、')}
              </p>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}
