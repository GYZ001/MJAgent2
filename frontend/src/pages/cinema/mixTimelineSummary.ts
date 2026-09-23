/**
 * 成片台"本次合成时间线"摘要的纯函数层：部分跳过与系统自动采纳都要让用户
 * 刷新页面回来也能看到，不能只在一次性 toast 里一闪而过（CLAUDE.md
 * 「User-Facing Behavior」）。从 CinemaPage.tsx 搬出（该文件基线 931 行，
 * 新增自动采纳摘要需要换出等量行数）——CinemaPage.test.ts 仍从
 * './CinemaPage' 导入 finalSkipSummary，CinemaPage.tsx 保留同名再导出。
 */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function timelineOf(report: unknown): Record<string, unknown> | null {
  const timeline = isRecord(report) ? report.timeline : null
  return isRecord(timeline) ? timeline : null
}

/**
 * 部分合成是主流程：任意一镜没有可用的已采纳视频（从没生成、生成中、生成
 * 失败、或采纳指向已失效/未过技术校验的版本）都会被透明跳过，不拖垮整份
 * 成片。跳过不能只在一次性 toast 里一闪而过——用户随时刷新页面回来查看时，
 * 仍要能看到"本次成片跳过了第几镜、为什么"，不能让人误以为拿到的是完整
 * 成片。返回 null 表示没有镜头被跳过（即完整成片，无需展示）。
 */
export function finalSkipSummary(report: unknown): string | null {
  const timeline = timelineOf(report)
  if (!timeline) return null
  const skipped = timeline.skipped_shot_nos
  if (!Array.isArray(skipped) || skipped.length === 0) return null
  const reasonsRaw = timeline.skip_reasons
  const reasons = isRecord(reasonsRaw) ? reasonsRaw : {}
  const detail = skipped
    .map(no => {
      const reason = reasons[String(no)]
      return typeof reason === 'string' && reason ? `第 ${no} 镜（${reason}）` : `第 ${no} 镜`
    })
    .join('、')
  return `本次成片跳过了${skipped.length}个镜头，其余镜头正常合成：${detail}。补齐后重新合成即可自动补全。`
}

/**
 * 系统在合成前自动代采（未经人工在生成台点"采纳"）的镜头：2026-09-23 差距
 * 分析发现这类镜头此前对用户完全不可见——成片混入了没人看过的版本。后端
 * 只在 report.timeline.auto_adopted_shot_nos 里给出镜号列表，这里只负责渲
 * 染成一行中文提示；返回 null 表示本次成片没有自动采纳的镜头。
 */
export function autoAdoptedSummary(report: unknown): string | null {
  const timeline = timelineOf(report)
  if (!timeline) return null
  const raw = timeline.auto_adopted_shot_nos
  if (!Array.isArray(raw) || raw.length === 0) return null
  const shotNos = raw.filter((no): no is number => typeof no === 'number').sort((a, b) => a - b)
  if (shotNos.length === 0) return null
  return `本集第 ${shotNos.join('、')} 镜为系统自动采纳，未经人工复核；如需确认画面，请到生成台逐镜查看后手动重新采纳。`
}
