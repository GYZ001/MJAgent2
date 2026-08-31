/** 人物谱角色列表的空态提示（筛选未命中 / 人物谱确无角色两种口径）。从
 * BiblePage.tsx 抽出：架构转向后角色默认由映射台按需发现，本页新增了「手动
 * 添加角色」入口后，这条文案必须同步更新——不能继续说「本页只负责展示」，
 * 那句话在本次改动后不再是真的。 */
export default function CharacterLibraryEmptyState({
  hasCriteria, query, totalCount, onResetFilters, onGoEpisodes,
}: {
  hasCriteria: boolean
  query: string
  totalCount: number
  onResetFilters: () => void
  onGoEpisodes: () => void
}) {
  return (
    <div className="library-filter-empty" role="status">
      <b>{hasCriteria ? '没有符合当前条件的角色' : '人物谱暂无角色'}</b>
      <p>{hasCriteria
        ? `${query ? `搜索“${query}”` : '当前筛选'}未命中；清除条件后可恢复全部 ${totalCount} 个角色。`
        : '角色在映射台按需发现：进入分集页选择集数并开始映射，原文里出现的角色会自动建卡；也可以用上方「+ 手动添加角色」直接创建。'}</p>
      {hasCriteria
        ? <button type="button" className="btn small" onClick={onResetFilters}>清除搜索与筛选</button>
        : <button type="button" className="btn small" onClick={onGoEpisodes}>前往分集页 →</button>}
    </div>
  )
}
