import { useId } from 'react'

export interface NewProjectSettingsValue {
  adaptation_mode: string
  aspect_ratio: string
  ai_label_enabled: boolean
}

/** 新建项目表单默认值（2026-09-23 用户拍板）：短剧节奏 / 9:16 / AI 标识关闭。 */
export const DEFAULT_NEW_PROJECT_SETTINGS: NewProjectSettingsValue = {
  adaptation_mode: 'short_drama', aspect_ratio: '9:16', ai_label_enabled: false,
}

/**
 * 新建项目表单的三项设置（改编强度/画幅/AI 生成标识），随 POST /projects/import
 * 一起提交。受控组件：值与变更都交给父组件（Studio.tsx）管理，创建后仍可在
 * 分集规划页的项目设置面板里修改。
 */
export default function NewProjectSettingsFields({
  value, onChange, disabled,
}: {
  value: NewProjectSettingsValue
  onChange: (next: NewProjectSettingsValue) => void
  disabled?: boolean
}) {
  const adaptationModeId = useId()
  const aspectRatioId = useId()
  const aiLabelId = useId()
  return (
    <div className="new-project-settings-fields">
      <div className="project-settings-field">
        <label htmlFor={adaptationModeId}>改编强度</label>
        <select id={adaptationModeId} disabled={disabled} value={value.adaptation_mode}
          onChange={event => onChange({ ...value, adaptation_mode: event.target.value })}>
          <option value="short_drama">短剧节奏（单集约 90 秒，允许精简非关键原文，推荐）</option>
          <option value="faithful">忠实原著（原文每一句剧情都要拍到）</option>
        </select>
        <p className="hint">只影响之后新生成的分镜；创建后仍可在分集规划页的项目设置里修改。</p>
      </div>
      <div className="project-settings-field">
        <label htmlFor={aspectRatioId}>画幅</label>
        <select id={aspectRatioId} disabled={disabled} value={value.aspect_ratio}
          onChange={event => onChange({ ...value, aspect_ratio: event.target.value })}>
          <option value="9:16">9:16（竖屏）</option>
          <option value="16:9">16:9（横屏）</option>
        </select>
        <p className="hint">只影响之后生成的视频与场景图；人物定妆照、道具图不随画幅变。</p>
      </div>
      <div className="project-settings-field">
        <label htmlFor={aiLabelId}>
          <input id={aiLabelId} type="checkbox" checked={value.ai_label_enabled} disabled={disabled}
            onChange={event => onChange({ ...value, ai_label_enabled: event.target.checked })} />
          {' '}AI 生成标识
        </label>
        <p className="hint">开启后成片片头 3 秒左上角会显示「本视频由人工智能生成」。</p>
      </div>
    </div>
  )
}
