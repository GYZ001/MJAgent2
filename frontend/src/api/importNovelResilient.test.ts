// importNovelResilient 只做一件事：用同一份请求体重放 importProject，直到
// 拿到带 project_id 的终态结果。这里不重新测试 importProject 本身的路径拼装
// （那是 projects.test.ts 的职责——若存在的话），只 mock 它来控制「网络失败 /
// 仍在执行中 / 业务错误 / 成功」四种返回形态。用假定时器避免真实等待退避间隔。

const { mockImportProject } = vi.hoisted(() => ({ mockImportProject: vi.fn() }))
vi.mock('./projects', () => ({ importProject: mockImportProject }))

import { afterEach, describe, expect, it, vi } from 'vitest'
// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import { ApiError } from './client'
// eslint-disable-next-line import/first
import { importNovelResilient } from './importNovelResilient'

afterEach(() => {
  mockImportProject.mockReset()
  vi.useRealTimers()
})

const body = { attachment_token: 'tok-1', name: '示例小说' }

function networkError(): ApiError {
  return new ApiError(0, '网络连接中断，请检查网络后重试', 'BACKEND_UNAVAILABLE', '网络错误')
}

function inProgressReplay() {
  // 命令总线对「相同幂等键仍在执行中」的重放响应：202 + 没有 project_id。
  return { ok: true, status: 'accepted', idempotency_in_progress: true } as any
}

function succeeded() {
  return { project_id: 'proj-1', ingestion: { chapter_count: 3, total_chars: 100 } }
}

describe('importNovelResilient', () => {
  it('首次网络失败、第二次成功：返回结果，且两次请求体（含 attachment_token）完全相同', async () => {
    vi.useFakeTimers()
    mockImportProject
      .mockRejectedValueOnce(networkError())
      .mockResolvedValueOnce(succeeded())

    const promise = importNovelResilient(body)
    await vi.advanceTimersByTimeAsync(2000)
    const result = await promise

    expect(result.project_id).toBe('proj-1')
    expect(mockImportProject).toHaveBeenCalledTimes(2)
    expect(mockImportProject.mock.calls[0][0]).toBe(body)
    expect(mockImportProject.mock.calls[1][0]).toBe(body)
  })

  it('重放命中「仍在执行中」的形态：继续等待重放，直到拿到带 project_id 的结果', async () => {
    vi.useFakeTimers()
    mockImportProject
      .mockResolvedValueOnce(inProgressReplay())
      .mockResolvedValueOnce(inProgressReplay())
      .mockResolvedValueOnce(succeeded())

    const promise = importNovelResilient(body)
    await vi.advanceTimersByTimeAsync(2000 + 4000)
    const result = await promise

    expect(result.project_id).toBe('proj-1')
    expect(mockImportProject).toHaveBeenCalledTimes(3)
  })

  it('业务错误（例如附件凭证失效）原样抛出，不重试', async () => {
    const businessError = new ApiError(422, '附件凭证不存在，请重新选择文件', 'attachment_invalid')
    mockImportProject.mockRejectedValueOnce(businessError)

    await expect(importNovelResilient(body)).rejects.toBe(businessError)
    expect(mockImportProject).toHaveBeenCalledTimes(1)
  })

  it('重试全部用尽仍拿不到结果：抛出中文提示，说明项目可能已经建好、该去刷新项目列表', async () => {
    vi.useFakeTimers()
    mockImportProject.mockRejectedValue(networkError())

    const expectation = expect(importNovelResilient(body)).rejects.toThrow(
      /可能已经建好[\s\S]*刷新项目列表/,
    )
    await vi.advanceTimersByTimeAsync(2000 + 4000 + 8000)
    await expectation

    // 2s/4s/8s 三次退避之间各重试一次，加上首次尝试共 4 次。
    expect(mockImportProject).toHaveBeenCalledTimes(4)
  })
})
