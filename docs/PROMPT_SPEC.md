# 提示词链规范（Prompt Spec）

> 对应 PRD §4.2~§4.4。每个 LLM 阶段 = 一个 prompt 模板 + 一个 Pydantic Schema + 一个业务规则校验器 + 修复回路。
> 本文件中的 prompt 是可直接使用的初稿；任何修改必须先跑金样回归（PRD §7）再合入。

## 当前生产合同（Blueprint 1.3.0 + IR Prompt 5.5 / IR v1.5，兼容 v3/v4 发布结构）

- 剧本写作前先生成 `screenplay-narrative-blueprint.v3`。模型只识别时间域、单一地点、
  人物位置、状态事实、重大决定依据和行为自主性；程序根据节点确定性生成
  `scene_plans`，剧本 IR 必须逐场消费。
- Blueprint 同时生成 `source_scene_owners` 和 `scene_derivations`。每个非标题 SRC
  必须且只能拥有一个 scene owner；跨场状态、决定前置和转场上下文只能通过显式派生
  关系传递，不得让另一场再次消费原 SRC。owner 冲突直接失败，不做静默去重。
- 蓝图采用可恢复分片协议。默认分片拥有 28 个连续 SRC；每片独立进行 Schema、来源覆盖、
  单一地点、时间关系、转场、状态引用和动机门禁。三次仍失败时只将当前片二分为 14，
  必要时继续二分到 7；已通过分片按来源哈希和边界状态哈希复用。
- 分片节点和事实由程序添加命名空间，跨片只通过上片输出的有效状态事实、人物位置和最近
  节点引用。分片合并后仍执行整集时间、空间、状态、自主性和来源顺序门禁。
- 每个 `scene_plan` 分开保存 `previous_scene_exit_state`、`opening_image` 和 `exit_state`。
  `entry_state` 只投影本场开场画面，禁止把车外或里间的上一镜状态当成本场入场画面。
- 场景复杂度由程序控制：同场累计 `dramatic_load <= 3`，且最多拥有 8 个 SRC。时间域、
  地点、回忆进出、显式边界或复杂度超限都会切场。
- 行为自主性使用结构化 `agency_mode` 和程序绑定的 `narrative_attribution`。受胁迫、
  失去行动能力与自愿选择不能在同一节点混写；生理反应、停止反抗或事后互动不能反向
  改写事件发生时的自主性。
- IR Artifact 必须记录其消费的完整蓝图哈希。蓝图变更后，无哈希或哈希不一致的旧 IR
  不得整版恢复，避免“蓝图已修、正文仍旧”；受影响正文必须重新投影并重新 QA。
- 人物身份使用稳定实体 key。原文有明确称谓时，`source_names` 必须逐字绑定授权原文，
  程序以来源称谓生成唯一显示名；未命名群众使用地点与戏剧职责构成稳定实体，不使用
  按出现顺序生成的临时编号，也不使用姓名黑白名单。

- 剧本 Baseline 使用 `screenplay-envelope.v1` + `screenplay-scene-shard.v4` 生成并合并为
  `screenplay-generation-ir.v1.5`。Envelope 只接收 Blueprint 全局摘要、集元数据和冻结
  identity registry，不接收完整原文；Scene Shard 只接收其 Blueprint scene plans、owned
  SRC、边界状态和冻结身份。模型负责场次有序单元、对白与观众意图；后端从 units 确定性生成 events、beats、动作阶段、
  audience priors、稳定 ID、
  精确来源 offset、状态重放、双向引用、窗口预算和最终 `EpisodeScreenplay`。
- 发布字段没有精简：`plot_spine/source_coverage/scene_outline/full_script_text`、
  `dialogue_chains/events/information_ledger/voice_bible` 及完整 `narrative_plan`
  仍在剧本发布前生成并通过既有验证，分镜不增加 IR 兼容分支。
- IR 场次的 `units` 是动作与对白的严格播放顺序。新 IR Artifact 通过
  `ScreenplayDocument.body_order` 保序往返；历史 Artifact 保持原投影行为。
- 模型不得生成最终 `S/E/F/A/SC/P/SE/RW/AP/AS/XI/XD` 编号及机械反向引用。
  这些字段只能由 `app.screenplay_ir.compile_screenplay_ir` 编译，避免模型把输出预算
  消耗在重复引用上，也避免同一关系在不同字段中漂移。
- IR 不再要求模型输出 events 或 beats。每个 `scenes.units` 单元必须声明其实际改编的
  细粒度 `source_segment_ids`；编译器按播放顺序先生成 event，再按 event 来源归属生成
  beat 和 `deliver/merge`。所有来源段都必须被正文 unit 消费，模型不得使用
  `context/coverage` 掩盖未进入正文的内容。
- IR 在 Pydantic 前接受可证明等价的供应商形状漂移：coverage 的
  `segment_ids/coverage_type/context_note`、单字符串 information、字符串形式的
  familiarity assumption，以及省略的 source_excerpt。归一化只改变容器/字段表示，
  不改写创作语义。
- 每个 Envelope/Scene Shard 只允许当前小单元的一次格式修复和一次语义修复。原始响应分别
  保存为 `screenplay_envelope_raw` / `screenplay_scene_shard_raw`，只有 Schema 与业务门禁均通过的
  normalized Artifact 才能复用。服务恢复按 contract version、Blueprint hash、identity registry
  hash、source hash、boundary hash 和 source ownership hash 复用已验证分片；禁止再次计费生成。
- 若供应商因长度上限在 events 等程序派生尾部截断，但顶层 `scenes` 数组已由标准 JSON
  decoder 证明完整，系统只恢复完整成员并从 units 重建尾部；不猜测或补闭未完成场次。
- `v1.4` 不允许未被 unit 直接消费的来源段发布；未知 ID、漏段、来源首次入戏顺序错误、
  对白错绑来源段、单 unit 过量挂载来源 ID都会在编译阶段失败。
- `audience` 只作为感知主体，不进入人物身份图；IR 实际引用但未预登记的功能身份按当前
  事件关系生成 contextual identity，不使用姓名或题材白名单。
