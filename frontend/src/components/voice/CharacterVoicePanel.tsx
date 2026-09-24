import { useState } from 'react'
import { ApiError } from '../../api'
import {
  adoptCharacterVoice, generateCharacterVoice, suggestVoiceDescription, type VoiceVersion,
} from '../../api/voices'
import DecisionDialog from '../DecisionDialog'
import VoicePlayButton from './VoicePlayButton'
import { findVoiceItem, useProjectVoices } from './useProjectVoices'

function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  return err instanceof Error ? err.message : '操作失败，请稍后重试'
}

function newIdemKey(prefix: string): string {
  const rand = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : Math.random().toString(16).slice(2)
  return `${prefix}:${rand}`
}

/** 候选/当前声音一行的状态角标：生成态优先于校验态（还没生成完，校验结果无
 *  意义）；校验态三档对应方案 5.4 节「通过 / 未通过+原因 / 未校验」。复用全站
 *  既有的 .stamp 印章视觉（BiblePage 定妆照状态同款），不新造一套配色。 */
function CandidateBadge({ voice }: { voice: VoiceVersion }) {
  if (voice.status === 'generating') return <span className="stamp gold">生成中</span>
  if (voice.status === 'failed') {
    return <span className="stamp red" title={voice.error || undefined}>生成失败</span>
  }
  if (voice.check_status === 'passed') return <span className="stamp green">校验通过</span>
  if (voice.check_status === 'failed') {
    return <span className="stamp red" title={voice.check_reason || undefined}>未通过校验</span>
  }
  return <span className="stamp grey">未校验</span>
}

/**
 * 「角色设定与生成参数」弹窗内的声音栏（方案 5.4 节，紧挨「语风」挂载）：编辑
 * 音色描述/试听文本、生成或重新生成（先过 DecisionDialog 确认，不用
 * window.confirm——本仓 vitest 跑在 node 环境无 window，且需要与"重新生成"时
 * 「新声音进候选」的额外说明共用同一套受控 UI）、候选列表试听与采用、当前声音
 * 标「当前」。请求中整体禁用，错误就地用中文显示，不弹 toast 之外的第二套通知。
 */
export default function CharacterVoicePanel({ projectId, characterName }: {
  projectId: string
  characterName: string
}) {
  const { data, refresh } = useProjectVoices(projectId)
  const item = findVoiceItem(data, characterName)
  const current = item?.current ?? null
  const candidates = [...(item?.candidates ?? [])].sort((a, b) => b.created_at - a.created_at)
  const generating = item?.generating ?? false
  // 数据还没到时按"已配置"处理，不因为一次轮询还没落地就误报横幅（见下方判据）。
  const voiceModelConfigured = data?.voice_model_configured ?? true

  const [draftPrompt, setDraftPrompt] = useState(() => current?.voice_prompt ?? '')
  const [draftPreview, setDraftPreview] = useState(() => current?.preview_text ?? '')
  const [suggesting, setSuggesting] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [adoptingId, setAdoptingId] = useState<string | null>(null)
  const [confirmGenerate, setConfirmGenerate] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const busy = suggesting || submitting || !!adoptingId || generating

  const suggest = async () => {
    setSuggesting(true)
    setError(null)
    try {
      const result = await suggestVoiceDescription(projectId, characterName)
      setDraftPrompt(result.voice_prompt)
      setDraftPreview(result.preview_text)
    } catch (err) {
      setError(describeError(err))
    } finally {
      setSuggesting(false)
    }
  }

  const doGenerate = async () => {
    setConfirmGenerate(false)
    setSubmitting(true)
    setError(null)
    try {
      await generateCharacterVoice(projectId, characterName, {
        voice_prompt: draftPrompt.trim(),
        preview_text: draftPreview.trim(),
        idempotency_key: newIdemKey(`voice-gen:${projectId}:${characterName}`),
      })
      refresh()
    } catch (err) {
      setError(describeError(err))
    } finally {
      setSubmitting(false)
    }
  }

  const adopt = async (voiceId: string) => {
    setAdoptingId(voiceId)
    setError(null)
    try {
      await adoptCharacterVoice(projectId, characterName, voiceId)
      refresh()
    } catch (err) {
      setError(describeError(err))
    } finally {
      setAdoptingId(null)
    }
  }

  return (
    <div className="voice-panel">
      <div><b>声音</b></div>
      {error && <p className="voice-error">{error}</p>}
      {!voiceModelConfigured && (
        <p className="hint">还没有配置声音生成模型，<a href="/system/models">去模型中心配置</a>后才能生成。</p>
      )}
      <label className="f">音色描述</label>
      <textarea
        className="voice-panel-textarea" rows={3} disabled={busy} value={draftPrompt}
        placeholder="留空则生成时由模型按角色设定自动写一份"
        onChange={event => setDraftPrompt(event.target.value)}
      />
      <label className="f">试听文本</label>
      <textarea
        className="voice-panel-textarea" rows={2} disabled={busy} value={draftPreview}
        placeholder="留空则生成时由模型按角色设定自动写一份"
        onChange={event => setDraftPreview(event.target.value)}
      />
      <div className="voice-panel-actions">
        <button type="button" className="btn small" disabled={busy} onClick={() => void suggest()}>
          {suggesting ? '生成描述中…' : '让模型写描述'}
        </button>
        <button
          type="button" className="btn small primary" disabled={busy || !voiceModelConfigured}
          onClick={() => setConfirmGenerate(true)}
        >
          {submitting ? '生成中…' : current ? '重新生成' : '生成声音'}
        </button>
        {generating && <span className="stamp gold">生成中</span>}
      </div>

      {current && (
        <div className="voice-candidate-item">
          <VoicePlayButton voice={current} label={`${characterName}当前声音`} />
          <CandidateBadge voice={current} />
          <span className="stamp green">当前</span>
        </div>
      )}
      {!!candidates.length && (
        <div className="voice-candidate-list">
          <b>候选</b>
          {candidates.map(candidate => (
            <div className="voice-candidate-item" key={candidate.id}>
              <VoicePlayButton voice={candidate} label={`${characterName}候选声音`} />
              <CandidateBadge voice={candidate} />
              {candidate.status === 'candidate' && (
                <button
                  type="button" className="btn small"
                  disabled={busy || adoptingId === candidate.id}
                  onClick={() => void adopt(candidate.id)}
                >
                  {adoptingId === candidate.id ? '采用中…' : '采用'}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {!current && !candidates.length && !generating && <p className="hint">这个角色还没有生成过声音。</p>}

      {confirmGenerate && (
        <DecisionDialog
          title={current ? '重新生成声音' : '生成声音'}
          summary="将调用一次付费的声音生成接口"
          message={current
            ? '当前声音在你采用新候选之前不会被替换；新声音会进入候选，需要你试听后点“采用”。'
            : '生成后如通过校验会自动成为这个角色的当前声音。'}
          confirmLabel="确认生成"
          cancelLabel="取消"
          onConfirm={() => void doGenerate()}
          onClose={() => setConfirmGenerate(false)}
        />
      )}
    </div>
  )
}
