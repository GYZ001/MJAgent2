import { useState } from 'react'
import { api, type ReusedReason } from '../../api'
import { reusedReasonLabel } from '../../lib/providerTaskRecovery'

/** 带意见重拍（09-23 方案 P0-4）：意见走既有的 critique 追加通道
 *  （app/media_exec/enqueue_prompt.py::storyboard_pack_prompt_text），只在这一次生成里
 *  追加在段落末尾，不改台词、不回写分镜——下一次不带意见的生成会打回原样。定位是
 *  「主观不满意的轻量微调」，不是失败恢复的官方出路：供应商因台词原句拒收要走分镜台
 *  「修订台词」，因画面描述且想「以后都不再犯」要走分镜台「复核说话人和群演」，本组件
 *  不重复、不取代这两条路（2026-09-21 b92df7a1 的教训：拦住用户却给不出真实可走的路）。
 *  不用模态弹窗（2026-08-29 已下线生成前确认弹窗），改成不拦截的行内展开区。 */

const MAX_CRITIQUE_LENGTH = 200

function newIdemKey(prefix: string): string {
  const rand = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : Math.random().toString(16).slice(2)
  return `${prefix}:${rand}`
}

export default function CritiqueRetakePanel({
  shotId,
  disabled,
  qualificationVersion,
  onToast,
  onRefresh,
}: {
  shotId: string
  /** 复用 GenerationPanel 已算好的生成资格判定（segmentGenerateDisabledReason），
   *  本组件不重复判定资格。 */
  disabled: boolean
  qualificationVersion?: string
  onToast: (message: string, isErr?: boolean) => void
  onRefresh: () => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const submit = async () => {
    const trimmed = text.trim()
    // disabled 是父组件按最新生成资格算出的（可能在面板展开后才变为 true，
    // 例如旁边「重新生成」按钮先把该镜头打成 running）；展开态必须继续遵守它，
    // 否则会在已有任务处理中时又提交一次。
    if (!trimmed || submitting || disabled) return
    setSubmitting(true)
    try {
      const result = await api.shotGenerate(
        shotId, undefined, false, [trimmed], qualificationVersion,
        newIdemKey(`wall-critique-retake:${shotId}`),
      ) as { reused?: boolean; reused_reason?: ReusedReason; job_id?: string }
      // reused=true 的三种真实成因（已有交付版本复用/仍在处理中/卡住待人工）用
      // 与「重新生成」按钮相同的诚实转述，不自造一句只覆盖其中一种情形的文案。
      onToast(result.reused
        ? reusedReasonLabel(result.reused_reason)
        : `已提交带意见重拍${result.job_id ? `，任务 ${result.job_id}` : ''}`)
      setText('')
      setOpen(false)
      await onRefresh()
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), true)
    } finally {
      setSubmitting(false)
    }
  }

  if (!open) {
    return (
      <button type="button" className="btn wall-critique-toggle" disabled={disabled}
        onClick={() => setOpen(true)}>
        带意见重拍
      </button>
    )
  }

  return (
    <div className="wall-critique-panel" aria-label="带意见重拍">
      <label htmlFor={`critique-${shotId}`}>这一版哪里不满意？</label>
      <textarea id={`critique-${shotId}`} value={text} maxLength={MAX_CRITIQUE_LENGTH}
        disabled={submitting} placeholder="例如：光线再暗一点、镜头运动更慢"
        onChange={event => setText(event.target.value)} />
      <p className="wall-hint">
        会消耗一次视频生成额度（约 15 秒）；意见原样发给视频模型，措辞过激可能被内容审核拒收；
        只影响这一次生成，不改台词、不写回分镜。
      </p>
      {disabled && <p className="wall-hint" role="status">生成资格刚刚发生变化，暂不能提交，请稍后再试。</p>}
      <div className="wall-critique-actions">
        <button type="button" className="btn primary wall-critique-submit"
          disabled={submitting || !text.trim() || disabled} onClick={() => submit()}>
          {submitting ? '提交中…' : '提交重拍'}
        </button>
        <button type="button" className="btn wall-critique-cancel" disabled={submitting}
          onClick={() => { setOpen(false); setText('') }}>
          取消
        </button>
      </div>
    </div>
  )
}