- 关闭 `screenplay_scene_shards_enabled` 时，旧整版 IR 输出预算仍按细粒度来源段数量在
  `20480~36864` 内动态计算，仅作为回滚路径。默认候选链每个 Scene Shard 目标 16~24 units、
  5k~12k JSON 字符，单集分片并发最多 2。单 unit 最多合并
  12 个连续来源段；整集改编净文本不得低于原文的 35%，每 12 个来源段的局部窗口不得
  低于 18%。模型不再重复输出事件；事件、前置状态、完成条件、可观察证据和动作阶段由
  `scenes.units` 确定性派生。旧 `v1.1/v1.2` Artifact 继续按原分段合同恢复。
- IR 归一化采用“宽容读取、严格发布”：所有形状映射与编译派生写入 normalization/
  compiler audit；最终 QA 依据 Issue 的 `must_fix/runtime_blocking` 属性阻断发布，
  不按错误码白名单降级。
- 剧本以完整内容交付为目标。后端先把原文建立 `SRC*` 索引，最终
  `source_coverage` 必须逐段标记为交付、合并、上下文保留或有证据的重复；禁止静默删戏。
- 剧情节拍、场次数和镜头数量不设产品上限。目标时长只作节奏与成本参考，不能反向裁剪剧情。
- 分镜先生成整集导演规划，再按场景批量生成详细镜头；不同场景在受控并发门内并行，
  不再把一集拆成几十次串行单镜调用。
- 每场都有上下文建立窗口：主要动作发生前必须交代必要的时间、地点、空间轴线、人物位置、
  人物关系和关键道具；连续场景可以用动作、声音、视线或道具桥接，避免机械重复远景。
- 每镜必须写明存在理由、交付项和结果变化，并具备“景别 + 角度 + 运动”摄影三元组及摄影动机。
- 动作场景至少包含一镜中景/全景/远景配合跟随或横摇，保证动作路径可读；关键情绪转折至少
  包含一镜近景/特写配合固定或推近，保证情绪可读。
- 人物与场景图片在剧本发布后异步准备，不阻塞分镜文本；视频提交仍必须通过人物谱、
  风格和参考资产硬门禁。

## 0. 通用机制

### 0.1 AgentLoop（所有生成阶段共用）

每轮都必须持久化 Run、Step、候选 Artifact、结构化 Issue、Evaluation 与 Decision。剧本和场景分镜包最多 2 轮，
媒体阶段按合同使用更小的有界次数；达到 stall、预算、取消或外部故障条件时明确退出，不允许无限重试或把最后一次
未通过结果静默当成成功。修复 prompt 只接收当前 Issue、最近候选和必要 ContextPack，评估器恢复输出不能独立触发采用。

修复 prompt 模板：

```
你上一次的输出未通过校验。请修复以下具体问题后重新输出完整 JSON（不要解释，不要 Markdown）：
{逐条错误，含字段路径与期望值，例如：
- shots[3].duration_s=18，但视频生成时长必须由模型判断为 5~10s 整数
- shots[7] 台词纯文字 32 字（不计标点），超过 5s 口播上限 18 字；请缩短台词、拆镜或增加合理时长
- shots[9].characters 含"老者"，角色圣经中不存在，圣经角色为：林风/苏婉/赵天霸}
原输出：
{json}
```

> 关键：错误必须**具体到字段和数值**。1.0 失败教训之一是从不告诉模型哪里错了。

### 0.2 通用 system prompt 前缀

```
你是专业的竖屏漫剧（动态漫画短剧）编剧与分镜师。
你的观众看的是 AI 生成视频，不是摄影机实拍；请为模型能力写作，不为文学完整度炫技。
输出规则：只输出一个 JSON 对象，无 Markdown 围栏，无解释文字；字符串内部的英文双引号必须写成 JSON 转义形式。
所有内容使用简体中文。
```

### 0.3 上下文与修复成本预算

- 首轮生成必须显式携带代码校验的硬规则；不允许只在 validator 里新增规则、却不同步生成 prompt。
- 场景包只携带“本场导演任务相关的剧本/原文窗口”；完整剧情由场次结构和整集导演规划保底。
- 场内镜头在同一次输出中共享完整入口、出口和空间轴线；跨场只传递边界状态，避免输入随镜头数二次增长。
- 修复轮只回传“错误历史 + 最近输出 + 当前任务精简上下文”，不重发整本原文。
- 场景请求产出一个 `StoryboardScenePack`，其中包含该场全部详细镜头；输出预算使用
  `STORYBOARD_OUTLINE_MAX_TOKENS`，单镜接口仅用于旧数据恢复和局部修复。
- provider adapter 的快速重试之后，Harness 模型网关对可重试限流/网络/5xx 使用 30s / 60s / 120s
  的有界指数退避，并重放同一请求；该等待持久化为 `PROVIDER_RETRY_SCHEDULED` / `PROVIDER_RETRY_RESUMED`
  RunEvent。独占阶段运行同步进入 `WAITING_RETRY`，自动流水线的共享并发 Run 只记事件、不暂停健康兄弟任务。
  退避重试不新开 AgentLoop 修复轮，已通过的逐镜 checkpoint 保持不变。

## A. 角色圣经

**输入**：预算内原文章节正文 + 后段章节原文开头抽样；摘要不得替代来源证据。
**温度**：0.5

```
任务：从小说文本中提取角色圣经与世界观，用于后续 AI 视频生成的一致性控制。

要求：
1. 只收录出场 2 次以上或明显重要的角色，最多 8 个。
2. appearance_canonical 是该角色的"固定外观锚点串"：40~60 字，必须包含
   性别年龄感/发型发色/服装款式与颜色/1 个标志性特征。只写视觉可见信息，
   不写性格。原著未描写的部分，按题材合理补全并保持内部一致。
3. visual_style_canonical：25~40 字的全局画风串，包含 美术风格/光线/色调，
   适配竖屏漫剧（例如"国漫厚涂插画风，电影级体积光，高饱和暖色调"，
   但必须依据本书题材定制，不要照抄示例）。
4. speech_style 用于后续台词写作：句长习惯/口头禅/敬语习惯等，15~30 字。

小说文本：
{chapters_text}

输出 JSON Schema：
{
  "characters": [
    {"name": str, "role": "主角|重要配角|反派",
     "appearance_canonical": str, "personality": str,
     "speech_style": str, "relationships": [{"to": str, "relation": str}]}
  ],
  "world": {"era": str, "genre": str, "visual_style_canonical": str}
}
```

