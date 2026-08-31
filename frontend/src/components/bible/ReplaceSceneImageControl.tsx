import { useRef, useState } from 'react'
import { api, ApiError } from '../../api'

/** 「场景设定与重绘」弹窗内的替换场景图控件：用户上传的图片直接替换当前场景图，
 * 旧图归档到负数 ep_start 历史槽位，可用返回的 rollback_url 对应的
 * manual-rollback 端点撤销——场景侧独立实现（不是多视角候选采纳流水线，见
 * 后端 manual_scene.py 模块 docstring），但前端交互与角色侧保持一致。两条
 * 必须如实告知的事项固定展示在按钮下方，不是只在出错时才出现。 */
export default function ReplaceSceneImageControl({
  projectId, sceneName, onChanged,
}: {
  projectId: string
  sceneName: string
  onChanged: () => void
}) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ message: string; sceneReferenceId?: string } | null>(null)

  const pickFile = () => fileRef.current?.click()

  const rollback = async () => {
    if (!notice?.sceneReferenceId || submitting) return
    setSubmitting(true)
    try {
      await api.rollbackManualSceneImage(projectId, sceneName, notice.sceneReferenceId)
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
      const result = await api.replaceSceneImage(projectId, sceneName, form)
      setNotice({
        message: [result.style_warning, result.downstream_notice].filter(Boolean).join(' '),
        sceneReferenceId: result.previous_scene_reference_id ?? undefined,
      })
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '替换场景图失败，请稍后重试')
    } finally {
      setSubmitting(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  return (
    <div style={{ marginTop: 10 }}>
      <label className="f">用图片替换当前场景图</label>
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
          {notice.sceneReferenceId && (
            <button type="button" className="btn small" disabled={submitting} onClick={() => void rollback()}>
              撤销这次替换
            </button>
          )}
        </div>
      )}
    </div>
  )
}
