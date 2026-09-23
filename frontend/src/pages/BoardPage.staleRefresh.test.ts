import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// useEpisode 内部走 usePoll，已有 ep 后再轮询失败不清空 ep；error 此前只喂给早退
// 的 `<QueryState hasData={false}>` 分支（见 542-555 行附近），ep 到手后就再没人
// 看（2026-09-23 补丁，同 orgs/resources 各面板 2c96b89c）。BoardPage.test.ts 已
// 经卡在 FILE_CONVENTIONS.toml 的行数棘轮上（363 行，精确实测值、零缓冲），装不下
// 这条新场景，另开文件；本页无组件渲染测试基建（同 BiblePage.test.ts 顶部注释），
// 用源码静态扫描守住接线不回归。
const source = readFileSync(fileURLToPath(new URL('./BoardPage.tsx', import.meta.url)), 'utf-8')

describe('分镜台——已有 ep 时后台轮询刷新失败不得被吞', () => {
  it('QueryState 早退分支之外单独渲染 StaleRefreshBanner，接的是同一个 error/refresh', () => {
    expect(source).toMatch(/import StaleRefreshBanner from '\.\.\/components\/StaleRefreshBanner'/)
    expect(source).toMatch(/<StaleRefreshBanner error=\{error\} onRetry=\{\(\) => void refresh\(\)\} objectName="分镜台" \/>/)
  })
})
