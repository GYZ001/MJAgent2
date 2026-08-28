# 分镜提示词架构决策

## 决策变更记录

**2026-08-24 原决策（已废止）**：分镜台产出供应商无关的结构化 IR，每个供应商一个
确定性编译器，派发时（生成台）选模型并编译；需要两套时对同一 IR 跑两个编译器。
IR 含 `frame_chain_intent` 支持 I2VA/FL2VA/L2VA 首尾帧链。

**2026-08-26 现决策（用户拍板，本文其余部分依此）**：三处推翻。

| | 原 | 现 | 用户依据 |
|---|---|---|---|
| 提示词怎么来 | 模型填结构化 IR，代码确定性编译成提示词文本 | **模型直接产出整块提示词文本** | 「提示词必须让模型生成，要把小说原文和事件划分都发给模型，让模型针对每个事件出提示词」 |
| 什么时候选模型 | 供应商无关，派发时选 | **分镜台前端先选，与生成台强绑定** | 「只需要在分镜台前端加个按钮…选择了哪个模型就只能用哪个模型生成视频」 |
| 跨段一致性 | 首尾帧链（段 N 末帧 → 段 N+1 首帧） | **只用参考图模式**，首尾帧链不做 | 「以后不用首尾帧这种方式了，只用参考图模式」 |

原决策的核心顾虑——两套提示词手工维护会漂移——在现决策下由**强绑定**解决：一集只
存在一套方言的提示词，不存在两份需要保持同步的真源。切换模型作废该集已生成的提示词，
不做静默转换。

## 上游变更：事件链取消

同日决策：剧本台改造成**映射台**，不再产出事件列表，只做三件事——本章新人物/新场景
发现、人物与地点到世界书图像素材的映射、模糊称谓到人物谱正名的映射。

因此「这一集有几段」的定量职责**搬到分镜台**：按 novel-to-storyboard SOP 第 1 步，
分镜台通读本章原文列节拍表，节拍按叙事单元归段，每段 15 秒 / 3-4 镜。段数由节拍决定，
不由上游给。这一条是整个改造的支点——不定死它，取消事件列表就等于没有任何东西决定
段数。

映射台自己的契约（`prep_pack`）也在同一晚经历了两次真实回归修复，均已合入
`app/production/prep_pack.py`（`PREP_PACK_VERSION`，现为 `2.0.2`）：2.0.1 是
`appellation_map` 的构造真源从「拿 `characters[].aliases` 反查」改成「解析时
原地记录结论」（`aliases` 的字面证据门槛与「这条提及有没有解析出身份」是两个
维度，混用会让模糊称谓在已解析成功的情况下从表里静默消失）；2.0.2 是事件链
取消（提交 `48e01ff`）留下的结构性回归——场景锚点候选表砍掉事件链那一路后只
剩 `[canonical_scene_name, name]`，而这两路在场景名是模型综合出的合成标签时
（EP1 五个场景全部如此，如"大青山山顶"不逐字出现在原文里）结构上必然落空，
`has_scene_anchor` 门禁具名拦截，5/5 场景全灭。修复把「逐字引文」证据形状下沉
到场景提及自己身上，不是在事件层面恢复证据（事件链本身不回来）。持久化契约
形状未变，两次都是补丁级版本号。

## 分镜台契约（2.0.0，冻结）

输入：本章小说原文（按 segment_index 分段）+ 映射台的 asset_manifest / appellation_map
+ 该集选定的 `target_video_model`。

```
{
  "storyboard_version": "2.0.0",
  "episode_no": int,
  "target_model": "seedance_2" | "minimax_h3",
  "beat_sheet": [{"beat_id": str, "summary": str, "segment_indexes": [int]}],
  "segments": [{
    "segment_no": int,
    "duration_s": 15,
    "synopsis": str,
    "source_segment_indexes": [int],
    "prompt_text": str,
    "shot_count": int,
    "dialogue": [{"speaker_identity_id": str, "line": str, "source_segment_index": int}],
    "resources": {
      "characters": [{"identity_id": str, "portrait_id": str|null, "description": str}],
      "scenes": [{"scene_id": str, "scene_reference_id": str|null, "description": str}],
      "props": [{"label": str, "description": str}]
    },
    "degraded_capabilities": [str]
  }]
}
```

三个字段的存在理由，不许在实现时"简化"掉：

