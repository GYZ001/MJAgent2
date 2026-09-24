import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  __resetVoicePlayerForTest, __setVoiceAudioFactoryForTest, activeVoiceId,
  pauseVoice, subscribeVoicePlayer, toggleVoice, type VoicePlayerHandle,
} from './voicePlayerStore'

/** 假播放器：不依赖任何 DOM（vitest 全局跑在 node 环境，没有 jsdom；即便有，
 *  jsdom 的 HTMLMediaElement.play/pause 也没有真正实现）。 */
function makeFakeHandle(): VoicePlayerHandle {
  return {
    play: vi.fn(),
    pause: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    src: '',
    currentTime: 0,
    preload: '',
  }
}

describe('voicePlayerStore', () => {
  beforeEach(() => {
    __resetVoicePlayerForTest()
    __setVoiceAudioFactoryForTest(null)
  })

  it('全站只创建一个播放器实例，反复换 src 复用；播 A 再播 B 时 A 被暂停', () => {
    const handle = makeFakeHandle()
    const factory = vi.fn(() => handle)
    __setVoiceAudioFactoryForTest(factory)

    toggleVoice('va', 'a.mp3')
    expect(factory).toHaveBeenCalledTimes(1)
    expect(handle.src).toBe('a.mp3')
    expect(handle.play).toHaveBeenCalledTimes(1)
    expect(activeVoiceId()).toBe('va')

    toggleVoice('vb', 'b.mp3')
    expect(factory).toHaveBeenCalledTimes(1) // 没有再创建第二个 <audio>
    expect(handle.pause).toHaveBeenCalledTimes(1) // 切换前先暂停了 A
    expect(handle.src).toBe('b.mp3')
    expect(handle.play).toHaveBeenCalledTimes(2)
    expect(activeVoiceId()).toBe('vb') // 同一时刻只有 vb 在播放
  })

  it('再次点击正在播放的同一个 id 会暂停（播放/暂停切换）', () => {
    __setVoiceAudioFactoryForTest(makeFakeHandle)
    toggleVoice('va', 'a.mp3')
    expect(activeVoiceId()).toBe('va')
    toggleVoice('va', 'a.mp3')
    expect(activeVoiceId()).toBeNull()
  })

  it('订阅者在播放目标变化时收到通知，取消订阅后不再收到', () => {
    __setVoiceAudioFactoryForTest(makeFakeHandle)
    const listener = vi.fn()
    const unsubscribe = subscribeVoicePlayer(listener)
    toggleVoice('va', 'a.mp3')
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
    toggleVoice('vb', 'b.mp3')
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('没有 id 或没有地址时不播放（VoicePlayButton 用这个语义禁用按钮）', () => {
    __setVoiceAudioFactoryForTest(makeFakeHandle)
    toggleVoice('', 'a.mp3')
    expect(activeVoiceId()).toBeNull()
    toggleVoice('va', '')
    expect(activeVoiceId()).toBeNull()
  })

  it('pauseVoice 在没有任何播放时是安全的空操作', () => {
    __setVoiceAudioFactoryForTest(makeFakeHandle)
    expect(() => pauseVoice()).not.toThrow()
    expect(activeVoiceId()).toBeNull()
  })
})
