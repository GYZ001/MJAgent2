/**
 * 映射台域里的「分集映射包」（episode_prep_pack）契约类型。
 *
 * 从 screenplay.ts 原样搬移（2026-09-01）：那个文件已经顶在行数棘轮上，而映射包
 * 的资产条目要补两个展示字段（见 PrepPackSceneAsset 的 current_scene_*）——按
 * CLAUDE.md「装不下时先想怎么拆，不要先想加基线」，把自成一体的映射包类型整体
 * 拆出来，screenplay.ts 的基线随之调低。类型定义本身没有改动（新增字段除外），
 * 对外仍从 `../api` 桶入口导出，调用方的 import 路径不变。
 */

/**
 * 映射台（原「剧本台」，2.0.0 架构收窄）转型后的轻量分集映射包
 * （episode_prep_pack，字段/类型名不改，仅界面文案改名）。取代 EpisodeScreenplay
 * 成为映射台的发布产物，投影在
 * `Episode.prep_pack` 字段（不是 `Episode.screenplay`——后端把两种产物形状分到
 * 不同字段，见 Episode.prep_pack 上的注释）。旧产物（无 prep_pack_version 字段）
 * 仍可能出现在 `Episode.screenplay` 中，调用方必须先按 prep_pack_version 判别，
 * 见 ScriptPage.tsx 的 isPrepPack。基础形状冻结见 docs/TRANSFORM_FREEZE_PLAN.md
 * §3；字段随版本持续演进，均按可选处理，不假设某个具体版本号是终点。
 *
 * 2.0.0（架构收窄，见 app/production/prep_pack.py 模块 docstring 的 2.0.0
 * 说明）：映射台不再产出任何叙事内容——`event_chain`/`hook`/`cliffhanger` 全部
 * 撤销，职责收窄为三件事：①发现本章新人物/新场景；②把人物/地点映射到世界书
 * 已有的图像素材；③把原文里的模糊人物称谓映射成人物谱里的精准称谓
 * （`appellation_map`，新增）。哪一集有几个叙事节拍是分镜台自己从原文提炼的
 * 职责，不再是这里的产物。资产条目原来用 `event_ids` 记账"这个资产出现在哪些
 * 事件"，事件没了，改用 `segment_indexes`（原文段落序号，1-based）直接记录
 * "这个资产真正在场的原文段"——不是改名，是从原文重新推导，语义更精确。旧产物
 * （`event_chain`/`event_ids`/`hook`/`cliffhanger`）仍可能出现在尚未重新生成的
 * 已发布集里，前端不假设一定是新形状；本文件的类型只描述当前后端产出的新形状，
 * 读取旧产物时这些字段就是 undefined，调用方需按可选处理（同旧例）。
 */

/**
 * 1.7.0+ 字段：一条绑定的来源证明——method 取值 direct/alias/resolution/
 * resolution_forward/candidate_verdict/discovery/alias_inherited 等，具体见
 * app/production/prep_pack.py 的 _prep_pack_provenance。anchor_segments/
 * anchor_phrase 是绑定判据钉住的原文证据；forward_chapter_label/
 * source_episode_no 只在特定 method 下才非空。前端只把 method 当低调提示
 * （悬浮提示）展示，不做任何业务判断。2.0.0 起 characters/scenes/
 * functional_extras/props 四类资产共用同一个 provenance 形状（此前只有
 * characters 有类型化的 provenance）。
 */
export interface PrepPackProvenance {
  method?: string;
  anchor_segments?: number[];
  anchor_phrase?: string;
  forward_chapter_label?: string;
  source_episode_no?: number;
  dual_anchor?: boolean;
  candidate_verdict_attempted?: boolean;
}

export interface PrepPackCharacterAsset {
  identity_id: string;
  display_name: string;
  portrait_id: string | null;
  /**
   * 2.0.0+ 字段：取代 event_ids，这个角色真正在场（画面出场）的原文段号，1-based。
   * 2.0.0 之前的产物（如 1.11.x）没有这个字段——运行时是 undefined，不是空数组；
   * 调用方必须把「字段不存在」和「测量后是 0 段」分开显示，不许把前者渲染成后者，
   * 见 ScriptPage.tsx 的 isLegacyPrepPackFormat / assetCoverageText。
   */
  segment_indexes?: number[];
  /** 1.2.0+ 字段；本集内对该角色的称谓（如「小胖子」）。之前的产物没有它。 */
  aliases?: string[];
  /**
   * 1.7.0+ 字段：画面与字幕分离（见 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.3）。
   * visual_entity_id 全局稳定，决定用哪张定妆照取图；display_name 绑定成功后会被
   * 改写为全局规范名，取图仍看 visual_entity_id 不受影响，两者语义分工不同。
   * 1.7.0 之前的产物没有这个字段。
   */
  visual_entity_id?: string;
  /**
   * 1.7.0+ 字段：本集原文对这个人的称呼（如第 1 集「银色长袍女子」），决定字幕/
   * 台词显示——刻意不提前剧透 display_name 这个全局规范名。1.7.0 之前的产物没有
   * 这个字段，此时前端只能退回展示 display_name。
   */
  display_appellation?: string;
  /** 1.6.0+ 字段；这条绑定是怎么判出来的，见 PrepPackProvenance。 */
  provenance?: PrepPackProvenance;
  /**
   * 非持久化展示字段（GET /episodes/{id} 投影时按当前状态实时算出，不进
   * 已发布 prep_pack 产物本身）：这个身份按本集集号解析出的「当前实际会用
   * 的那张」定妆照，与生成时 app.portraits.current_portrait_ref 同一份判据
   * ——portrait_id 是发布时固化的快照，只做溯源，不代表当前状态。两者不同
   * 时前端应提示"已更新"；current_portrait_id 为 null 表示当前无可用定妆照
   * （不得回退显示 portrait_id 对应的旧图）。
   */
  current_portrait_id?: string | null;
  current_portrait_image_url?: string | null;
}