- **`prompt_text` 是模型直接产出的整块可复制文本**，代码不再拼装、不再挂尾缀。skill
  明确要求每段提示词是一整块可直接复制的文本——把风格尾缀拆出去让用户自己拼，是最
  常见的人为失误来源。
- **`dialogue` 与 `prompt_text` 并存不是冗余**：`prompt_text` 是给模型看的，`dialogue`
  是给闸门看的，用于回查说话人在场与台词出处（见下，比对出处不比对措辞）。
- **`source_segment_indexes` 是验收的抓手**：交付判据是逐条比对原文，没有这个回指就
  只能靠人肉找。

`resources` 的映射规则：人物与场景尽量指向映射台的真实素材（`portrait_id` /
`scene_reference_id`）；实在映射不到的，以及物品类（世界书没有物品素材库），
`*_id` 留 null，只出文字描述。

## 契约版本：2.0.0 → 2.0.1 → 2.0.2

版本变化只动**生成时送给模型的输入形状与检测口径**；冻结的持久化形状
（`StoryboardPack`/`StoryboardPackSegment` 字段名与结构，即上面那份 JSON）自
2.0.0 起未变。`STORYBOARD_PACK_CONTRACT_MARKER`（现为 `storyboard_pack/2.0.2`，
写入每行 `Shot.prompt_contract_version`）是数据自带的版本标签，不是按集/按镜
维护的白名单——它存在的唯一目的是让 `resume` 判断「这一行是不是用当前这套
判据/提示词生成的」，marker 不一致就不能当作"已经用新判据生成过"直接复用。

| 版本 | 触发 | 改了什么 |
|---|---|---|
| 2.0.1 | EP1 真实回归（`ep_3d523ff4d0a4`/`run_46660b74d025`）逐段对照原文发现产出缺陷 | 送模型的 `task_payload.relevant_assets` 新增世界书标准外观/场景锚点（`appearance`/`scene_canonical`），phase 2 新增三条自洽要求，两套方言指令块各补一条硬要求 |
| 2.0.2 | EP1/EP6/EP7 十集回归横向核对，EP7 8 条角色引用全部自造前缀（`character:`/`char:`/`ch:`）且无一触发降级标记 | 修 `identity_id` 合法域判据的真值短路 bug（见下「判据范式转变」）；把提示词里「不许自造」的禁令式收尾改写成「取值域从哪来、必须逐字整串复制、未收录角色怎么写」的正面陈述 |

**已知且被接受的不一致**：写这份文档时，库里 10 个已生成分集只有 EP7
（`ep_621d93ac1231`）落在 2.0.2，其余 9 个（含只读的 EP1、EP6）仍是 2.0.1。
不做批量重跑：`app.production.storyboard_pack` 的持久化函数落库前先执行
`DELETE FROM shots WHERE episode_id=?`，`shots → shot_versions → jobs` 是
`ON DELETE CASCADE`——重新生成旧集会连同已经采纳/生成的视频一起删掉。旧版本
不是需要立刻处理的缺陷现场，是尚未重跑的旧产物，重跑与否由用户按需拍板。

## 台词闸门（唯一从 F1-F6 那批幸存的闸门）

原 F1-F6「内容不许编」批次是老「事件链→分镜大纲→逐镜」管线上的闸门，管线拆了闸门
跟着走。只有 F3 的内核升级留用，且**判据已于 2026-08-26 由用户放宽**。

**放宽前（已废止）**：每句台词必须逐字出自本章原文，切点落在句子边界。

**现判据**：不是小说里每句话都有剧情意义，台词允许省略、压缩、改写措辞，只要不偏离
本章剧情。闸门只剩两条：

1. **说话人必须在场**——该角色在这一段的原文里有在场证据，不是只出现过名字。
2. **每句台词必须有可溯源的原文段落**——`source_segment_index` 指向的原文里，确实是
   这个人在这个位置说了意思相当的话。措辞不比对，出处比对。

整句无出处的台词仍然拦下。

依据：EP6 那条「李富贵台词」的事故是两个问题叠加——台词内容是事件 summary 的压缩改写
（这一半现在被允许了），以及**这句话被安给了当时不在广场的人**（这一半是用户当时判定
「做的就是一坨屎」的主因，保留拦截）。新架构里台词不再经过 summary 中转，模型直接读
原文，压缩改写的失真源头本就被结构消掉了；剩下这两条闸门管的是「说话人张冠李戴」和
「凭空造话」。

