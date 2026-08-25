import { describe, expect, it } from 'vitest'
import { isChunkLoadError } from './ErrorBoundary'

describe('分包加载失败识别', () => {
  it('认出各浏览器的动态 import 失败措辞', () => {
    const chunkErrors = [
      Object.assign(new Error('Loading chunk 42 failed.'), { name: 'ChunkLoadError' }),
      new Error('Failed to fetch dynamically imported module: /assets/WallPage-CJvYpOUT.js'),
      new Error('error loading dynamically imported module'),
      new Error('Importing a module script failed.'),
    ]
    for (const error of chunkErrors) {
      expect(isChunkLoadError(error)).toBe(true)
    }
  })

  it('不把普通渲染异常误判为分包失败', () => {
    expect(isChunkLoadError(new TypeError("Cannot read properties of undefined (reading 'shots')"))).toBe(false)
    expect(isChunkLoadError(new Error('无法连接本机后端服务，请等待服务恢复后重试'))).toBe(false)
    expect(isChunkLoadError(null)).toBe(false)
  })
})
