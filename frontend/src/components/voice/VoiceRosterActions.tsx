import { useState } from 'react'
import { ApiError } from '../../api'
import { generateMissingVoices } from '../../api/voices'
import DecisionDialog from '../DecisionDialog'
import { useProjectVoices } from './useProjectVoices'

function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  return err instanceof Error ? err.message : '批量生成失败，请稍后重试'
}

const MAX_NAMES_SHOWN = 6

/**
 * 人物谱工具栏的批量声音入口（方案 5.4 节）：`voice_model_configured` 为 false
 * 时没有任何生成能力可用，改成显示横幅并给出去模型中心的入口——不能把用户晾
 * 在原地（CLAUDE.md「拦住用户时必须给出路」）。已配置时只在存在缺口（没有当前
 * 声音也没在生成中的角色）时才显示按钮，缺口清空后自动收起，不留一个点了没用
 * 的按钮。
 */
export default function VoiceRosterActions({ projectId }: { projectId: string }) {
  const { data, refresh } = useProjectVoices(projectId)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!data) return null
  if (!data.voice_model_configured) {
    return (
      <div className="voice-config-banner" role="status">
        还没有配置声音生成模型，<a href="/system/models">去模型中心配置</a>
      </div>
    )
  }

  const missing = data.items.filter(item => !item.current && !item.generating)
  if (!missing.length) return null
  const names = missing.map(item => item.character_name)
  const namesText = names.length > MAX_NAMES_SHOWN
    ? `${names.slice(0, MAX_NAMES_SHOWN).join('、')} 等 ${names.length} 个角色`
    : names.join('、')

  const runGenerate = async () => {
    setConfirmOpen(false)
    setSubmitting(true)
    setError(null)
    try {
      await generateMissingVoices(projectId)
      refresh()
    } catch (err) {
      setError(describeError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <span className="voice-roster-actions">
      <button type="button" className="btn small" disabled={submitting} onClick={() => setConfirmOpen(true)}>
        {submitting ? '正在受理…' : `为未配置声音的角色生成（${missing.length} 个）`}
      </button>
      {error && <span className="voice-error">{error}</span>}
      {confirmOpen && (
        <DecisionDialog
          title="批量生成声音"
          summary={`将调用 ${missing.length} 次付费的声音生成接口`}
          message={`为以下角色各生成一版候选声音：${namesText}。`}
          confirmLabel="确认生成"
          cancelLabel="取消"
          onConfirm={() => void runGenerate()}
          onClose={() => setConfirmOpen(false)}
        />
      )}
    </span>
  )
}