**业务校验**：characters 1~8 个；appearance_canonical 长度 30~80 字；name 互不重复；relationships.to 必须指向已收录角色。

## A2. 场景圣经（场景图素材库，2026-06-16 新增·跨集场景一致性核心）

> 动机：一致性体系原本只覆盖人物（角色圣经 + 定妆照 + 参考图注入）。场景仅是 `scene_setting` 文本标签，关键帧逐镜重生，同一地点跨镜/跨集环境漂移。本阶段把「角色圣经→定妆照」那条链平移到场景：先提取一份规范场景清单（场景图素材库底稿），出图入库，分镜场景收敛到库内，渲染时同场景复用同一张场景图。

**输入**：与角色圣经相同的原文预算（`_render_bible_source`）+ 已定稿角色圣经（取 `visual_style_canonical`/`genre`）。**温度**：0.5。**阶段**：角色圣经定稿后触发（`stages.generate_scene_bible`），与定妆照并行。

每个场景字段：`name`（稳定场景短标签，4~10 字，分镜 scene_setting 收敛到它）、`scene_canonical`（固定场景锚点串 30~60 字：地点/室内外/光线时段/标志陈设/氛围色调，纯视觉、无人物、贴合画风、非真人）、`location_kind`（室内/室外/其他）。

**业务校验**（`validate_scene_bible`）：scenes 1~40 个；name 非空且不重复；scene_canonical 长度 30~80 字。

**下游消费**：
- 分镜大纲 + 逐镜 prompt 注入「可用场景图素材库」清单（`_scene_library_block`），要求 scene_setting 收敛到库内场景名。
- 校验 V12（`validate_storyboard_scenes`）：每个 shot 的 scene_setting 必须能映射到库内规范场景（`match_scene_name` 容错匹配），命中回填 `shot.scene_name`；库为空时放行。
- 分镜阶段反应式发现库外新场景（`scenes.ensure_scenes_for_storyboard`，仿新角色发现），够戏份才补入库 + 出图。
- 渲染期（关键帧/视频参考图）按 `shot.scene_name` 取该场景的场景库图作为 `scene` 型参考图注入——同场景所有镜头、所有集共用同一张图 → 场景一致。

## B. 确定性剧集映射（非 Agent 阶段）

本阶段禁止调用 LLM，也没有分集 prompt。`app/planning.py` 是唯一实现：小说摄入按正则切章后，第 N 章确定性映射为
第 N 集，`source_chapters=[N]`，集号连续且不重叠。`title/hook/synopsis/cliffhanger` 是可编辑展示元数据，
不得替代原文章节作为剧本或分镜证据。`target_duration_s` 仅为历史兼容的节奏参考字段，不限制镜头数或整集总时长；
每个视频镜头由分镜模型按单一连续动作与口播密度判断为 5~10 秒整数；选择能自然完成内容的最短时长，超过 10 秒仍无法承载或进入不同节拍时继续拆为相邻镜头，直到完整覆盖本章剧情。

**业务校验**：章节数等于剧集数；每集恰好映射一个同序章节；映射阶段 provider 调用数必须为 0。

## C0. 剧本台「主线骨架 + 主线清单」（2026-07-25 · Renderability First）

> 关联 PRD：`PRD/剧本分镜主线压缩与视频能力适配方案.md`。
> 成功标准：可拍、可生成、可观看的主线节拍；禁止抠细节。

机制：当前剧本台由模型先产出按因果排序的 events 与严格保序的 scene units；后端从
events 生成 `plot_spine/spine_beats`，再投影正文、对白链和完整叙事权威图。以下
`key_lines/key_plot_points` 均为程序投影结果：

- `key_lines`：推动 spine 的主线台词，不设固定条数上限；按完整语义链保留，并由目标时长与逐镜口播容量约束。禁止为了凑数把人物谱原文台词全量入库。
- `key_plot_points`：4~8 条，与 spine 局势变化对齐。
- 单集戏剧契约：`dramatic_question` / `protagonist_goal` / `obstacle` / `stakes` 仍必填。
- 校验拦超纲细节词与 drop_list 回流；分镜数由剧情完整覆盖决定，仅保留 20 镜技术硬上限防止失控重复生成。

下游消费：分镜 prompt 注入 spine + 主线台词/剧情点；`validate_storyboard_preserves_key_content` 覆盖 must_keep spine。

源文预算：剧本台源文按 `SCREENPLAY_SOURCE_BUDGET_CHARS=24000` 注入；超预算时追加截断标记。

## C1. 可拍剧本（分集之后、分镜之前，2026-06-14 升级；节拍链当前未接入 live 链路）

> 动机：一段式分镜的产出是"把原文均匀切块塞满字数"——格式约束管得住密度，管不住戏剧结构。
> 紧凑的本质是**每个 5~10 秒镜头都有一次局势变化**，连贯的本质是**拍与拍构成因果链**。
> 张欣指出“不能拿小说直接分镜”，本阶段即承担小说 → 可拍剧本的改编职责：先定谁在场、谁说什么、什么动作可见、局势怎样变，再让分镜做视觉翻译。

**历史说明**：本节描述的独立节拍链未接入 live 链路，不构成活动合同。当前 live 链路直接从完整剧本生成分镜大纲并逐镜填充；镜头数不由兼容时长字段推导，逐镜模型按剧情、单一动作和口播在 5~10 秒内判断时长。

