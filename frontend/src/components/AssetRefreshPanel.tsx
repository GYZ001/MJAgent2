import { useEffect, useState } from 'react'
import { api } from '../api'
import type { AssetRefreshGroup, AssetRefreshReport } from '../api/assetRefresh'

/** 「参考资产已更新」面板（2026-10-03）：人物定妆照/场景图/道具卡在本集已
 *  采用视频之后才补齐或更新时，按实体成组展示影响范围、成组重生成、整组
 *  原子采纳。生成台与成片台共用同一个组件（两边只各加一行引用，逻辑不重复
 *  一遍，见 CLAUDE.md「前端文件 ≤300 行，必要时拆组件」）。
 *
 *  界面承诺与实际行为一致（CLAUDE.md「User-Facing Behavior」）：
 *  - 「重生成本组」只对本组"需要重生成"的段发起付费重生成，不动其它段；
 *  - 「整组采用」会一次性替换这些段的采用视频，旧版本保留可回退，任何一段
 *    没选定候选版本就不允许点击——不做静默的"自动选最新"兜底；
 *  - 额度不够时不拦截（CLAUDE.md「不得提供强制忽略式绕过」的反面：这里连
 *    拦截本身都不做，只如实提示，决定权留给用户）。
 */

function newIdemKey(prefix: string): string {
  const rand = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : Math.random().toString(16).slice(2)
  return `${prefix}:${rand}`
}

type SelectionMap = Record<string, Record<string, string>>

export default function AssetRefreshPanel({
  episodeId,
  onToast,
  onRefresh,
}: {
  episodeId: string
  onToast: (message: string, isErr?: boolean) => void
  onRefresh: () => unknown
}) {
  const [report, setReport] = useState<AssetRefreshReport | null>(null)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [selected, setSelected] = useState<SelectionMap>({})

  const load = async () => {
    try {
      setReport(await api.getAssetRefreshReport(episodeId))
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), true)
    }
  }

  useEffect(() => { void load() }, [episodeId])

  useEffect(() => {
    if (!report) return
    setSelected(prev => {
      const next: SelectionMap = {}
      for (const group of report.groups) {
        const groupSelection = { ...(prev[group.entity_key] || {}) }
        for (const member of group.members) {
          if (member.status === 'has_candidate' && !groupSelection[member.shot_id] && member.candidates[0]) {
            groupSelection[member.shot_id] = member.candidates[0].version_id
          }
        }
        next[group.entity_key] = groupSelection
      }
      return next
    })
  }, [report])

  if (!report || report.groups.length === 0) return null

  const regenerateGroup = async (group: AssetRefreshGroup) => {
    setBusyKey(group.entity_key)
    try {
      const result = await api.regenerateAssetRefresh(episodeId, [group.entity_key], newIdemKey(`asset-refresh:${episodeId}`))
      onToast(result.errors.length ? `${result.message}：${result.errors.map(e => `段 ${e.shot_id} 被拦住`).join('；')}` : result.message, result.errors.length > 0)
      await load()
      await onRefresh()
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), true)
    } finally {
      setBusyKey(null)
    }
  }

  const adoptGroup = async (group: AssetRefreshGroup) => {
    const versions = selected[group.entity_key] || {}
    const needed = group.members.filter(m => m.status === 'has_candidate')
    if (group.needs_regen_shot_ids.length > 0) {
      onToast(`还有 ${group.needs_regen_shot_ids.length} 段尚未重生成，请先完成重生成再整组采用`, true)
      return
    }
    if (!needed.length || needed.some(m => !versions[m.shot_id])) {
      onToast('请先为每个待切换段落选定要采用的版本', true)
      return
    }
    setBusyKey(group.entity_key)
    try {
      const result = await api.adoptAssetRefreshGroup(
        episodeId, group.entity_key, versions, `按最新参考统一采用「${group.entity_name}」`,
        newIdemKey(`asset-refresh-adopt:${episodeId}`),
      )
      onToast(`已一次性替换 ${result.adopted.length} 段的采用视频；旧版本保留可回退`)
      await load()
      await onRefresh()
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), true)
    } finally {
      setBusyKey(null)
    }
  }

  const quota = report.quota
  return (
    <section className="card asset-refresh-panel" aria-label="参考资产已更新">
      <div><b>参考资产已更新</b><span>{report.groups.length} 项待处理</span></div>
      <p className="hint">
        人物定妆照/场景图/道具卡在本集已采用视频之后又更新了；本面板只影响下面列出的实体与段落，
        不动其它内容。重生成需要消耗视频额度（约 {quota.needs_regen_seconds} 秒）
        {quota.account && !quota.account.unlimited && (
          quota.enough
            ? `，剩余额度 ${quota.account.total_remaining} 秒，够用`
            : `，剩余额度仅 ${quota.account.total_remaining} 秒，可能不够，仍可继续，不会被强制拦住`
        )}。
      </p>
      {report.groups.map(group => (
        <AssetRefreshGroupCard
          key={group.entity_key}
          group={group}
          busy={busyKey === group.entity_key}
          selected={selected[group.entity_key] || {}}
          onSelect={(shotId, versionId) => setSelected(prev => ({
            ...prev, [group.entity_key]: { ...(prev[group.entity_key] || {}), [shotId]: versionId },
          }))}
          onRegenerate={() => { void regenerateGroup(group) }}
          onAdopt={() => { void adoptGroup(group) }}
        />
      ))}
    </section>
  )
}

