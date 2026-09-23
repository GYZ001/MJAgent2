import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// useEpisode 内部走 usePoll，已有 ep 后再轮询失败不清空 ep；error 此前只喂给早退
// 的 `if (error && !ep) / if (!ep)` 两条 QueryState 分支，ep 到手后就再没人看
// （2026-09-23 补丁，同 orgs/resources 各面板 2c96b89c）。WallPage.test.ts 已经卡
// 在 FILE_CONVENTIONS.toml 的行数棘轮上（291 行，精确实测值、零缓冲），装不下这
// 条新场景，另开文件；本页无组件渲染测试基建（同 BiblePage.test.ts 顶部注释），
// 用源码静态扫描守住接线不回归。
const source = readFileSync(fileURLToPath(new URL('./WallPage.tsx', import.meta.url)), 'utf-8')

describe('生成台——已有 ep 时后台轮询刷新失败不得被吞', () => {
  it('QueryState 早退分支之外单独渲染 StaleRefreshBanner，接的是同一个 error/refresh', () => {
    expect(source).toMatch(/import StaleRefreshBanner from '\.\.\/components\/StaleRefreshBanner'/)
    expect(source).toMatch(/<StaleRefreshBanner error=\{error\} onRetry=\{refresh\} objectName="生成台" \/>/)
  })

  it('两条早退 QueryState 分支保持单行 if 形态（本次为腾行数预算而合并，行为不变）', () => {
    expect(source).toMatch(/if \(error && !ep\) return <QueryState loading=\{false\} error=\{error\} status=\{status\} hasData=\{false\} objectName="生成台">\{null\}<\/QueryState>/)
    expect(source).toMatch(/if \(!ep\) return <QueryState loading=\{loading !== false\} error=\{null\} hasData=\{false\} objectName="生成台">\{null\}<\/QueryState>/)
  })
})