**当晚（同一天）再放宽为不拦截**：以上两条规则的定义没有变，但见下一节——
用户在落地过程中进一步拍板，第一版分镜提示词连这两条也不拦截生成/确认了，
只记录不挡路。本节定义仍然是判据的权威来源，只是当前不生效。

## 第一版不设内容门禁（2026-08-26 当晚，用户拍板）

用户原话：「我认为第一版的分镜提示词先不需要任何门禁……只要格式没问题就直接
作用到下一环节」。这是比放宽台词闸门更进一步的决定，范围覆盖分镜台 2.0.0 的
**全部内容类判断**，不止台词：

- 上一节的两条台词规则（说话人在场、台词可溯源）；
- `resources.characters[].identity_id` / `resources.scenes[].scene_id` 是否
  真的是映射台已知身份（`[STORYBOARD_PACK_RESOURCE_CHARACTER_UNKNOWN]` /
  `[STORYBOARD_PACK_RESOURCE_SCENE_UNKNOWN]`）。

判据本身**照算不减**——`app.production.storyboard_pack._segment_content_
advisories`（生成时）与 `app.validators.validate_storyboard_pack_dialogue`
（确认时，经 `app.domain.video_ops._evaluate_storyboard_pack_for_
confirmation`）用的是同一套 `[STORYBOARD_PACK_*]` 标签、同一套判断逻辑，两处
各算一遍——只是结论从「拦截」改成「附着在产物上的可见信息」：生成时写进
`shots.shot_contract_json.storyboard_pack_segment.degraded_capabilities[]`，
确认时降级成 `WARNING` 级 Issue（进 `warnings`，不进 `structural_errors`），
不再让 `chat_structured` 重试、不再让分集卡在确认门禁上。

**仍然拦截的只剩形状问题**（`_validate_segment_draft`）：`prompt_text` 非空且
不超 `config.PROMPT_CHAR_LIMIT`；MiniMax H3 方言的三个固定字段名
（`integrated_multimodal_description:`/`overall_soundscape:`/
`non_diegetic_music:`）必须出现在文本里——写错字段名 H3 不报错，只会静默降级
成自由文本理解，这是接口语法问题，不是内容质量判断。结构层面的段时长必须
15 秒、`shot_no` 连续递增，走 `app.validators.validate_storyboard` 的既有短路
分支，同样保留阻断。

这是**第一版的临时状态**，不是「这两条判据被证明没用」的结论——上面两节定义
的规则仍然是未来重新收紧为拦截时的判据来源，只是当前不生效。

## 两套方言的差异（实现时按 target_model 二选一，不并存）

| | Seedance 2.0（provider `hiagent`） | MiniMax H3（provider `minimax_h3`） |
|---|---|---|
| 形态 | 中文自由散文 | 三字段 `integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music` |
| 时间 | 「镜头1/2/3」序号；秒数只写括号里当软提示 | `[Shot N] At 00:03.500` 严格递增真切点 |
| 多镜触发 | 15s 时长 + 首句「电影级预告片质感，多镜头叙事，镜头之间硬切」 | 原生，写几个 `[Shot N]` 就是几镜 |
| 语言 | 全中文 | 描述用英文；台词与屏上文字保留原文 |
| 角色引用 | `@角色名` + 参考图 | 素材编号 + 一句职责声明 |
| 台词 | 融进音频描述 | `(S1)` 说话人 ID + `<d>[Chinese] 原话</d>` |
| 屏上文字 | **能力缺失**：汉字必乱码 → 写「无字」，进后期合成清单 | 官方强项 → 双引号原文 |
| 音频 | 「全片贯穿」一段带过 | 拆两个字段，且**不许留空**（留空不等于静音，模型会自己补且不受控） |

**降级不许静默**：Seedance 侧的屏上文字能力缺失，必须写进该段的
`degraded_capabilities[]` 并产出后期文字合成清单。

## 模型无关的内容层（两套方言共用，只维护一份）

来自 novel-to-storyboard/references/failure-modes.md，每条都对应一个实测踩过的坑。
放弃首尾帧链后，前三条从"建议"提升为**生成时校验**——它们是跨段一致性仅剩的防线：

1. **连续性元素逐镜重复写**（头巾、发髻、腰带、伤疤）；只在开头的角色定义里写一次，
   段边界必掉。