每拍字段：`day_offset + time_of_day + location`（时间数值化，**代码校验单调递增→机制性禁闪回**；场景标签由代码渲染如"次日清晨，场景A"）、`characters`（实际在场）、`dramatic_event`（谁做了什么）、`visible_action`（画面可见动作/表情/道具反应）、`key_dialogues`（关键台词，可空）、`turn`（局势变化/新信息）、`carry`（留给下一拍的钩子）、`beat_type`、`source_excerpt`（原文逐字摘录）。

结构校验：第 1 拍=钩子、末拍=尾钩、中段 ≥1 反转/高潮、禁止连续两拍铺垫、因果链（i+1 拍由 i 拍 carry 触发，prompt 约束）。

输出 JSON Schema：
```json
{
  "episode_no": 1,
  "beats": [
    {
      "beat_no": 1,
      "day_offset": 0,
      "time_of_day": "清晨|上午|中午|下午|傍晚|夜晚|深夜",
      "location": "主地点短标签",
      "characters": ["角色圣经准确姓名"],
      "dramatic_event": "谁做了什么",
- `continuity_mode` 是真实连续性语义；只有 `action_continuation` 使用上镜尾帧作为视频 0 秒起点。`transition` 存在后一镜上，表示它如何从前一镜进入，并由最终编辑执行
      "key_dialogues": ["本拍最值得保留的台词"],
      "turn": "局势变化/新信息",
      "carry": "留给下一拍的未完成动作或悬念",
      "source_excerpt": "小说原文逐字摘录"
  ]
}
```

## C2. 对拍展开（原 C 阶段）

第 i 镜实现第 i 拍。关键变化：
- `scene_setting` 必须逐字等于剧本表渲染标签（代码校验）→ 时间线与场景标签稳定
- `continuity_mode` 保留叙事/剪辑语义；视频工具层另按已发布 `FIRST_LAST_FRAME_MODE` 素材合同决定 0 秒输入。场景内第二镜使用首镜采用视频真实尾帧，第三镜起复用紧邻上一镜静态尾帧。
- `continuity_state_in/out` 保存场景版本/光线/轴线、人物 look/outfit/手部占用、道具 revision/owner/location/form/text_state；代码从上镜 `out` 继承未改字段
- `key_dialogues` 优先写入 dialogues；`visible_action + turn + carry` 决定 action_desc 的主动作、局势推进和镜尾落点
- 声轨纪律（2026-07-25 改：禁止旁白）：分镜只保留真实台词（dialogues）；`narration` 必须为空；禁止内心OS/画外解说。人群嘲讽/恭维写进 action_desc。不能把有对白的剧本压成纯画面卡。
- 静默镜纪律（v15 新增）：有效 `dialogues/audio_timeline` 为空时，`primary_action/action_desc/first_frame_desc/last_frame_desc` 不得要求人物开口、说完、打招呼、问话或做说话口型；否则确认门和视频提交前门禁同时失败，禁止让视频模型自行补词。
- 首尾帧纪律（v12 新增）：`first_frame_desc` 与 `last_frame_desc` 必须【同机位、同场景、同构图】，只让人物动作从开始推进到结束；二者不能完全相同（`_too_similar` ≥0.85 会退回），但绝不能变成两个不同的镜头/景别/场景——否则 5~10s 视频在两帧间出现反常识的跳变/形变
- 首尾帧执行纪律（v15 新增）：执行器必须先解析真实首帧，再把该首帧作为第一张 i2i 种子生成尾帧；尾帧缓存指纹包含首帧 SHA-256。视频 prompt 必须声明首图为 0 秒、尾图为结束时刻，并按反打/反应/角度变化选择连续横摇、弧移或推近，禁止同时出现“固定机位”和“换构图”。
- 物理与特效纪律（v12 新增，主要进图像/视频 prompt）：动作符合现实物理与人体运动规律，禁止瞬移/穿模/道具凭空出现消失；特效光效服从剧情，日常镜头写实克制、不堆满屏光效，仅高潮/爆发镜头用强特效且不遮挡面部
- `source_excerpt`：每镜必须带对应小说原文逐字摘录，作为 Seedance 兜底参考，不允许改写成摘要
- shot.characters ⊇ 该拍在场角色（代码校验）

## C. 单集分镜脚本（核心阶段）

**输入**：已生成可拍剧本 + 该集 source_chapters 原文全文 + hook/cliffhanger + 角色圣经 + 上一集结尾摘要（衔接用）。
**禁止**：分镜阶段不得使用 episode.synopsis；它只是前端展示字段，避免概要压缩导致细节丢失。
**温度**：0.7。每轮只输出一个镜头，使用独立、受控的单镜输出预算，不沿用整集剧本的超大 `max_tokens`。

**活动逐镜合同（Storyboard 2.1.2）**：系统先生成整集大纲，再按 checkpoint 一次只请求一个镜头。模型每轮根对象必须是
`{"episode_no": int, "is_final": bool, "shot": {...}}`，只允许单数 `shot`，禁止 `shots` 数组和顺带输出下一镜。
大纲 `covers` 若超过最长 10 秒的口播纯文字容量（不计标点），会在模型调用前按句读一次确定性拆成足够多的相邻节拍；单个无句读长句也会按同一计数口径切分，且不丢失内容。模型只负责当前镜，下一节拍由下一轮生成。
大纲动作容量与视频生成前门禁共用同一阈值：5~6 秒最多 2 个顺序动作节拍，7~10 秒最多 3 个。`primary_action`、`beat` 或 `covers` 暴露出超限动作链时，规划器优先确定性拆成前后相邻两镜；若逐镜扩写（例如补写人物入画路径）才导致超限，当前镜 Agent Loop 必须先定向修复，仍不可满足时由局部 Repair Router 拆该大纲节点，不重做整集。
修复轮会在候选之后再次声明这份输出合同；warning 回退必须让候选内容、残余 Issue 与 Artifact 来自同一次 schema-valid 迭代，退出提示按实际的 `stalled`、`no_quality_gain` 或 `max_iterations` 展示。

