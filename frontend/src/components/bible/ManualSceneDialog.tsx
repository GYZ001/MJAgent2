import { useId, useState } from 'react'
import { api, ApiError } from '../../api'
import { useFocusTrap } from '../../hooks/useFocusTrap'

const SCENE_MIN = 30
const SCENE_MAX = 80

function ManualSceneForm({
  projectId, onClose, onAdded,
}: {
  projectId: string
  onClose: () => void
  onAdded: (name: string) => void
}) {
  const titleId = useId()
  const trapRef = useFocusTrap(true, onClose)
  const [name, setName] = useState('')
  const [canonical, setCanonical] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const canonicalLen = canonical.trim().length
  const valid = name.trim() && canonicalLen >= SCENE_MIN && canonicalLen <= SCENE_MAX && file

  const submit = async () => {
    if (!valid || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const form = new FormData()
      form.append('name', name.trim())
      form.append('scene_canonical', canonical.trim())
      form.append('image', file as File)
      const result = await api.addManualScene(projectId, form)
      onAdded(result.name)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '新增场景失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h3 id={titleId}>手动新增场景</h3>
        <p>名称、场景锚点与场景图全部由你填写和上传，不走模型判断。上传的图片不受项目统一画风约束，可能与其它镜头画风不一致。</p>
        <div className="review-impact">
          <label className="f">场景名称</label>
          <input type="text" value={name} disabled={submitting} maxLength={30}
            onChange={event => setName(event.target.value)} placeholder="例如：后山竹林" />
          <label className="f">场景锚点（{SCENE_MIN}~{SCENE_MAX} 字：地点/室内外/光线/陈设/氛围）</label>
          <textarea rows={3} value={canonical} disabled={submitting}
            onChange={event => setCanonical(event.target.value)}
            placeholder="例如：清晨薄雾笼罩的后山竹林，青石小径蜿蜒，晨光透过竹叶洒落，氛围清幽静谧" />
          <p className="hint">
            {canonicalLen}/{SCENE_MIN}~{SCENE_MAX} 字
            {canonicalLen > 0 && canonicalLen < SCENE_MIN && `，还差 ${SCENE_MIN - canonicalLen} 字`}
            {canonicalLen > SCENE_MAX && `，超出上限 ${canonicalLen - SCENE_MAX} 字`}
          </p>
          <label className="f">场景图</label>
          <input type="file" accept="image/jpeg,image/png,image/webp" disabled={submitting}
            onChange={event => setFile(event.target.files?.[0] ?? null)} />
        </div>
        {error && <p className="review-impact danger">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" disabled={!valid || submitting} onClick={submit}>
            {submitting ? '提交中…' : '新增场景'}
          </button>
        </div>
      </section>
    </div>
  )
}

/** 场景库页「手动新增场景」入口：工具栏挂一行即可，判据全部在
 * ManualSceneForm 内部——名称/场景锚点/场景图全部用户提供，不走模型；去重、
 * 长度校验、上传校验均由后端把关。 */
export default function ManualSceneDialog({
  projectId, onAdded,
}: {
  projectId: string
  onAdded: () => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className="btn small" onClick={() => setOpen(true)}>+ 手动添加场景</button>
      {open && (
        <ManualSceneForm
          projectId={projectId}
          onClose={() => setOpen(false)}
          onAdded={() => { setOpen(false); onAdded() }}
        />
      )}
    </>
  )
}