2. **角色锚每镜重新点名**，不能指望模型记住三次切镜。
3. **群像锁死人数并加负向约束**；不锁，模型会自己加人，且加进来的往往面部崩坏。
4. 情绪写成面部肌肉动作，不写抽象情绪词。
5. 关键叙事道具锚定为构图约束（「始终清晰可见 + 画面位置」），不写「道具被抛出」。
6. 特效用物理描述代替文化词（「化作长虹」→「一道细长银白光带高速横穿画面并留下拖影」）。
7. 收尾段最后一镜必须是格局镜（大远景/升起拉远），否则片子断在人物中景上没有落点。
8. 失败重跑一次只改一处（主体、运镜、光线三者之一），否则无法归因。

## QC

成片质检（切点、色温、响度、面部一致性）与模型无关，直接收编
`docs/prompt-skills/novel-to-storyboard/scripts/qc_video.py`，接入生成台验收链。
依赖 ffmpeg/ffprobe 与 cv2/PIL/numpy，属 P2——先得有成片。

## 生成台判据大改造（2026-08-26 夜间，与分镜台产物直接相关）

分镜台 2.0.0 落地后，下游（生成台）的一批判据暴露出与旧架构耦合的问题，同一晚
一并处理。这些改动不在 `app/production/storyboard_pack.py` 里，但都是分镜台
产物能不能顺利流到生成台的直接前提，记在这里而不是散在别处。

### 被删除/被拆除的功能

- **VLM 视频质检整体下线**：`qa_shot()`（原 `app/stages.py`）及其输入准备
  函数已删；`grade_shot_video`（`app/evidence/media.py`）与
  `select_best_video_candidate` 改为**纯技术校验**——文件存在、容器格式、
  ffprobe 时长，函数注释原话「技术校验是唯一客观判据」；`qa_json` 类的视频
  质检结果不再产出/展示；监控页「视觉质检与评分」面板（标题/描述/`affects`
  三段完整内容）整段移除；`auto_qa` 设置项从 `app.config.DEFAULT_SETTINGS`
  删除。
  **独立子系统仍然活着，未受影响**：人物定妆照/场景参考图的 QA
  （`app/multiview.py` 的 `qa_json` 落库、`review_portrait_image`）是另一套
  机制；`vlm_request_concurrency` 设置项因此保留，监控页文案已改挂到"人物
  定妆照/场景参考图质检请求"。前端个别旧标签（如模型分配表里"视觉理解模型"
  一行的 note、调用日志 `vlm_qa` 的显示名仍写着"视频质检"）是未清理的历史
  文案残留，不代表功能还在——本轮未触碰这些字符串。
  **后续已清理（用户在观测里发现"检查视频画面质量"出现在根本没有视频的
  人物谱/映射台阶段）**：`vlm_qa` 的显示名改为"检查画面质量"
  （`app/observability/api.py`）、"画面质检"（`MonitorPage.tsx`），模型分配表
  该行 note 改为"定妆照、场景图与关键帧质检"。kind 本身仍活着且未改名——它
  覆盖定妆照、场景参考图、多视角整包与关键帧几何质检，只有视频那一路下线了。
- **冷观众审读与一次观看校准（叙事权威链路的下游验证子系统）整体删除**：
  `app/narrative_review.py`、`app/narrative_calibration.py`、
  `app/domain/narrative_calibration_ops.py` 三个模块连同对应测试文件一并
  删除。`app.production.publish` 里原本写
  `narrative_status`/`narrative_review_artifact_id`/`narrative_calibration_
  artifact_id` 的分支代码已确认是**从未被真正赋值过的死分支**（两个局部
  变量在删除前从未被任何调用方补上产出它们的那一步，`narrative_authority=
  True` 那一支永远不可达），删除不改变任何当前可达路径的行为。
  **未动的部分，容易被误认为是同一件事**：`app/narrative.py`、
  `app/stages.py` 里逐镜叙事闸门的路由、以及 `resolve_downstream_
  screenplay` 的 `narrative_authority_required` 分类逻辑本身，本轮未改——
  它们服务的是仍然存在的「叙事权威（`narrative_plan` 驱动）剧集」这条
  legacy 路径，不是被删除的冷观众审读子系统。提交 `108e2c1` 修的是这个
  分类标志此前在 `app/narrative.py` 的 `validate_storyboard_screenplay_
  authority`/`validate_storyboard_narrative` 里被忽略，导致
  `narrative_authority_required=False` 的 prep_pack 集撞上「不可能赢」的
  `NARRATIVE_PLAN_MISSING` 死循环（EP6 第七轮实测：42 镜每镜都会以同样方式
  撞死），与本节删除的冷观众审读子系统是两回事，不要混为一谈。
