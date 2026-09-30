import { compressSegmentIndexes } from '../../lib/segmentIndexes'

/**
 * 未确定指代的称谓（asset_manifest.unresolved_appellations，2.0.8+ 字段）。
 *
 * 叙述向称谓归属（app/production/prep_pack/appellation_resolve.py）判定原文
 * 证据不足以确定这个称谓具体指谁——包括代词指代不明，也包括"可能就是人物谱里
 * 已登记的某个人，但原文没有能逐字对上号的依据"——时不再铸一个虚假的独立群演
 * 实体（那会让分镜台把它当成本段合法出场身份，制造"多出一个人"）。真实案例
 * （第2集）：「温老师」其实就是候选本人温念、「他」「有人」都曾被误判成独立
 * 群演。这里只是一份次要、紧凑的可见记录，供人工核查，不代表系统认定它是
 * 某个独立出镜的人；字段缺失或列表为空（旧产物、或本集确实没有这类称谓）时
 * 整块不渲染。
 */
export interface UnresolvedAppellationItem {
  label: string
  segment_indexes: number[]
}

export default function UnresolvedAppellations({
  items,
}: {
  items?: UnresolvedAppellationItem[]
}) {
  if (!items || !items.length) return null
  return (
    <details className="card prep-appellation-details">
      <summary className="prep-section-heading">
        未确定指代的称谓（不会作为独立人物进入分镜） · {items.length}
      </summary>
      <ul className="prep-timeline">
        {items.map((item, index) => (
          <li className="prep-timeline-item" key={`${item.label || 'unresolved'}-${index}`}>
            <span className="prep-timeline-marker" aria-hidden="true">
              {compressSegmentIndexes(item.segment_indexes || []) || '-'}
            </span>
            <div className="prep-timeline-details">
              <span className="prep-timeline-headline">「{item.label || '未命名称谓'}」</span>
            </div>
          </li>
        ))}
      </ul>
    </details>
  )
}
