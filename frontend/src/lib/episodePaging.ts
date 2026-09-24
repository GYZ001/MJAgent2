/** 从 pages/EpisodesPage.tsx 挪来（2026-09-23，项目设置面板腾行数用）：分集分页
 *  跳转的纯判定逻辑，不碰组件状态，天然适合独立成文件——同 EpisodeBatchConfirmDialog
 *  的先例（EP-01 第二阶段）。外部导入路径同步改到这里，见 EpisodesPage.test.ts。 */
export function resolveEpisodePage(
  value: string,
  pageCount: number,
  currentPage: number,
): { page: number; message: string } {
  const raw = Number.parseInt(value.trim(), 10)
  if (!Number.isFinite(raw)) {
    return { page: currentPage, message: `请输入 1 到 ${pageCount} 之间的页码` }
  }
  const page = Math.min(pageCount, Math.max(1, raw))
  if (page === currentPage && raw === currentPage) {
    return { page, message: `当前已是第 ${currentPage} 页` }
  }
  if (raw < 1) return { page, message: '页码不能小于 1，已跳到第一页' }
  if (raw > pageCount) return { page, message: `页码不能超过 ${pageCount}，已跳到最后一页` }
  return { page, message: `已跳到第 ${page} 页` }
}
