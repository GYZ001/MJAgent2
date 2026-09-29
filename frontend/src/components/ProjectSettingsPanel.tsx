import { useEffect, useId, useState } from 'react'
import { api, type AspectRatioImpact, type Project, type ProjectSettingsUpdate } from '../api'
import DecisionDialog from './DecisionDialog'
import { buildAspectRatioImpactDialog } from '../lib/aspectRatioImpact'

/**
 * 项目设置面板（2026-09-23 用户拍板；2026-09-28 加统一配乐/片头预告/主角内心
 * 独白三个开关，同日接上成片合成消费方——见 app.final_edit_enhance；同日再加
 * 旁白音色角色，见 app.voice.segment_refs 的旁白参考音频接线）：
 * 改编强度 / 画幅 / AI 生成标识 / 统一配乐 / 片头预告 / 主角内心独白 / 旁白音色
 * 角色。挂在分集规划页（EpisodesPage.tsx），全部字段都走 PUT /projects/{id}/settings
 * （命令总线，非法值 409 中文 detail，直接展示 ApiError.message 即可，同
 * StageTextModelPicker 的简单 try/catch 模式——这个端点不涉及审批/确认形态的响应）。
 * 三个开关现在都有真实消费方：开启后下一次成片合成会生效（已合成的成片需要
 * 重新合成才生效，同 AI 标识）；界面文案不再写「尚未上线」/「功能开发中」。
 *
 * 画幅是唯一需要二次确认的字段：切换前调用影响预估接口，如实告知有多少已采用
 * 视频/场景图仍是旧画幅，用户确认后才真正提交（其余字段改动不影响已有产物，
 * 不需要二次确认）。旁白音色角色是唯一的文本输入字段：本地维护草稿状态，失焦
 * 且与已保存值不同才提交，避免逐字触发请求；project 的最新值变化时（例如保存
 * 成功后 onSaved 触发的刷新）用 useEffect 把草稿同步回来。
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
  const enhanceMusicBed = !!project.enhance_music_bed
  const enhanceTeaser = !!project.enhance_teaser
  const enhanceMonologue = !!project.enhance_monologue
  const narratorVoiceCharacter = project.narrator_voice_character ?? ''

  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [pendingAspectRatio, setPendingAspectRatio] = useState<string | null>(null)
  const [impact, setImpact] = useState<AspectRatioImpact | null>(null)
  const [impactError, setImpactError] = useState<string | null>(null)
  const [impactLoading, setImpactLoading] = useState(false)
  const [narratorDraft, setNarratorDraft] = useState(narratorVoiceCharacter)
  useEffect(() => setNarratorDraft(narratorVoiceCharacter), [narratorVoiceCharacter])
  const adaptationModeId = useId()
  const aspectRatioId = useId()
  const aiLabelId = useId()
  const enhanceMusicBedId = useId()
  const enhanceTeaserId = useId()
  const enhanceMonologueId = useId()
  const narratorVoiceCharacterId = useId()

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
      <div className="project-settings-field">
        <label htmlFor={enhanceMusicBedId}>
          <input id={enhanceMusicBedId} type="checkbox" checked={enhanceMusicBed} disabled={busy}
            onChange={event => void save({ enhance_music_bed: event.target.checked })} />
          {' '}统一配乐
        </label>
        <p className="hint">
          开启后，分镜台生成的每段视频不再各自带配乐，只保留人物对白与环境音；成片合成时
          会按剧情自动配一条贯穿全集的背景音乐（曲库为公有领域/CC0 钢琴曲，相邻情绪相近的
          段落会复用同一首减少切歌感），并按台词自动闪避音量。只消耗少量免费文本调用与本地
          算力，不花视频额度。
        </p>
      </div>
      <div className="project-settings-field">
        <label htmlFor={enhanceTeaserId}>
          <input id={enhanceTeaserId} type="checkbox" checked={enhanceTeaser} disabled={busy}
            onChange={event => void save({ enhance_teaser: event.target.checked })} />
          {' '}片头预告
        </label>
        <p className="hint">
          开启后，成片合成时会从本集画面挑 3-5 个高能瞬间剪成约 8-12 秒的预告片，硬切拼接并
          接在正片最前面，正片字幕与连播章节会自动按预告时长整体后移。只消耗少量免费文本
          调用与本地算力，不花视频额度。
        </p>
      </div>
      <div className="project-settings-field">
        <label htmlFor={enhanceMonologueId}>
          <input id={enhanceMonologueId} type="checkbox" checked={enhanceMonologue} disabled={busy}
            onChange={event => void save({ enhance_monologue: event.target.checked })} />
          {' '}主角内心独白
        </label>
        <p className="hint">
          开启后，成片合成时会在剧情静默处插入 1-8 句取自原文的角色心理描写旁白（逐字引用，
          不改写、不编造），用该角色已设置的固定音色朗读并出字幕；未设置固定音色的角色会
          跳过。会产生少量语音合成费用（非视频额度），文本编排本身不计费。
        </p>
      </div>
      <div className="project-settings-field">
        <label htmlFor={narratorVoiceCharacterId}>旁白音色角色</label>
        <input id={narratorVoiceCharacterId} type="text" disabled={busy} value={narratorDraft}
          placeholder="留空＝不设置，旁白每段音色不固定"
          onChange={event => setNarratorDraft(event.target.value)}
          onBlur={() => {
            const next = narratorDraft.trim()
            if (next === narratorVoiceCharacter) return
            void save({ narrator_voice_character: next })
          }} />
        <p className="hint">
          填写人物谱中已存在的角色正名（例如「温念」），旁白会挂上该角色已采用的固定音色，
          保持前后一致；角色不存在会保存失败并提示。留空表示不设置，旁白每段音色由视频模型
          随机分配。只影响设置后新生成的分镜与视频，已生成的分集需要重新生成分镜才会生效。
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
