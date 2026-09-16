import { useState } from 'react'
import { api, type ShotEditImpact } from '../api'

export type SceneOption = { name: string; imageUrl?: string | null }

/**
 * 分镜台：人工更换本段绑定的场景图。
 *
 * 为什么必须有这个入口：段落的 scene_name 由分镜阶段按剧本场次标注归一得到，
 * 而一段（shots 表一行）只有一个场景字段，2.x 的一段里却有多个镜头切换——段内
 * 跨场景时这个字段必然只能取其一。2026-09-16 实测：龙猫出爪 ep1 第 17 段剧本标注
 * 是「人间·老街」，归一到「人间老街区」没错，但段内镜 1 站在宠物医院门口，于是
 * 门口那一镜拿不到本该匹配的「晚安宠物医院门口」参考图。模型和归一都没做错，是
 * 结构装不下，所以要给人一个改的地方。在此之前分镜台只能看不能改：后端的
 * shot.update 早就允许改 scene_name（edit_shot.py 的 editable_keys），但前端一个
 * 调用点都没有。
 *
 * **只换参考图，不动 prompt_text**：2.x 段落的提示词是分镜模型写的整段散文，不是
 * 模板拼的，机械替换其中的场景措辞会写坏叙事。界面必须把这点讲明白，不能让用户
 * 以为换了绑定文字也会跟着变——展示与实际行为不一致就是界面撒谎。
 *
 * 三步走（起草会话 → 影响预览 → 提交）是后端强制的，理由见
 * api/storyboard/methods.ts 里 ShotEditSession 的注释。每次改动都重新走一遍，
 * 不跨改动复用 token。
 */
export default function SegmentSceneEdit({ shotId, currentSceneName, sceneOptions, notify, onSaved }: {
  shotId: string
  currentSceneName: string
  sceneOptions: SceneOption[]
  notify: (message: string, error?: boolean) => void
  onSaved: () => void
}) {
  const [open, setOpen] = useState(false)
  const [picked, setPicked] = useState(currentSceneName)
  const [impact, setImpact] = useState<ShotEditImpact | null>(null)
  const [session, setSession] = useState<{ token: string; baselineHash: string; artifactId: string | null } | null>(null)
  const [busy, setBusy] = useState(false)

  async function perform(action: () => Promise<void>) {
    setBusy(true)
    try { await action() } catch (error) { notify(error instanceof Error ? error.message : '操作失败，请重试', true) }
    finally { setBusy(false) }
  }

  function reset() {
    setOpen(false); setImpact(null); setSession(null); setPicked(currentSceneName)
  }

  // 换了选择就作废已看过的影响：preview_token 与具体那一组改动绑定，
  // 拿旧 token 提交新改动后端会 409。
  function pick(name: string) { setPicked(name); setImpact(null); setSession(null) }

  async function checkImpact() {
    const started = await api.startShotEditSession(shotId)
    const result = await api.previewShotEditImpact(shotId, {
      edit_session_token: started.edit_session_token,
      changes: { scene_name: picked },
    })
    setSession({ token: started.edit_session_token, baselineHash: started.baseline_content_hash, artifactId: started.baseline_artifact_id })
    setImpact(result)
    if (result.unchanged) notify(result.message || '场景没有变化，不会创建新版本')
  }

  async function save() {
    if (!session || !impact?.preview_token) return
    await api.updateShot(shotId, {
      scene_name: picked,
      expected_version: session.artifactId,
      edit_session_token: session.token,
      preview_token: impact.preview_token,
      baseline_content_hash: session.baselineHash,
      revision_reason: `人工更换本段场景绑定：${currentSceneName || '未绑定'} → ${picked}`,
    })
    notify(`本段场景已改为「${picked}」；提示词正文未改动，生成台会按新场景取参考图`)
    reset(); onSaved()
  }

  const staleTotal = impact?.stale_count ?? 0
  return (
    <section className="segment-scene-edit" aria-label="更换本段场景">
      {!open
        ? <button type="button" className="text-action" onClick={() => setOpen(true)}>更换本段场景</button>
        : (
          <div className="segment-scene-edit-body">
            <p className="segment-scene-edit-hint">
              当前绑定：<b>{currentSceneName || '未绑定'}</b>。
              一段只能绑一个场景；段内跨场景时选画面主体所在的那个。
              <b>只更换参考图，提示词正文不会改动。</b>
            </p>
            <label className="segment-scene-edit-pick">
              换成
              <select value={picked} disabled={busy} onChange={event => pick(event.target.value)}>
                {!sceneOptions.some(item => item.name === currentSceneName) && currentSceneName && (
                  <option value={currentSceneName}>{currentSceneName}（当前，不在场景库中）</option>
                )}
                {sceneOptions.map(item => (
                  <option key={item.name} value={item.name}>{item.name}{item.imageUrl ? '' : '（暂无场景图）'}</option>
                ))}
              </select>
            </label>
            {impact && !impact.unchanged && (
              <div className="segment-scene-edit-impact" role="status">
                <b>保存会让 {staleTotal} 项下游产物失效</b>
                <ul>
                  {Object.entries(impact.by_artifact_type ?? {}).map(([label, count]) => (
                    <li key={label}>{label}：{count}</li>
                  ))}
                </ul>
                {impact.paid_media_invalidated && <p>本段已有生成产物，保存后需要重新生成本段视频。</p>}
              </div>
            )}
            <div className="segment-scene-edit-actions">
              {!impact
                ? <button type="button" disabled={busy || picked === currentSceneName} onClick={() => void perform(checkImpact)}>
                    {busy ? '处理中…' : '查看影响'}
                  </button>
                : <button type="button" disabled={busy || Boolean(impact.unchanged)} onClick={() => void perform(save)}>
                    {busy ? '保存中…' : '确认更换'}
                  </button>}
              <button type="button" className="text-action" disabled={busy} onClick={reset}>取消</button>
            </div>
          </div>
        )}
    </section>
  )
}
