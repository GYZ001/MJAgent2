// api/voices.ts 五个接口函数：只验证方法、路径拼装（含 encodeURIComponent）与
// body 形状，不重新测试 request()/mutate() 本身（那是 client.ts 的职责）。

const { mockGet, mockMutate } = vi.hoisted(() => ({ mockGet: vi.fn(), mockMutate: vi.fn() }))
vi.mock('./client', () => ({ get: mockGet, mutate: mockMutate }))

import { afterEach, describe, expect, it, vi } from 'vitest'
// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import {
  adoptCharacterVoice, generateCharacterVoice, generateMissingVoices,
  getProjectVoices, suggestVoiceDescription,
} from './voices'

afterEach(() => { mockGet.mockReset(); mockMutate.mockReset() })

const name = encodeURIComponent('张三')

describe('api/voices', () => {
  it('getProjectVoices 走 GET /projects/{id}/voices', () => {
    getProjectVoices('p1')
    expect(mockGet).toHaveBeenCalledWith('/projects/p1/voices')
    expect(mockMutate).not.toHaveBeenCalled()
  })

  it('suggestVoiceDescription 走 POST .../voice-description，不带 body（免费建议）', () => {
    suggestVoiceDescription('p1', '张三')
    expect(mockMutate).toHaveBeenCalledWith('POST', `/projects/p1/characters/${name}/voice-description`)
  })

  it('generateCharacterVoice 走 POST .../voices，逐字带上 voice_prompt/preview_text/idempotency_key', () => {
    const body = { voice_prompt: '低沉', preview_text: '天要下雨了。', idempotency_key: 'k-1' }
    generateCharacterVoice('p1', '张三', body)
    expect(mockMutate).toHaveBeenCalledWith('POST', `/projects/p1/characters/${name}/voices`, body)
  })

  it('adoptCharacterVoice 走 POST .../voices/{voice_id}/adopt', () => {
    adoptCharacterVoice('p1', '张三', 'v-9')
    expect(mockMutate).toHaveBeenCalledWith('POST', `/projects/p1/characters/${name}/voices/v-9/adopt`)
  })

  it('generateMissingVoices 走 POST /projects/{id}/voices/generate-missing，不带 body', () => {
    generateMissingVoices('p1')
    expect(mockMutate).toHaveBeenCalledWith('POST', '/projects/p1/voices/generate-missing')
  })
})