**功能性角色合同**：有姓名、原文明确称谓、重要或需要跨镜/跨集保持身份的角色使用稳定
实体 key；原文称谓逐字进入 `source_names`，正文、上下文、入场状态和对白说话人统一投影
同一显示名。原文确实未命名且无需持久定妆的群众，使用“地点 + 戏剧职责”稳定标识，
不按出现顺序编号。角色同一性由语义预检结合后续上下文判断，不使用姓名、服饰、性别或
题材词表猜测。决议与证据持久化到 episode，恢复与 Patch 后重放；任何未映射的身份都是
不可豁免的剧本发布 blocker，不得进入分镜。

```
任务：为漫剧第 {episode_no} 集《{title}》编写分镜脚本。

硬性输出规范（以下规则由代码校验，违反会被退回重写；请首轮直接满足）：
1. episode_no 必须等于 {episode_no}；当前只输出单数 shot，shot_no 必须等于系统指定的下一镜序号；整集落库后 shot_no 连续递增，不能跳号、重复或乱序。
2. 单集镜头数与总时长不设产品上限；必须完整覆盖剧本、必保留清单和结尾钩子后才能结束。
3. 按完整剧情逐项拆镜；关键台词、复杂动作或关键剧情点在单镜放不下时，继续新增相邻镜头拆分承接。
4. duration_s **默认 5s**；只能取 5~10 的整数。口播预算按**台词纯文字（不计标点）**：5s≤18字 … 10s≤36字；动作预算为 5~6s≤2 个顺序节拍、7~10s≤3 个；任一容量超限都必须拆镜或删减非主线细节。
5. 每条 shot 只表现【一个】连贯流畅的主动作，一镜到底拍这一件事，写清"起势→过程→收势"；入画/转身、穿行/走到、停下、操作道具、结果显现、开口都按实际顺序计入动作容量，禁止靠缩写 `primary_action`、扩写 `action_desc` 绕过。
6. 画面负责动作和表情，声轨只保留真实台词；禁止旁白/内心OS。不能把有对白的完整剧本压成纯画面卡。
7. 声轨纪律：主线对白写入 dialogues；人群/气氛声写进 action_desc；`narration` 必须为空字符串。
8. （v12 改）特效/光效服从剧情，不要每镜堆特效：日常对话与一般场景写实克制；只有情绪高潮或力量爆发镜头才用强特效，且不得遮挡人物面部。动作须符合现实物理与人体常识，禁止瞬移/穿模/道具凭空出现消失，复杂手势改简单稳定动作。
9. action_desc 要写清一个连贯动作的起势、过程、收势和可见反应；单镜只完成一个可拍动作，禁止在 5~10 秒内塞入多镜头快切。
10. source_excerpt 必填：每条 shot 必须带对应小说原文摘录，至少 8 字、不设上限，必须从下方"本集改编源文本"逐字摘录；可以截取最相关的连续段落，不要改写成摘要，不要写分镜解释。它会作为 Seedance prompt 的兜底参考。
11. 每个 5~10s 视频段必须推进一个明确的新动作、信息或局势变化。禁止单纯场景氛围、人物姿态、重复上一镜内容。
12. 【硬性·禁旁白】narration 必须为空；禁止内心OS/画外解说。无法开口的信息改用画面姿态表达。
13. 角色名必须准确：characters 不能为空；具体姓名、重要角色和跨镜持续角色只能使用角色圣经或剧本 `identity_contracts` 中的 canonical identity。功能身份只能引用剧本已签发的 `role_type=functional_character` 稳定 identity_id，并在 action_desc 或首尾帧中明确入画；不得根据职业、年龄、服饰、称号、体貌词或固定词表自行判定路人，也不得把来源称谓改写成按出场顺序编号的路人甲乙丙。幕后发消息者、纸条落款、屏幕昵称、AI 软件名不算出场角色。
14. action_desc 必须显式写出本镜头主要角色的准确姓名，不能只写"他/她/男人/女人/镜头/纸张"；每个动作节点都优先围绕人物表情、动作、道具反应和剧情后果展开。
15. dialogues 只写人物实际开口台词，dialogues[*].speaker 必须在本镜头 characters 中；不要把纸条文字、屏幕文字、手机通知写成 speaker。
16. 单句台词可按人物语气灵活长短，但单镜台词纯文字（不计标点）必须符合第 4 条所选时长的口播预算；关键长台词超过 10s 容量时请拆成连续相邻镜头分段说。emotion 只能取：平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定。台词从原著提炼为口语化短句，但优先保留关键细节和人物说话风格：{各角色 speech_style}
17. scene_setting 只是连续性标签，不是渲染重点，建议 18 字以内（不强制），只写"时间，地点"；能不写氛围就不写，禁止堆砌薄雾、灯光、杂物、墙面、天气等环境描写。镜头主要渲染故事情节和人物。
18. shot_size 只能取：远景|全景|中景|近景|特写；camera_move 只能取：固定|推近|拉远|横摇|跟随；transition 只能取：硬切|叠化|淡出淡入|黑场|闪黑|闪白|甩镜|遮挡转场|匹配剪辑|声音延续+叠化|声音先行+淡入。
19. 同一 scene_setting 的镜头必须连续排列，不能被其他场景打断；同一场景的 scene_setting 必须逐字相同，格式建议："时间，地点"。
20. shot_size 由当前动作、人物调度和情绪表达决定；剧情需要时允许连续镜头使用相同景别，不得仅为形式变化牺牲可拍性。情绪高点可优先考虑特写。
21. 相邻镜头必须有明确上下文接力：同场景连续镜头 continuity_from_prev=true，下一镜 action_desc 的开头必须承接上一镜结尾的动作、道具、屏幕内容或情绪；换时间/地点时 continuity_from_prev=false，且 narration 或 action_desc 必须写清转场原因/时间跳跃。
22. 转场设计：同场景连续镜只能用"硬切"；只要 scene_name/scene_time 与上镜不同，就选择明确换场方式。转场由 final_edit 执行；前镜尾帧和后镜首帧都保留干净稳定句柄，不在原始片段里预烧叠化/闪光/黑场。
23. 第 1 个镜头必须呈现本集 hook：{hook}
    最后 1 个镜头必须呈现悬念钩：{cliffhanger}

首轮输出前必须逐镜预检（这些就是代码校验器的具体判定条件，不要等返工）：
1. 按完整覆盖剧本规划所需 shot；duration_s 全部为 5~10 秒整数，由模型逐镜判断并选择能自然完成单一动作与口播的最短时长。逐镜复核动作容量（5~6s≤2、7~10s≤3）与口播容量；任一超限必须新增相邻镜头拆分承接。
2. 第 1 镜 continuity_from_prev 必须为 false；第 2 镜开始逐条和上一镜比较 scene_setting。
3. 如果本镜 scene_setting 与上一镜完全相同：
   - continuity_from_prev 必须为 true；
   - transition 必须为"硬切"；
   - characters 至少保留上一镜的 1 个核心人物；
   - action_desc 开头必须承接上一镜结尾的道具/屏幕内容/动作/情绪，不能重新介绍场景或重复上一镜发现。
4. 如果本镜 scene_setting 与上一镜不同：
   - continuity_from_prev 必须为 false；
   - transition 必须选择明确换场方式，绝不能用"硬切"；
   - narration 或 action_desc 必须写清承接原因、时间跳跃或线索带入，建议出现：次日、第二天、清晨、与此同时、随后、几小时后、带着 等承接词；
   - 上一镜 last_frame_desc 保留干净稳定的动作结果，本镜 first_frame_desc 是新时间/新地点的建立画面；不预烧转场特效；
   - 如果只是同一段连续动作里从房间走到门口/楼道/桌边/窗前，不要改 scene_setting，继续沿用上一镜主场景标签，把移动写进 action_desc。
5. scene_setting 是稳定短标签，不是镜头内容：同一连续时空统一写同一个"时间，主地点"，例如"当日，场景A"；不要在相邻镜头里改成"当日，场景A楼道外/桌前/门口"导致断链。
6. characters 只写本镜头实际可见/在场的人；屏幕发信人、纸条落款、新闻里提到的人、AI 软件名不算 characters。它们只能写在 action_desc 或 narration。
7. 每条 action_desc 必须显式写出 characters 中的准确角色名，把这一个连贯动作写清（2~4 个动作片段，不塞快切）；不要只写纸张、屏幕、镜头、场景自己在动。
8. 每条 shot 的 source_excerpt 必填，必须从本集原文逐字摘录至少 8 字（不设上限），作为 Seedance 生成兜底参考。
9. （v13 改）声轨预检：若完整剧本对应段落有“角色名：台词”，本镜必须写 dialogues；若有“角色名（内心/OS）：台词”，本镜必须写 narration 并以内心标签开头；合法功能性路人实际入画开口时可写 dialogues，泛化的人群嘲讽/恭维等集体声写入 narration 或 action_desc。整集至少约 75% 镜头要有 dialogues 或 narration。
10. first_frame_desc 与 last_frame_desc 必须同机位、同场景、同构图，只让人物动作从开始推进到结束，不要变成两个不同镜头/景别/场景。

常见错误 → 正确写法（角色A/场景A仅为占位示例，请替换成本集真实角色与场景）：
- 错：上一镜"当日，场景A"，本镜"当日，场景A楼道外"，transition="硬切"，又没有解释。对：若是角色A从房内走到门口，scene_setting 仍写"当日，场景A"，continuity_from_prev=true，action_desc 写"角色A攥着上一镜的纸页走向门口……"。
- 错：纸条上出现一个落款名就把 characters 写成 ["该落款名"]。对：如果画面只拍到角色A和纸条，characters 写 ["角色A"]，纸条文字放 action_desc/narration。
- 错：下一镜重新说"场景A昏暗、桌上有电脑"。对：下一镜直接从上一镜结尾继续，写"角色A仍盯着刚弹出的新闻推送，手指停在屏幕上，随后抬头望向门口，最后攥紧纸页。"。

分镜前置步骤（只在脑内完成，不要输出到 JSON）：
1. 先按原文顺序列出覆盖完整剧本所需的剧情段；模型先在 5~10 秒内选择能自然完成该单一节拍的最短时长，若超过 10 秒仍放不下或包含不同节拍，再拆出相邻承接镜头分担，不得把同一事件拆成互相重复的摘要，也不得跳到原文后文再跳回来。
2. 为每个剧情段记录"上一镜尾状态 → 本镜起始状态 → 本镜结尾钩子"。写 action_desc 时直接体现这个接力，让用户能从镜01一路读到最后一镜，不需要猜中间发生了什么。
3. 建立专名锁定表：角色圣经姓名只能逐字使用 {角色名列表}；原文中的地名、书名、软件名、屏幕/纸条文字、人名必须逐字照抄，不要猜新名字、改字、换同音字或把普通称谓升级成新角色。
   注意：专名出现在纸条、屏幕、新闻或旁白里，不等于它就是本镜头 characters；characters 只放实际可见/在场的人。
4. 如果原文用"我/他/她"，必须结合角色圣经和上下文还原为准确角色名；还原不了就用动作主体的普通称谓，不要编姓名。

创作要求：
- （v13 改）叙事主力=台词/内心OS+画面：能用角色对白说清的写 dialogues，无法说出口的内心变化写 narration，非角色圣经人物的人群声也写 narration 或 action_desc。少用的是解释性旁白，不是删掉剧本里已经存在的声音。
- 特效/光效服从剧情、动作符合现实物理：日常镜头克制写实、不堆满屏光效，仅高潮/爆发用强特效且不遮脸；单镜一个连续动作，人物位置/姿态/道具连续变化，不要瞬移/穿模/道具凭空出现消失。
- 场景描述能忽略就忽略：只保留最短时间地点标签；不要让薄雾、灯光、街道、杂物成为镜头主角。每个视频段的主角必须是人物、人物动作、人物反应和故事线索；场景只能服务于人物正在做什么、发现什么、失去什么、决定什么。
- `story_event_id` 必须始终输出 JSON 字符串；没有对应事件时输出空字符串 `""`，禁止输出 `null`。`source_excerpt` 中的双引号必须按 JSON 规范转义，或使用中文引号，不能破坏根对象语法。
- 每个镜头输出前完成自检：shot_no 连续、duration_s 全部为 5~10 秒整数且与动作/口播容量匹配、characters 非空且姓名准确、action_desc 出现准确角色名、source_excerpt 已从原文逐字摘录、scene_setting 足够短、文案满足信息密度下限且不检查上限、
  剧情载荷足够、action_desc 是一个连贯主动作（2~4 个动作片段、不塞快切）、台词 speaker 在本镜头 characters 中且不能是旁白、与上一镜有动作/道具/情绪/信息承接。

镜头连贯铁律（成片是否连贯取决于此，与 app/stages.py 同步）：
- 只有 `action_continuation` 把上镜尾帧当作本镜 0 秒输入；`same_scene_cut/reverse_angle/reaction_cut/insert_detail` 继承结构化状态，但重新构图。
- 相邻镜头以 `continuity_state_out -> continuity_state_in` 承接；无明示动作改变的人物、道具、轴线与光线字段由代码继承。
- 下一镜不要重新介绍同一场景，不要把上一镜已经完成的发现/动作重新讲一遍；必须推进到"因此发生了什么"。
- 如果必须跨时间或跨地点，transition 必须选择明确换场，禁止"硬切"；普通时空跳转用"淡出淡入"，情绪/回忆延续用"声音延续+叠化"，悬疑冲击用"闪黑/闪白"，动作追逐用"甩镜/遮挡转场"，构图呼应用"匹配剪辑"。continuity_from_prev=false，并在 narration 或 action_desc 写清"次日/几小时后/与此同时/他带着某线索来到某处"这类承接语；换场镜会使用自己的首图开启新场景。
- 每个场景首镜（链头）优先远景/全景交代环境，并生成自己的首图+尾图。
- 场景切换 transition 表示"从上一镜进入本镜"的方式；由 final_edit 使用 xfade/acrossfade 执行，生成模型只提供可剪辑句柄。
- 角色不得凭空出现，中途登场须写明入场方式。

本集改编源文本：
{source_text}
分镜改编依据：只以以上原文全文、hook、悬念钩、角色圣经和上一集结尾为准；episode.synopsis 仅用于前端展示，禁止作为分镜剧情依据。
角色圣经：{bible_json}
上一集结尾：{prev_ending}

输出 JSON Schema：
{
  "episode_no": int,
  "is_final": bool,
  "shot": {
     "shot_no": int, "duration_s": int,
     "shot_size": "远景|全景|中景|近景|特写",
     "camera_move": "固定|推近|拉远|横摇|跟随",
     "scene_time": str, "scene_name": str, "scene_setting": str,
     "characters": [str], // 可见的角色圣经姓名或 identity_contracts 已声明的稳定功能身份
     "action_desc": str, "source_excerpt": str, // 对应本镜头的小说原文逐字摘录
     "state_in": str, "primary_action": str, "state_out": str,
     "continuity_mode": "action_continuation|same_scene_cut|reaction_cut|reverse_angle|insert_detail|scene_change",
     "continuity_state_in": {"scene": {}, "characters": {}, "props": {}},
     "continuity_state_out": {"scene": {}, "characters": {}, "props": {}},
     "required_text": {"surface": str, "exact_text": str,
                       "strategy": "deterministic_insert|audio_only|embedded_prop|none",
                       "delivery_owner_shot_no": int|null},
     "story_event_id": str, // 无对应事件时为 ""，不得为 null
     "new_information_ids": [str], // 只引用 information_ledger 中的 I1/I2 等内部编号
     "narration": str|null,
     "dialogues": [{"speaker": str, "line": str,
                    "emotion": "平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定"}],
     "transition": "硬切|叠化|淡出淡入|黑场|闪黑|闪白|甩镜|遮挡转场|匹配剪辑|声音延续+叠化|声音先行+淡入",
     "continuity_from_prev": bool
  }
}
```

