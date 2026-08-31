import type { PrepPackCharacterAsset, PrepPackSceneAsset } from '../../api'

/**
 * 新发现 vs 索引历史资源的统计口径：provenance.method === 'discovery' 是后端
 * 对"这一集里首次发现、当场建卡 + 生成定妆照"的确定性标记（见
 * app/production/prep_pack/resolve_assets.py 的 method="discovery" 赋值，
 * 角色 463 行、场景 714 行；app/production/prep_pack/discovery.py 106 行
 * `_discover_new_characters` 的调用链最终会跑到 app.portraits.ensure_cards_for_text(
 * generate_portraits=True)，是真的建卡+出图，不是只做文本标记）。其余取值
 * （direct/alias/resolution/resolution_forward/candidate_verdict/
 * alias_inherited）都是命中人物谱/场景库里已有条目，即"索引历史资源"——映射台
 * 一次点击里这两件事自动一起做，不需要用户分两步分别触发。
 */
function isNewlyDiscovered(item: { provenance?: { method?: string } }): boolean {
  return item.provenance?.method === 'discovery'
}

/**
 * provenance 字段本身是 1.6.0+ 产物才有；更旧的产物里全体条目的 method 都是
 * undefined，此时无法区分新增与历史——渲染"索引历史 N 位"会把"没测过"冒充成
 * "测过、全部是历史资源"，是 CLAUDE.md 明确禁止的"字段缺失冒充测量结果"。
 * 调用方据此判断整块要不要渲染。
 */
export function hasDiscoveryProvenance(
  characters: PrepPackCharacterAsset[],
  scenes: PrepPackSceneAsset[],
): boolean {
  return [...characters, ...scenes].some(item => Boolean(item.provenance?.method))
}

/** 映射台完成后的一目了然摘要：本集新建了多少张角色卡/场景卡、又复用了多少
 *  已有素材——用户不必逐条悬停 provenance 提示才能知道"发生了什么"，呼应
 *  CLAUDE.md「过程/结果要让用户看得见」。人物与场景各自独立展示，某一类
 *  本集没有条目就不展示那一半。 */
export default function PrepPackDiscoverySummary({
  characters,
  scenes,
}: {
  characters: PrepPackCharacterAsset[]
  scenes: PrepPackSceneAsset[]
}) {
  if (!hasDiscoveryProvenance(characters, scenes)) return null
  const newCharacterCount = characters.filter(isNewlyDiscovered).length
  const knownCharacterCount = characters.length - newCharacterCount
  const newSceneCount = scenes.filter(isNewlyDiscovered).length
  const knownSceneCount = scenes.length - newSceneCount
  return (
    <p className="prep-discovery-summary" role="status">
      {characters.length > 0 && (
        <span className="prep-discovery-summary-item">
          人物：新发现 {newCharacterCount} 位（已建卡 · 已生成定妆照） · 索引历史 {knownCharacterCount} 位
        </span>
      )}
      {scenes.length > 0 && (
        <span className="prep-discovery-summary-item">
          场景：新发现 {newSceneCount} 个 · 索引历史 {knownSceneCount} 个
        </span>
      )}
    </p>
  )
}
