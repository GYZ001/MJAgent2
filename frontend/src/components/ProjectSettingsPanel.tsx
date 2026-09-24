import { useId, useState } from 'react'
import { api, type AspectRatioImpact, type Project, type ProjectSettingsUpdate } from '../api'
import DecisionDialog from './DecisionDialog'
import { buildAspectRatioImpactDialog } from '../lib/aspectRatioImpact'

/**
 * 项目设置面板（2026-09-23 用户拍板）：改编强度 / 画幅 / AI 生成标识。挂在分集
 * 规划页（EpisodesPage.tsx），三项都走 PUT /projects/{id}/settings（命令总线，
 * 非法值 409 中文 detail，直接展示 ApiError.message 即可，同 StageTextModelPicker
 * 的简单 try/catch 模式——这个端点不涉及审批/确认形态的响应）。
 *
 * 画幅是唯一需要二次确认的字段：切换前调用影响预估接口，如实告知有多少已采用
 * 视频/场景图仍是旧画幅，用户确认后才真正提交（其余两项改动不影响已有产物，
 * 不需要二次确认）。
 */
export default function ProjectSettingsPanel({
  project, toast, onSaved,
}: {
  project: Project
  toast: (message: string, isErr?: boolean) => void
  onSaved: () => void
}) {
  const adaptationMode = project.adaptation_mode ?? 'faithful'
  const aspectRatio = project.aspect_ratio ?? '9:16'
  const aiLabelEnabled = !!project.ai_label_enabled

  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [pendingAspectRatio, setPendingAspectRatio] = useState<string | null>(null)
  const [impact, setImpact] = useState<AspectRatioImpact | null>(null)
  const [impactError, setImpactError] = useState<string | null>(null)
  const [impactLoading, setImpactLoading] = useState(false)
  const adaptationModeId = useId()
  const aspectRatioId = useId()
  const aiLabelId = useId()

  const save = async (patch: ProjectSettingsUpdate) => {
    setBusy(true)
    setFormError(null)
    try {
      await api.updateProjectSettings(project.id, patch)
      toast('项目设置已保存')
      onSaved()
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setFormError(message)
      toast(message, true)
    } finally {
      setBusy(false)
    }
  }

  const closeAspectRatioDialog = () => {
    setPendingAspectRatio(null)
    setImpact(null)
    setImpactError(null)
  }

  const requestAspectRatioChange = async (next: string) => {
    if (next === aspectRatio || busy) return
    setPendingAspectRatio(next)
    setImpact(null)
    setImpactError(null)
    setImpactLoading(true)
    try {
      setImpact(await api.getAspectRatioImpact(project.id, next))
    } catch (err) {
      setImpactError(err instanceof Error ? err.message : String(err))
    } finally {
      setImpactLoading(false)
    }
  }

  const aspectRatioDialog = pendingAspectRatio && (impact || impactError)
    ? buildAspectRatioImpactDialog(pendingAspectRatio, impact, impactError)
    : null

  return (
    <section className="card project-settings-panel">
      <h2>项目设置</h2>
      {formError && <p className="query-error" role="alert">{formError}</p>}
      <div className="project-settings-field">
        <label htmlFor={adaptationModeId}>改编强度</label>
        <select id={adaptationModeId} disabled={busy} value={adaptationMode}
          onChange={event => void save({ adaptation_mode: event.target.value })}>
          <option value="short_drama">短剧节奏（单集约 90 秒，允许精简非关键原文）</option>
          <option value="faithful">忠实原著（原文每一句剧情都要拍到）</option>
        </select>
        <p className="hint">只影响之后新生成的分镜；已生成的分镜按生成时的档位判定。</p>
      </div>
      <div className="project-settings-field">
        <label htmlFor={aspectRatioId}>画幅</label>
        <select id={aspectRatioId} disabled={busy || impactLoading} value={aspectRatio}
          onChange={event => void requestAspectRatioChange(event.target.value)}>
          <option value="9:16">9:16（竖屏）</option>
          <option value="16:9">16:9（横屏）</option>
        </select>
        <p className="hint">
          只影响之后生成的视频与场景图；已生成的视频在成片合成时会等比放大裁切进新画布，构图可能被裁掉。
          人物定妆照、道具图不随画幅变。
        </p>
      </div>
      <div className="project-settings-field">
        <label htmlFor={aiLabelId}>
          <input id={aiLabelId} type="checkbox" checked={aiLabelEnabled} disabled={busy}
            onChange={event => void save({ ai_label_enabled: event.target.checked })} />
          {' '}AI 生成标识
        </label>
        <p className="hint">
          开启后，成片片头 3 秒左上角会显示「本视频由人工智能生成」；只影响之后合成的成片，
          已合成的成片需要重新合成才生效。
        </p>
      </div>
      {aspectRatioDialog && (
        <DecisionDialog
          title={aspectRatioDialog.title}
          summary={aspectRatioDialog.summary}
          message={aspectRatioDialog.message}
          details={aspectRatioDialog.details}
          danger={aspectRatioDialog.danger}
          confirmLabel="确认切换画幅"
          cancelLabel="取消（保持当前画幅）"
          onClose={closeAspectRatioDialog}
          onConfirm={() => {
            const next = pendingAspectRatio
            closeAspectRatioDialog()
            void save({ aspect_ratio: next as string })
          }}
        />
      )}
    </section>
  )
}