function AssetRefreshGroupCard({
  group, busy, selected, onSelect, onRegenerate, onAdopt,
}: {
  group: AssetRefreshGroup
  busy: boolean
  selected: Record<string, string>
  onSelect: (shotId: string, versionId: string) => void
  onRegenerate: () => void
  onAdopt: () => void
}) {
  const needsRegen = group.needs_regen_shot_ids.length
  const hasCandidateMembers = group.members.filter(m => m.status === 'has_candidate')
  // 本组只要还有段落需要重生成，就不允许整组采用——否则会一次性替换一部分
  // 段落而把另一部分段落的旧资产留在成片里，制造新的跨段不一致（CLAUDE.md
  // 「不做部分采用」，与用户拒绝「只换第 10 段」同一类问题）。
  const adoptReady = needsRegen === 0 && hasCandidateMembers.length > 0 && hasCandidateMembers.every(m => selected[m.shot_id])
  const adoptBlockedHint = needsRegen > 0 ? `还有 ${needsRegen} 段尚未重生成，请先完成重生成` : '请先为每个待切换段落选定版本'
  return (
    <div className="asset-refresh-group">
      <div>
        <b>{group.entity_name}</b><span className="hint">（{group.category_label}）</span>
      </div>
      <ul>
        {group.members.map(member => (
          <li key={member.shot_id}>
            段 {member.shot_no}：{member.status_label}
            {member.status === 'has_candidate' && (
              <select
                aria-label={`段 ${member.shot_no} 采用候选版本`}
                value={selected[member.shot_id] || ''}
                onChange={event => onSelect(member.shot_id, event.target.value)}
              >
                {member.candidates.map(candidate => (
                  <option key={candidate.version_id} value={candidate.version_id}>{candidate.version_id}</option>
                ))}
              </select>
            )}
            {member.status !== 'latest' && member.status !== 'not_adopted' && (
              <span className="hint"> {member.reason}</span>
            )}
          </li>
        ))}
      </ul>
      <div className="asset-refresh-actions">
        <button type="button" className="btn" disabled={busy || !needsRegen} onClick={onRegenerate}>
          {busy ? '处理中…' : `重生成本组（${needsRegen} 段）`}
        </button>
        <button
          type="button" className="btn primary" disabled={busy || !adoptReady} onClick={onAdopt}
          title={adoptReady ? undefined : adoptBlockedHint}
        >
          整组采用
        </button>
      </div>
    </div>
  )
}