export interface PrepPackSceneAsset {
  scene_id: string;
  display_name: string;
  scene_reference_id: string | null;
  /**
   * 非持久化展示字段（GET /episodes/{id} 投影时按当前状态实时算出，不进已发布
   * 产物本身），语义与 current_portrait_* 对称：这个场景按本集集号解析出的
   * 「当前实际会用的那张」场景图，与生成侧同一份判据（后端
   * app.multiview.scene_row_for_episode）。scene_reference_id 是固化快照，只做
   * 溯源——出图解耦到后台后，映射那一刻它恒为 null，拿它查图必然查不到。两个
   * 字段为 null 表示当前没有可用场景图，不得回退显示快照对应的旧图。
   */
  current_scene_reference_id?: string | null;
  current_scene_image_url?: string | null;
  /** 2.0.0+ 字段，undefined 于旧产物——语义同 PrepPackCharacterAsset.segment_indexes。 */
  segment_indexes?: number[];
  provenance?: PrepPackProvenance;
}

/**
 * 2.0.0 新增：道具/物品——世界书没有道具图像素材库，只有文字描述
 * （description），不映射任何图片，见 app/production/prep_pack.py 的
 * _prep_pack_build_prop_manifest。
 */
export interface PrepPackProp {
  label: string;
  description: string;
  /** 2.0.0+ 字段，undefined 于旧产物——语义同 PrepPackCharacterAsset.segment_indexes。 */
  segment_indexes?: number[];
  provenance?: PrepPackProvenance;
}

/**
 * 1.3.0+ 字段：群演 / 一次性人物——没有定妆照是设计使然（不进人物谱身份体系），
 * 不是数据缺失，前端展示时用统一占位图标，不当成"图片没找到"处理。
 */
export interface PrepPackFunctionalExtra {
  label: string;
  /** 2.0.0+ 字段，undefined 于旧产物——语义同 PrepPackCharacterAsset.segment_indexes。 */
  segment_indexes?: number[];
  visual_entity_id?: string;
  provenance?: PrepPackProvenance;
}

export interface PrepPackAssetManifest {
  characters: PrepPackCharacterAsset[];
  scenes: PrepPackSceneAsset[];
  /** 2.0.0+ 字段；更早的产物没有它，读取时按可选处理。 */
  props?: PrepPackProp[];
  /** 1.3.0+ 字段；1.2.0 及更早的产物没有它，读取时按可选处理。 */
  functional_extras?: PrepPackFunctionalExtra[];
}

/**
 * 2.0.0 新增：把原文里的模糊人物称谓（如「那少年」「小胖子」）映射到人物谱里的
 * 精准称谓——asset_manifest.characters[] 已有的别名消歧结论，按 (原文称谓,
 * 原文段号) 逐条摊平展示，供人工核对"这一段原文里的这个称谓，系统认为指的是
 * 谱内哪个人"。只覆盖已解析到 identity_id 的人物提及，不含 functional_extras
 * （群演没有精准身份可映射）。
 */
export interface PrepPackAppellationMapEntry {
  raw_mention: string;
  segment_index: number;
  identity_id: string;
  canonical_appellation: string;
}

export interface PrepPackEpisodeScope {
  chapter_indexes: number[];
  source_segment_count: number;
}

/**
 * 覆盖账本四账 + uncovered 的元素形状后端未最终敲定（frozen payload 示例给的是空数组），
 * 防御性地按 number 或 {segment_index} 两种可能解析，两者都不匹配时原样兜底展示。
 */
export type PrepPackCoverageEntry = number | { segment_index?: number | string } | Record<string, unknown>;

export interface PrepPackCoverageLedger {
  total_segments: number;
  delivered: PrepPackCoverageEntry[];
  merged: PrepPackCoverageEntry[];
  retained_as_context: PrepPackCoverageEntry[];
  proven_duplicates: PrepPackCoverageEntry[];
  uncovered: PrepPackCoverageEntry[];
  /**
   * 第五账（1.4.0+ 字段，1.3.0 及更早的产物没有它）：副文本——章节名/作者留言段等
   * 不属于正文、但已被合法计入覆盖的原文段。不算未覆盖，参与"已覆盖"总数
   * 的并集计算，见 ScriptPage.tsx 的 coverageGateSummary。
   */
  paratext?: PrepPackCoverageEntry[];
}

export interface EpisodePrepPack {
  prep_pack_version: string;
  episode_no: number;
  episode_scope: PrepPackEpisodeScope;
  asset_manifest: PrepPackAssetManifest;
  /** 2.0.0+ 字段；更早的产物没有它，读取时按可选处理。 */
  appellation_map?: PrepPackAppellationMapEntry[];
  coverage_ledger: PrepPackCoverageLedger;
}