- **分镜台 → 生成台之间的「完成发布证据」人工确认闸门，对分镜台 2.0.0 管线
  已绕过；机制本身没有删，继续服务旧管线**（不是"整个拆除"，措辞要精确：
  被拆掉的是"新管线也要点一次确认"这个要求，凭证机制本身对旧管线原样保留）。
  `_assert_storyboard_generation_gate`
  （`app.domain.video_ops`）、`_has_current_storyboard_completion_
  certificate`、`storyboard_completion_certificate_id` 列、
  `app.production.certificate` 整套机制都还在，服务于 `narrative_
  authority_required=True` 的旧叙事权威管线。变化的是：`app.domain.
  storyboard_ops` 的工作台状态投影与 `app.domain.review_wall._review_
  upstream_snapshot` 的资格判定，现在对分镜台 2.0.0 集直接用
  `storyboard_pack_prompts_complete` 短路放行——不再要求用户额外点一次
  「确认视频提示词」才能推进。旧管线这个人工确认会把 `episodes.status` 推到
  `confirmed`；分镜台 2.0.0 产物齐全时发布证据在生成完成的同一个事务里已经
  自动落盘，没有等价的人工确认步骤，继续要求点击只会把新管线的集永远卡住
  （见下「判据范式转变」的同一族问题）。

### 判据范式转变：产物信号取代整体状态

这是本轮最值得记录的一条：**判据挂在一个会被正常操作改动的整体状态上，而
不是挂在"这件事本身成没成"上**，这个缺陷家族在同一晚至少复现了四次，分布
在互不相干的模块里：

- **`episodes.status` 白名单**：分镜台 2.0.0 跑完只落 `status='scripted'`，
  从不推进到旧管线用来放行的 `confirmed`/`generating`/`done`/`mixed`——那是
  给需要人工点一次"确认"的旧逐镜叙事管线设计的仪式，新管线没有等价步骤。
  凡是继续挂这份白名单的判据，都会把已经产出完整产物的分集永久判不过（真实
  复现：`ep_3d523ff4d0a4` 8 段全部通过、发布证据齐全，仍无法确认；
  `ep_0a7130b7b402` 六段视频提示词齐全仍卡在"分镜尚未完整确认"）。新增的
  `app.domain.common.storyboard_pack_prompts_complete(conn, episode_id)` 是
  替代判据的范例：只看每段 `shot_contract_json.storyboard_pack_segment
  .prompt_text` 是否非空 + 尾镜 `is_final=True`，完全不读 `episodes.status`。
  这个判据目前接入了生成资格判定（`app.domain.review_wall`）、resume 判断
  （`app.production.storyboard_pack`）、交付包 CAS 判据（`app.delivery
  ._episode_release_status_cas_clause`，在标准白名单基础上额外放行
  `scripted`）、工作台状态投影（`app.domain.storyboard_ops`）。
- **资格围栏用精确相等比较整集范围的素材清单**（`app.media_exec.run_job`）：
  校验一个已捕获的媒体生成资格快照是否仍然有效时，原判据是整集
  `asset_inputs` 的全集相等，导致并行兄弟镜正常新增素材互相打死——EP1 实测
  复现：镜 5/6/7 本身有效且已下载，只因为镜 5/6 自己的画廊在别的兄弟镜任务
  还没跑到自己的 checkpoint 时新增了条目，就被围栏拦下。修法：改成子集包含
  （`asset_contract(expected) <= asset_contract(current)`），且比较时排除
  本镜自己的资产——本镜的画廊是这次任务的输出而不是上游依赖，不能让"生成
  成功"这个动作自己作废自己捕获的令牌；被替换/删除的已捕获资产仍然会让子集
  判断失败，继续 fail-closed。