**业务校验器（代码实现，逐条对应修复反馈）**：

| # | 规则 | 错误消息模板 |
|---|---|---|
| V1 | 单集不设镜头数或总时长产品上限；完整覆盖剧本和尾钩后结束 | `分镜异常超过技术熔断值，请检查重复生成` |
| V2 | duration 为 5~10s 整数；口播按台词纯文字（不计标点）随时长增长：5s≤18 … 10s≤36 | `超过本镜 {d}s 的口播上限 {n} 字` / `台词纯文字 {x} 字（不计标点）` |
### D1. Seedance 结构化连续性提示词合同（`seedance_structured_continuity_v4`）
| V4 | 角色合法性；具体姓名必须来自角色圣经或 canonical identity contract；功能身份必须由 typed identity contract 明确声明且入画；禁止按角色名称词表推断；characters 非空；speaker 必须在本镜头 characters 中 | `既不在角色圣经中，也未被 identity contract 声明` / `功能身份未明确入画` |
视频最终 prompt 由 `app.compiler.compile_prompt` 确定性编译，合同版本写入 `prompt_contract_version=seedance_structured_continuity_v4`。核心输入是 `continuity_mode`、自然语言状态链与可比较的 `continuity_state_in/out`；仅 `action_continuation` 可使用上一镜尾帧作为 0 秒起点，其余模式必须重新构图。
| V6 | 场景连续性；scene_setting 只作时间+地点标签（长度不校验）；同场景必须接上镜，换场必须写承接 | `scene_setting"{x}"在 shots[i] 与 shots[j] 间被打断` / `缺少承接说明` |
| V7 | shot_no 连续 / 枚举值合法 | 同模板 |
| V8 | 单镜一个连贯动作：action_desc 目标 70 字（硬下限 40，够写清一个动作即可） | `action_desc 仅{x}字，低于硬下限 40 字` |
| V9 | 镜头数由完整覆盖剧本决定；只有技术熔断值，不设产品上限 | `分镜异常超过技术熔断值，请检查是否出现重复生成` |
| V10 | 完整剧本声轨：主线对白写入 dialogues；不再强制内心OS/旁白密度 | `分镜对白不足` |
| V11 | 必保留清单：`key_lines` 必须落到 dialogues；`key_plot_points` 落到 action_desc/声轨 | `分镜丢失了剧本标记的{x}条主线台词：…` / `分镜丢失了剧本标记的{x}条主线剧情点：…` |

