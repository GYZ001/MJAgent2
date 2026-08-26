import { useState } from 'react'
import { api, ApiError, TextModelChoice } from '../api'

export type StageTextModelField = 'bible_text_provider' | 'script_text_provider' | 'board_text_provider'

type Props = {
  projectId: string
  field: StageTextModelField
  /** 显示在下拉左侧的短标签，如「文本模型」。三个环节统一用同一个词，靠 title
   *  说明具体是哪个环节，避免用户把它和分镜台的「视频模型」搞混。 */
  label: string
  title: string
  value?: string | null
  choices: TextModelChoice[]
  disabled?: boolean
  toast: (message: string, isErr?: boolean) => void
  onSaved: () => void
}

/** 世界书/映射台/分镜台共用的分环节文本模型下拉。项目级设置：切换只影响该环节
 * 之后新发起的生成调用选哪个 provider，不作废已有产出，因此不需要二次确认——
 * 与分镜台「视频模型」的强绑定切换（会清空已生成产物）是两回事。 */
export default function StageTextModelPicker({
  projectId, field, label, title, value, choices, disabled, toast, onSaved,
}: Props) {
  const [busy, setBusy] = useState(false)
  const current = value || ''

  const submit = async (next: string) => {
    if (next === current) return
    setBusy(true)
    try {
      await api.put(`/projects/${projectId}/text-models`, { [field]: next })
      onSaved()
    } catch (caught) {
      const apiError = caught as ApiError
      toast(apiError.message || '切换文本模型失败', true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <label className="stage-model-picker-item" title={title}>
      <span>{label}</span>
      <select
        aria-label={title}
        disabled={disabled || busy}
        value={current}
        onChange={event => void submit(event.target.value)}
      >
        <option value="">系统默认</option>
        {choices.map(choice => (
          <option key={choice.provider} value={choice.provider}>{choice.label}</option>
        ))}
      </select>
    </label>
  )
}