- **客户端持有的 `qualification_version` 因整集素材清单增长而自我作废**
  （`app.domain.review_wall._review_upstream_snapshot`）：同一族缺陷的另一
  处现场（`CON-409`/`ERR-20260826-3de956`）——点段 1 生成会让段 1 的素材进
  清单，段 2 此前拿到手的整集哈希随之失配，同一次真实操作把自己顶掉。修法
  是拆分而不是放宽相等比较：把"稳定事实"（剧本/分镜是否重发布、上游任务是
  否在跑、叙事权威判定）与"资产解析结果"分开算；稳定部分继续用严格相等（这
  类漂移必须 409）；资产部分按 `shot_id` 拆开各自求摘要，episode 级
  `qualification_version` 仍是整集操作（补齐全片/批量陈旧资产修复）专用，
  另给每镜一份 `shot_qualification_versions[shot_id]`，单镜生成/采纳只认
  自己这一份，兄弟镜新增素材不会出现在这份材料里。
- **`if known_character_ids and x not in known_character_ids` 的真值短路**
  （`app.production.storyboard_pack._segment_content_advisories`）：EP7 真实
  回归发现——`known_character_ids` 恰好是空集（prep_pack 没能把「孟浩」解析
  进 `asset_manifest`）时，`and` 短路成 `False`，整条判断被跳过，模型对
  同一角色自造的三种非法前缀引用一条告警都没触发。空取值域的正确含义是
  "取值域里什么都不合法"，不是"没什么好查的"；`set()` 对任何非空字符串的
  `not in` 天然为真，去掉真值判断后空集合会自动让每条引用都不合法，不需要
  为它单独开分支（scene 侧同构一并修）。这条修复只改变告警是否触发，不改变
  该判断是否拦截生成——判断本身在同一晚被降级为非拦截，见上面「第一版不设
  内容门禁」。

**替代范式**：判据挂产物信号，不挂会被正常操作动到的整体状态字段或全集聚合
快照。`storyboard_pack_prompts_complete` 是这条范式目前最完整的落地范例。

### 配套的落库/审计改动

- **`shots.adopted_version_id` 新增触发器 `guard_adopted_version_terminal_
  status`**（`app/db.py`）：`AFTER UPDATE OF status ON shot_versions WHEN
  NEW.status != 'succeeded'`，同事务把仍指向这个版本的 `shots.adopted_
  version_id` 清空，保证不变量"非空 ⟺ 那一版真有可用视频"在并发路径（QA 对
  `review_dependency_snapshot` 的新鲜度复核可能把已采用版本判失败）下也不
  破防；历史脏数据由 `_repair_dangling_video_adoption` 一次性修复，触发器
  只堵新增。
- **分镜台落库不再写占位 `shot_versions` 行**：第一次真生成即 v1。
- **`gate_decisions` 自动审计**（`app.domain.common.ensure_storyboard_pack_
  release_gate_decision`）：`storyboard_pack_prompts_complete` 判定通过后，
  给"分镜提示词齐全 → 可进生成台"这个转换点补一条留痕——`gate_key=
  'storyboard_pack_release'`（独立 key，不复用人工确认用的 `gate_key=
  'storyboard'`，避免自动记录被误当真人工确认、短路掉真正的确认路径）、
  `decided_by='system:storyboard_pack_prompts_complete'`（如实标注系统自动
  判定，不写 `'user'`）。幂等靠 DB 级部分唯一索引 `idx_gate_decisions_
  storyboard_pack_release`（`ON gate_decisions(artifact_id) WHERE gate_key=
  'storyboard_pack_release'`），并发重复写在 INSERT 层被挡住。**只记账，
  不拦截**：写失败只记日志、吞掉异常，不向上抛出挡住生成——审计是记账，不是
  闸门。

### 模型输出解析：JSON 恢复改用 schema 覆盖度择优

