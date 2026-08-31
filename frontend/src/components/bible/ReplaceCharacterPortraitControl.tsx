import { useRef, useState } from 'react'
import { api, ApiError } from '../../api'

/** 「角色设定与生成参数」弹窗内的替换定妆照控件：用户上传的图片直接替换当前
 * 定妆照，旧图归档到负数 ep_start 历史槽位（后端 promote_staged_initial_
 * portrait 已有形态），可用 rollback_url 指向的既有回滚端点撤销——不新开一套
 * 回滚 UI，替换成功后的提示文案里直接给出撤销按钮。两条必须如实告知的事项
 * （不受统一画风约束、下游已生成产物不会自动重做）固定展示在按钮下方，不是
 * 只在出错时才出现。 */
export default function ReplaceCharacterPortraitControl({
  projectId, characterName, onChanged,
}: {
  projectId: string
  characterName: string
  onChanged: () => void
}) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ message: string; rollbackUrl?: string; portraitId?: string } | null>(null)

  const pickFile = () => fileRef.current?.click()

  const rollback = async () => {
    if (!notice?.portraitId || submitting) return
    setSubmitting(true)
    try {
      await api.rollbackPortraitCandidate(projectId, characterName, notice.portraitId)
      setNotice(null)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '撤销替换失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  const onFileChosen = async (file: File | undefined) => {
    if (!file || submitting) return
    setSubmitting(true)
    setError(null)
    setNotice(null)
    try {
      const form = new FormData()
      form.append('image', file)
      const result = await api.replaceCharacterPortraitImage(projectId, characterName, form)
      setNotice({
        message: [result.style_warning, result.downstream_notice].filter(Boolean).join(' '),
        rollbackUrl: result.rollback_url,
        portraitId: result.portrait_id,
      })
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '替换定妆照失败，请稍后重试')
    } finally {
      setSubmitting(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  return (
    <div style={{ marginTop: 10 }}>
      <label className="f">用图片替换当前定妆照</label>
      <p className="hint">上传的图片不受项目统一画风约束；替换只影响之后的新产出，此前已生成的分镜/视频仍用旧图，不会自动重做。</p>
      <input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" style={{ display: 'none' }}
        onChange={event => { void onFileChosen(event.target.files?.[0]) }} />
      <button type="button" className="btn small" disabled={submitting} onClick={pickFile}>
        {submitting ? '处理中…' : '选择图片替换'}
      </button>
      {error && <p className="review-impact danger">{error}</p>}
      {notice && (
        <div className="review-impact">
          <p>{notice.message}</p>
          {notice.rollbackUrl && (
            <button type="button" className="btn small" disabled={submitting} onClick={() => void rollback()}>
              撤销这次替换
            </button>
          )}
        </div>
      )}
    </div>
  )
}
