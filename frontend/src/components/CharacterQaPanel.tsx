import { Portrait } from '../api'
import { statusLabel, statusTitle } from '../lib/statusLabels'

const VIEW_LABELS: Record<string, string> = {
  front_full: '正面全身',
  three_quarter: '3/4 面',
  profile: '侧面',
  back_full: '背面全身',
  face_closeup: '面部特写',
}

export default function CharacterQaPanel({
  characterName,
  portrait,
  onClose,
}: {
  characterName: string
  portrait: Portrait
  onClose: () => void
}) {
  const qa = portrait.group_qa
  const hard = qa?.hard_failures ?? []
  const soft = qa?.issues ?? []
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={e => {
      if (e.currentTarget === e.target) onClose()
    }}>
      <section className="impact-dialog character-qa-panel" role="dialog" aria-modal="true" aria-label="人物 QA 详情">
        <h3>{characterName} · 定妆 QA</h3>
        <ul>
          <li>包状态：<span title={statusTitle(portrait.pack_status || '')}>{statusLabel(portrait.pack_status)}</span></li>
          <li>整包结论：{qa?.status ? <span title={statusTitle(qa.status)}>{statusLabel(qa.status)}</span> : '未验证'}
            {typeof qa?.overall === 'number' ? ` · ${qa.overall.toFixed(2)}` : ''}</li>
          <li>脸一致性：{qa?.face_consistency ?? '—'}</li>
          <li>发型一致性：{qa?.hair_consistency ?? '—'}</li>
          <li>服装一致性：{qa?.outfit_consistency ?? '—'}</li>
        </ul>
        {!!hard.length && (
          <>
            <h4>硬失败</h4>
            <ul>{hard.map(item => <li key={item}>{item}</li>)}</ul>
          </>
        )}
        {!!soft.length && (
          <>
            <h4>警告</h4>
            <ul>{soft.map(item => <li key={item}>{item}</li>)}</ul>
          </>
        )}
        <h4>视角级结果</h4>
        <ul>
          {(qa?.views ?? portrait.views ?? []).map((view, index) => {
            const role = ('view_role' in view ? view.view_role : undefined) || `view-${index}`
            const overall = 'overall' in view ? view.overall : ('qa_overall' in view ? view.qa_overall : null)
            const issues = ('issues' in view ? view.issues : undefined) || []
            const fails = ('hard_failures' in view ? view.hard_failures : undefined) || []
            return (
              <li key={role}>
                {VIEW_LABELS[role || ''] || role}
                {typeof overall === 'number' ? ` · ${overall.toFixed(2)}` : ''}
                {fails?.length ? ` · 硬失败：${fails.join('；')}` : ''}
                {issues?.length ? ` · ${issues.slice(0, 2).join('；')}` : ''}
              </li>
            )
          })}
        </ul>
        <p className="hint">采用原因：硬门禁通过后自动采用当前包；失败包不切换下游引用。人工特批默认不可越过硬门禁。</p>
        <div className="dialog-actions">
          <button type="button" className="btn primary" onClick={onClose}>关闭</button>
        </div>
      </section>
    </div>
  )
}
