import { useId, useRef, useState } from 'react'
import { api, ApiError } from '../../api'
import { useFocusTrap } from '../../hooks/useFocusTrap'

const APPEARANCE_MIN = 20
const APPEARANCE_MAX = 80

function ManualCharacterForm({
  projectId, onClose, onAdded,
}: {
  projectId: string
  onClose: () => void
  onAdded: (name: string) => void
}) {
  const titleId = useId()
  const trapRef = useFocusTrap(true, onClose)
  const [name, setName] = useState('')
  const [appearance, setAppearance] = useState('')
  const [costume, setCostume] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const appearanceLen = appearance.trim().length
  const valid = name.trim() && appearanceLen >= APPEARANCE_MIN && appearanceLen <= APPEARANCE_MAX
    && costume.trim() && file

  const submit = async () => {
    if (!valid || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const form = new FormData()
      form.append('name', name.trim())
      form.append('appearance_canonical', appearance.trim())
      form.append('period_costume_canonical', costume.trim())
      form.append('image', file as File)
      const result = await api.addManualCharacter(projectId, form)
      onAdded(result.name)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '新增角色失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h3 id={titleId}>手动新增角色</h3>
        <p>名称、外观、服饰与定妆照全部由你填写和上传，不走模型判断。上传的图片不受项目统一画风约束，可能与其它镜头画风不一致。</p>
        <div className="review-impact">
          <label className="f">角色名称</label>
          <input type="text" value={name} disabled={submitting} maxLength={30}
            onChange={event => setName(event.target.value)} placeholder="例如：李富贵" />
          <label className="f">外观锚点（{APPEARANCE_MIN}~{APPEARANCE_MAX} 字，只写常规完整着装与体貌特征）</label>
          <textarea rows={3} value={appearance} disabled={submitting}
            onChange={event => setAppearance(event.target.value)}
            placeholder="例如：圆脸胖身，锦袍加身，蓄短须，常年笑意盈盈" />
          <p className="hint">
            {appearanceLen}/{APPEARANCE_MIN}~{APPEARANCE_MAX} 字
            {appearanceLen > 0 && appearanceLen < APPEARANCE_MIN
              && `，还差 ${APPEARANCE_MIN - appearanceLen} 字`}
            {appearanceLen > APPEARANCE_MAX && `，超出上限 ${appearanceLen - APPEARANCE_MAX} 字`}
          </p>
          <label className="f">年代服饰</label>
          <input type="text" value={costume} disabled={submitting} maxLength={80}
            onChange={event => setCostume(event.target.value)} placeholder="例如：常服布衣，粗麻材质，无现代元素" />
          <label className="f">定妆照</label>
          <input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" disabled={submitting}
            onChange={event => setFile(event.target.files?.[0] ?? null)} />
        </div>
        {error && <p className="review-impact danger">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" disabled={!valid || submitting} onClick={submit}>
            {submitting ? '提交中…' : '新增角色'}
          </button>
        </div>
      </section>
    </div>
  )
}

/** 人物谱页「手动新增角色」入口：工具栏挂一行即可，判据全部在
 * ManualCharacterForm 内部——名称/外观/服饰/定妆照全部用户提供，不走模型；
 * 去重、长度校验、上传校验均由后端把关，前端只原样显示后端给的真实报错。 */
export default function ManualCharacterDialog({
  projectId, onAdded,
}: {
  projectId: string
  onAdded: () => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className="btn small" onClick={() => setOpen(true)}>+ 手动添加角色</button>
      {open && (
        <ManualCharacterForm
          projectId={projectId}
          onClose={() => setOpen(false)}
          onAdded={() => { setOpen(false); onAdded() }}
        />
      )}
    </>
  )
}
