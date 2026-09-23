import { describe, expect, it } from 'vitest'
import { videoModelSwitchToast } from './videoModelSwitchCopy'

const label = (value: string) => (value === 'minimax_h3' ? 'MiniMax H3' : 'Seedance 2.0')

describe('videoModelSwitchToast', () => {
  it('没有清空视频、方言也不陈旧时只报告切换结果', () => {
    expect(videoModelSwitchToast({
      changed: true, cleared_videos: 0, target_video_model: 'minimax_h3',
      storyboard_dialect_stale: false, stale_segment_count: 0,
    }, label)).toBe('已切换为 MiniMax H3')
  })

  it('清空了旧方言视频时带上数量', () => {
    expect(videoModelSwitchToast({
      changed: true, cleared_videos: 3, target_video_model: 'minimax_h3',
      storyboard_dialect_stale: false, stale_segment_count: 0,
    }, label)).toBe('已切换为 MiniMax H3，清空了 3 个旧方言视频')
  })

  it('分镜提示词仍是旧方言时必须把后端 message 带出来，不能让这条信息消失', () => {
    const toast = videoModelSwitchToast({
      changed: true, cleared_videos: 0, target_video_model: 'minimax_h3',
      storyboard_dialect_stale: true, stale_segment_count: 5,
      message: '分镜提示词仍是 hiagent 写法，需在分镜台重新生成本集分镜后才能生成视频',
    }, label)
    expect(toast).toContain('已切换为 MiniMax H3')
    expect(toast).toContain('分镜提示词仍是 hiagent 写法，需在分镜台重新生成本集分镜后才能生成视频')
  })

  it('storyboard_dialect_stale 为 true 但没有 message 时不拼出空话', () => {
    expect(videoModelSwitchToast({
      changed: true, cleared_videos: 0, target_video_model: 'minimax_h3',
      storyboard_dialect_stale: true, stale_segment_count: 5,
    }, label)).toBe('已切换为 MiniMax H3')
  })
})