> V5 的动作节拍检测使用共享动词词表与顺序分隔启发，并在分镜大纲、逐镜 QA、人工编辑保存、视频提交前复用同一容量阈值；V6 的上下文接力只挡明显断裂，核心仍由提示词要求模型先做连续剧情链。
> V11 配合剧本台的"必保留清单"机制（见 §C0），专治"重要台词/剧情在压缩中被静默丢弃"：剧本台先显式挑出绝不能丢的金句与关键反转并校验其写进了正文，分镜台再逐条落实并校验其仍在镜头里。务实优先（模糊匹配、只拦明显丢失），避免空耗修复轮次。

## D. Prompt 编译（确定性代码，非 LLM——列在此处仅为完整性）

### D1. Seedance 结构化连续性提示词合同（`seedance_structured_continuity_v5`）

视频最终 prompt 由 `app.compiler.compile_prompt` 确定性编译，合同版本写入 `prompt_contract_version=seedance_structured_continuity_v5`。核心输入是叙事 `continuity_mode`、已发布视频模式、首帧来源、镜间关系、自然语言状态链与可比较的 `continuity_state_in/out`。`FIRST_LAST_FRAME_MODE` 以实际输入首帧覆盖文本起点，并新增 `FIRST-LAST CONTINUOUS PATH`，不得再按 `same_scene_cut` 输出“不要沿用上一镜尾帧”的冲突指令。

