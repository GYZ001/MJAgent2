import type { VoiceItem, VoiceVersion } from '../../api/voices'
import VoicePlayButton from './VoicePlayButton'
import { findVoiceItem, useProjectVoices } from './useProjectVoices'

/** 跳回人物谱的链接地址：故意不用 App.tsx::locationFor——那个函数被
 *  ScriptPage.render.test.ts 等好几处 `vi.mock('../App', () => ({...}))` 成一个
 *  不含 locationFor 的窄替身（历史上这条依赖链从没真正调用过它），本组件是这条
 *  路径第一个会调用它的消费方，贸然复用会让既有测试炸在"mock 里没有这个导出"。
 *  URL 形状与 locationFor(view='bible', ...) 完全一致（人物谱没有 episode/chapter
 *  参数），用不到那个函数的其余分支，直接拼字符串更稳。 */
function bibleHref(projectId: string | null | undefined): string {
  return projectId ? `/projects/${encodeURIComponent(projectId)}/bible` : '/workspaces'
}

export type VoiceChipState =
  | { kind: 'current'; voice: VoiceVersion }
  | { kind: 'generating' }
  | { kind: 'failed'; reason: string }
  | { kind: 'unconfigured' }

/** 人物卡/映射台人物谱共用的声音状态判定：当前声音优先；没有当前声音但正在生成
 *  中显示"生成中"；都没有时看最近一条候选是不是失败态；否则就是"从没配过"。 */
export function voiceChipState(item: VoiceItem | null): VoiceChipState {
  if (item?.current) return { kind: 'current', voice: item.current }
  if (item?.generating) return { kind: 'generating' }
  const latest = [...(item?.candidates ?? [])].sort((a, b) => b.created_at - a.created_at)[0]
  if (latest?.status === 'failed') return { kind: 'failed', reason: latest.error || '生成失败，原因未知' }
  return { kind: 'unconfigured' }
}

const STATE_TEXT: Record<Exclude<VoiceChipState['kind'], 'current'>, string> = {
  generating: '声音生成中…',
  failed: '声音生成失败',
  unconfigured: '未配置声音',
}

/**
 * 声音状态标记（方案 5.4 节）：有当前声音显示可播放的按钮；否则显示文字状态。
 * BiblePage（人物卡，`readOnly=false`）用纯展示态，操作入口是旁边的「角色设定与
 * 生成参数」弹窗；ScriptPage（映射台人物谱，`readOnly=true`）只读，非"当前声音"
 * 三态点击跳回人物谱（CLAUDE.md「拦住用户时必须给出路」）——"当前声音"态本身
 * 已经是可用的播放按钮，不需要也不应该再套一层 <a>（<button> 不能嵌在 <a> 里）。
 *
 * 数据还没到（loading 且未命中 item）时不抢先下结论显示"未配置"：那会在数据到达
 * 后又跳成别的状态，闪一下不如先不显示。
 */
export default function VoiceChip({ projectId, characterName, readOnly = false }: {
  projectId: string | null | undefined
  characterName: string | null | undefined
  readOnly?: boolean
}) {
  const { data, loading } = useProjectVoices(projectId)
  if (!characterName) return null
  const item = findVoiceItem(data, characterName)
  if (!item && loading) return null

  const state = voiceChipState(item)
  if (state.kind === 'current') {
    return (
      <span className="voice-chip">
        <VoicePlayButton voice={state.voice} label={`${characterName}的声音`} size="inline" />
      </span>
    )
  }

  const text = STATE_TEXT[state.kind]
  const title = state.kind === 'failed' ? state.reason : undefined
  const className = `voice-chip voice-chip-text voice-chip-${state.kind}`
  if (readOnly) {
    // 可见文字本身就是 "未配置声音"/"声音生成中…"/"声音生成失败"，足以充当可访问
    // 名称，不额外拼回角色名——人物谱同一行前面已经显示过一次姓名，重复拼进这里
    // 会让"角色名只应出现一次"这类既有断言（ScriptPage.test.ts）连带炸掉。
    return (
      <a href={bibleHref(projectId)} className={className} title={title}>
        {text}
      </a>
    )
  }
  return <span className={className} title={title}>{text}</span>
}