`app/harness/model_gateway.py` 的 `_latest_json_authority_root`：一次损坏的
模型响应里可能出现不止一个"独立成立、证明不是子结构"的顶层 JSON 根（例如流中
一个重复/畸形的 key 提前闭合了本该是根的对象，后面跟着一个恰好自己也能闭合
括号的裸数组）。**改前**：取扫描到的最后一个根，假设损坏总是"越往后越接近
正确答案"。**已证伪**（`ERR-20260824-7ab7cb`）：一个重复/畸形的 `scenes` key
提前关闭了本该是根的、数据完整（带合法 `characters` 数组）的对象，把它甩在了
一个只是自己括号闭合干净的裸尾随 `scenes` 数组前面——位置说明不了哪个是真
答案，反而是更早的那个才是。**改后**：候选按数据信号比较——优先取对象类型
候选（每个调用点最终都要一个 JSON 对象），对象候选之间比较各自覆盖了调用方
声明的 schema 里几个顶层字段（非空才算覆盖，`"characters": []` 与完全不提
`characters` 同分），**位置只用于打平局**（流式响应偶尔确实会"越写越对"，
打平局时这仍是合理的佐证，但不再是主判据）。丢弃候选时——不管最终选中的那个
是否通过校验——都产出 `STRUCTURED_JSON_RECOVERY_CANDIDATE_DISCARDED` 事件：
schema 校验只能验证选中的那一个，无法区分"这个字段本来就该是空的"和"这个字段
在被丢弃的候选里其实有数据"（`ERR-20260824-7ab7cb` 现场，选中的候选验证成一个
空 `characters` 列表，没有报出任何错误）。可见不拦截：这个事件从不改变控制流，
只留一条线索，让同类损坏形状能被找到而不是静默复现同一次数据丢失。

### 可观测性：worker trace 绑定与参考图展示

- **`video_create`/`video_poll` 等 `provider_calls` 现在带 `trace_id`/
  `run_id`/`step_run_id`**：`app.media_exec.run_job._worker_loop` 是单个
  长驻 `asyncio.Task`，会在同一个 Task 里连续 `await` 处理很多个 job，永远
  不会像 per-request handler 那样在两个 job 之间拿到全新的 Context，`with`
  形态的 `bind_trace` 因此覆盖不到它。新增 `app.observability.tracing.
  set_worker_trace(run_id, step_run_id)`，在 worker 每接到一个 job、发起
  任何供应商调用之前无条件覆写一次（即便 `run_id` 为空也要调用——否则一个
  没有持久化 `run_id` 的历史 job 会静默沿用同一循环里上一个 job 留下的
  trace）。这不是「同步依赖里写 ContextVar 会失效」那类陷阱：这里的调用与
  它之后的每次供应商调用都在同一个 asyncio Task、同一个 Context 里执行，
  没有跨线程传播的问题。
- **参考图在观测链路里改用身份标注 + 按需取图，而不是内嵌 base64**
  （`app.observability.api._compact_media_call_input`）：`video_create`
  请求体里每张参考图原本是 0.3~1.5MB 的 `data:` base64 URI，原样吐给前端
  会尝试渲染几 MB 文本直接卡死页面；改成每张图一个小对象——角色
  （`reference_image`/`first_frame`/...）、大致字节数、MIME、从
  `shot_versions.image_inputs` 取回的身份标注（角色名/场景名/衔接帧），外加
  一个可按需拉取这张原图字节的 `view_url`。提示词等文字字段原样保留、可展开
  查看全文，只裁剪图片的 base64 负载本身。

### 进行中

- **世界书/映射台/分镜台分环节文本模型选择**：截至本文档这次修订，工作区里
  没有找到任何相关代码（`app/model_registry.py`、`app/config.py`、
  `app/schemas.py` 均无按环节区分文本模型的字段或设置项，`app/model_
  registry.py` 也没有未提交的改动）。如果确有 agent 在并行推进这项功能，
  尚未落地到写这份文档时的快照里，其最终形态待补充记录，不要按本节描述
  假设它已经存在。

## 与既有代码的衔接

- `app/video_prompt_profiles.py` 的 `SEEDANCE_2_PROFILE` / `MINIMAX_H3_PROFILE` 是正确
  接缝，保留；但职责从「编译器规则」收窄为「交给模型的方言约束」。
- `app/video_prompt_ai.py` 的 `generate_ai_video_prompt` 位置不对：它挂在
  `app/media_exec/run_job.py:3393`，视频任务执行时才跑，吃的是单个 `Shot` 和内部
  Cinematic Continuity Contract，不是小说原文；且最终提示词字符串由
  `_render_seedance_prompt` / `_render_minimax_h3_prompt` 用代码拼。这条路径要上移到
  分镜台、换输入为原文+映射、去掉代码拼装。
- `app/compiler.py` 时长夹取已放开到 15s（提交 5d77e45）。
- H3 的固定指令行是模式信标，逐字符照抄 `official-format.md`，加断言测试锁字面量，
  防止被「顺手改写」。放弃首尾帧链后只剩 T2VA 与参考图模式，I2VA/FL2VA/L2VA 的对齐
  指令行不再需要。