最终 prompt 固定包含 FORMAT、REFERENCE ROLES、START STATE、ONE CURRENT ACTION、END STATE、STRUCTURED CONTINUITY、AUDIO TIMELINE、ON-SCREEN TEXT、DO NOT 等段落。`required_text.strategy=deterministic_insert` 时，原始视频明确禁字，精确中文由终剪渲染；转场也只由 final_edit 执行，避免双重转场。`source_excerpt` 只作为上游改编证据与校验依据，禁止进入 Seedance 最终 prompt。

信息台账实行“内部编号、中文语义”双层合同：`new_information_ids` 只保存 `I1`、`I2` 这类稳定去重键，界面通过 `information_ledger[].content` 展示中文内容；历史 snake_case ID 可保留用于兼容，但会在接口层派生中文说明。生成视频前，`do_not_repeat` 必须解析成中文剧情约束；无法解析的裸 ID 会被过滤，绝不直接发送给 Seedance。

```python
def compile_prompt(shot, bible, style) -> str:
    parts = [
        style.visual_style_canonical,                      # 画风锚点，逐字
        f"{shot.shot_size}，{shot.camera_move}镜头",        # 镜头语言
        shot.scene_setting,                                 # 场景，同场景逐字复用
        *[bible[c].appearance_canonical for c in shot.characters],  # 角色锚点，逐字
        shot.action_desc,                                   # 动作
        # source_excerpt 不进入 Seedance prompt；仅保留在上游证据与校验链路。
    ]
    text = "。".join(parts)
    text = enforce_length(text, LIMIT)    # 超长按 动作>场景>风格 之外的修饰语裁剪，
                                          # 角色锚点串永不裁剪
    assert shot.duration_s in range(5, 11)
    return f"{text} --ratio 9:16 --dur {shot.duration_s}"
```

负向词表（全局一份，随版本管理）：
`真人，照片质感，文字，水印，字幕，logo，多余的人，畸形手指，面部扭曲，名人长相，画面割裂`
（注入方式取决于 Seedance 2.0 是否有独立 negative_prompt 参数，M0 验证；无则追加到 prompt 尾部"避免出现：…"）

## E. VLM 质检

**输入**：镜头视频抽帧（首/中/尾 3 帧）+ 该镜头的 action_desc + 出场角色锚点串。
**温度**：0.2

```
你是 AI 视频质检员。对照预期检查这 3 帧画面（同一镜头的首/中/尾），输出 JSON。

预期画面：{action_desc}
预期场景：{scene_setting}
预期角色外观：
{每个角色的 appearance_canonical}

检查项（各 0~1 评分）：
1. character_match  角色外观与预期相符（发型/服装/年龄感）
2. action_match     画面内容与预期动作相符
3. clean_frame      无文字/水印/多余人物/肢体畸形

评分硬规则：
- `action_match` 和 `character_match` 是主项；核心动作未出现、错人或明显换脸/换装时，对应主项必须 ≤0.4。
- `overall <= min(character_match, action_match)`，画面干净不能掩盖错人或错动作。
- `issues` 只写可见、可定向修复的问题；达标时为空数组。

输出：{"character_match": float, "action_match": float, "clean_frame": float,
      "overall": float, "issues": [str]}    // issues 用一句话描述具体问题，
                                            // 将被拼入重生成 prompt 的负向区
```

**结果使用**：overall <0.6 且自动重试次数 <1 → 将 issues 追加到重生 prompt；否则进人工评审墙。若 VLM 输出非标准 JSON 或缺少必需评分，标记 `qa_recovered=true`：可展示恢复结果，但不得据此触发付费重抽。

## F. 金样回归（提示词变更的准入门槛）

- `golden/` 目录放 3 本固定测试小说节选（都市/玄幻/悬疑各 1，每本前 5 章）
- 回归脚本：对 3 本各跑 A→B→C 链，输出结构合法率、修复回路触发次数、V1~V7 违规分布、总耗时
- 任何 prompt 修改：先跑回归，三项指标（合法率不降、修复次数不升、耗时不升 20%+）通过才可合入
- 回归结果留档 `golden/runs/<date>.json`，趋势可查
