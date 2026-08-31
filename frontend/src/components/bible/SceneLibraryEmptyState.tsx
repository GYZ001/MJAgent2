/** 场景库场景列表的空态提示。从 ScenesPage.tsx 抽出：架构转向后场景默认由
 * 映射台按需发现，本页新增了「手动添加场景」入口后，这条文案必须同步更新
 * ——不能继续说「本页只负责展示与补图」，那句话在本次改动后不再是真的。 */
export default function SceneLibraryEmptyState({ onGoEpisodes }: { onGoEpisodes: () => void }) {
  return (
    <div className="library-filter-empty" role="status">
      <b>场景库暂无场景</b>
      <p>场景在映射台按需发现：进入分集页选择集数并开始映射，原文里出现的场景会自动建卡；也可以用上方「+ 手动添加场景」直接创建。</p>
      <button type="button" className="btn small" onClick={onGoEpisodes}>前往分集页 →</button>
    </div>
  )
}
