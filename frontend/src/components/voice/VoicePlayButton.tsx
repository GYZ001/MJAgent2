import { useEffect, useState } from 'react'
import type { VoiceVersion } from '../../api/voices'
import { activeVoiceId, subscribeVoicePlayer, toggleVoice } from './voicePlayerStore'

/** 只用得到的字段——候选/当前声音都是同一个 VoiceVersion 形状，调用方不需要
 *  传整个对象。 */
export type PlayableVoice = Pick<VoiceVersion, 'id' | 'audio_url' | 'clip_url' | 'clip_duration_s'>

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return ''
  return `${Math.round(seconds * 10) / 10} 秒`
}

/**
 * 全站共用播放器（voicePlayerStore.ts）的按钮外观：点击播放/暂停切换，同一时刻
 * 只有一个 VoicePlayButton 处于"播放中"视觉态——订阅 store，别人开始播放时本
 * 按钮会自动切回"未播放"，不需要调用方手动协调。`preload="none"`：本按钮不主动
 * 加载音频，只在真的点击播放时才发请求。没有可播放地址（生成中/失败）时禁用，
 * 不隐藏——用户能看出"这里应该有声音"而不是布局跳动。
 */
export default function VoicePlayButton({ voice, label, size = 'inline' }: {
  voice: PlayableVoice | null | undefined
  label: string
  size?: 'inline' | 'thumb'
}) {
  const [, forceRender] = useState(0)
  useEffect(() => subscribeVoicePlayer(() => forceRender(tick => tick + 1)), [])

  const id = voice?.id || ''
  // 优先播参考片段（≤5 秒，就是会传给视频模型的那段），显示的时长才与听到的一致；
  // 片段缺失时退回完整试听，此时不标时长（clip_duration_s 不是它的时长）。
  const clipUrl = voice?.clip_url || ''
  const url = clipUrl || voice?.audio_url || ''
  const disabled = !id || !url
  const playing = !disabled && activeVoiceId() === id
  const durationText = clipUrl ? formatDuration(voice?.clip_duration_s) : ''
  const actionLabel = disabled
    ? `${label}暂无可播放的声音`
    : playing ? `暂停播放${label}` : `播放${label}`

  return (
    <button
      type="button"
      className={`voice-play-btn voice-play-btn-${size}${playing ? ' playing' : ''}`}
      disabled={disabled}
      aria-label={actionLabel}
      title={actionLabel}
      onClick={() => toggleVoice(id, url)}
    >
      <span className="voice-play-icon" aria-hidden="true">{playing ? '❚❚' : '▶'}</span>
      {durationText && <span className="voice-play-duration">{durationText}</span>}
    </button>
  )
}
