# 全链路叙事连续性与观众认知泛化整改 PRD

> 版本：v1.3
> 状态：Proposed
> 日期：2026-08-04
> 适用范围：章节原文 → 叙事命题与事件图 → 剧本 → 角色/观众认知 → 分镜规划 → 叙事盲审 → 分镜发布
> 基准事故：《我欲封天》第 6 集《铜镜的快乐》
> 文档边界：只描述叙事、剧本、导演意图、观众理解和分镜方案；不描述视频生成、模型媒体输入、候选素材、后期合成或声音工程

---

## 0. 执行结论

本次整改的核心不是增加转场，也不是为第 6 集补几个特写，而是建立一条可验证的叙事真相链：

```mermaid
flowchart LR
    A["章节原文与来源证据"] --> B["统一叙事命题"]
    B --> C["故事真值与事件因果图"]
    C --> D["角色信念与人物决策链"]
    D --> E["剧本与场景戏剧合同"]
    E --> F["观众体验意图与认知任务"]
    F --> G["原子分镜与镜头贡献"]
    G --> H["状态链与叙事边界合同"]
    H --> I["冷观众叙事盲审"]
    I --> J["叙事就绪分镜包"]
```

原文事实与改编事实必须以显式改编关系连接；进入改编故事后，同一个剧情事实在剧本、角色认知、观众认知和分镜中必须保持同一身份。下游不能重新从散文中猜测“现在讲的是哪件事”，也不能拿原文证据直接证明已经被改写的事实。

第 6 集暴露的不是孤立问题，而是一条结构性失效链：

1. 剧本仍带未解决的主线缺失进入分镜；
2. 分镜把“未交付”统一处理为复制相邻镜并插入；
3. 动作容量依赖固定动词子串，未见表达会被低估；
4. 事件 ID 只验证格式，不验证真实引用；
5. 关键转折虽然“写进了镜头”，却没有独立可读时间；
6. 没有维护观众此刻知道什么、怀疑什么、忘记什么和应该疑惑什么；
7. 去重逻辑不能区分无意义重演和具有验证价值的功能性重复；
8. 单镜正确不等于场景、段落和整集的戏剧推进成立。

整改后，每个镜头都必须有明确的观众功能，并至少改变以下一项：故事状态、角色状态、观众认知、情绪压力、时空理解或戏剧问题。没有任何增量的镜头必须合并或删除。

---

## 1. 文档范围

### 1.1 本 PRD 负责

- 原文事实与改编事实的可追溯关系；
- 剧情事件的因果顺序和状态变化；
- 角色知道什么、误解什么以及为何做出决定；
- 观众应注意、理解、怀疑、期待和感受什么；
- 新命题进入故事时是否需要铺垫、强调、反应、验证或唤回；
- 场景、段落和整集的目标、阻力、升级、转折、兑现与结尾状态；
- 分镜的动作容量、信息容量、注意容量和处理时间；
- 相邻分镜的状态、视点、时空、情绪和认知承接；
- 重复动作、关键转折欠交付、人物动机断裂和认知缺口的泛化修复；
- 剧本/分镜级冷观众盲审、人工一次观看校准和发布门禁。

### 1.2 明确不负责

- 视频生成模型及其输入模式；
- 视频提示词的供应商语法；
- 参考图、首尾帧、参考视频或媒体发布；
- 视频任务队列、并发、时延、成本和失败重试；
- 视频候选选择、媒体质量检测和后期合成；
- 转场滤镜、响度、混音和成片编码。

本 PRD 只输出完整、可验证的叙事和分镜合同，供其他生产环节消费，但不规定它们如何实现。

---

## 2. 强制原则：禁止白名单式补丁

### 2.1 禁止的实现方式

以下方案不得进入主链路：

- 新增固定动作词表来识别本次事故中的“打坐、发热、爆裂、倒地”等表达；
- 为某部小说、某一集、某个角色或某个镜号增加特判；
- 用有限同义词表决定剧情覆盖、动作重复或状态承接；
- 用正则命中某些中文短语后直接决定拆镜、插镜或留白；
- 把某个问题码永久映射为唯一修复动作；
- 写成 `if new_object: insert_closeup()`、`if new_rule: add_reaction_shot()`；
- 按人物、道具、能力、地点、规则等内容类别维护不同引导模板；
- 仅用场景名相同、镜号相邻或共享若干汉字判断连续性；
- 为第 6 集维护“不得重复打坐”“铜镜必须特写”等静态清单；
- AI 不确定时回退到关键词词表继续判定。

### 2.2 允许且必要的稳定合同

以下属于通用结构约束，不属于白名单：

- 稳定 ID、真实引用和来源证据；
- 事件图有向无环、因果父子顺序和状态前后关系；
- 主要交付所有权、认知截止点和证据可达性；
- 单镜容量、处理时间和状态增量；
- 叙事草稿、需要复核、叙事就绪等工作状态；
- 由当前项目动态生成的实体、命题、已交付动作和未来保留事件；
- 经跨题材盲审校准的阈值，但阈值不得绑定剧情词或题材类别。

判断标准：**确定性规则约束数据关系，AI 负责理解具体语义；任何规则都不能枚举“什么剧情应该拍成什么镜头”。**

### 2.3 泛化验收

方案必须同时通过：

1. 第 6 集事故回归；
2. 未见过和人为虚构的动作、实体与规则；
3. 同义改写和语序变化；
4. 人物、道具和地点重命名；
5. 都市、悬疑、修仙、喜剧和纯对白等不同题材；
6. 有意悬念、戏剧反讽、回忆和时间跳跃；
7. 随机事件图中的乱序、重复、状态回退和认知缺口。

只修好第 6 集不视为完成。

---

## 3. 产品目标与验收指标

### 3.1 产品目标

1. 每个当前作用域必须交付的剧情事件拥有稳定命题、来源、因果位置和主要交付责任。
2. 角色行动能追溯到该角色实际获得的感知、判断、目标变化和选择。
3. 观众理解只来自已经呈现的分镜证据，不读取故事真值或未来答案。
4. 新命题是否需要额外引导，由认知缺口和容量推导，不由内容类别决定。
5. 关键转折获得独立可读窗口，但不强制一定物理切镜。
6. 非动作镜头只要具有认知、情绪、空间或戏剧贡献，即可合法存在。
7. 同一动作默认不重复交付；具有新观众增量的验证、对照或唤回可以保留。
8. 单镜、场景、段落和整集四个层级都具有可验证的戏剧推进。
9. 局部修改先形成候选叙事方案，完成整集重验后才替换正式方案。
10. 所有判断在同义改写、实体重命名和新题材下保持语义等价。

### 3.2 核心指标

| 指标 | 目标 |
|---|---:|
| 当前作用域必交付事件结构化覆盖率 | 100% |
| 命题、事件、动作、镜头引用合法率 | 100% |
| 事件因果乱序 | 0 |
| 未授权的主要动作重复 | 0 |
| 相邻分镜无因状态回退 | 0 |
| 关键转折独立可读窗口缺失 | 0 |
| 关键人物决策动机断裂 | 0 |
| 关键认知任务超过截止事件仍未交付 | 0 |
| 冷观众稳定形成非预期错误因果 | 0 |
| 有意悬念被提前解释或遗漏被误标为悬念 | 0 |
| 无功能镜头 | 0 |
| 白名单或剧集特判驱动的修复分支 | 0 |

### 3.3 质量边界

AI 盲审只能作为一致性和理解度代理，不能单独证明“大众一定喜欢”。叙事就绪阈值必须使用跨题材真人一次观看测试校准，并持续比较 AI 盲审与真人理解结果的相关性。

---

## 4. 全链路不变量

### 4.1 唯一命题与引用不变量

同一叙事域内的同一个命题只创建一个 `proposition_id`。原文事实与改编事实属于不同叙事域：只要事实内容发生实质变化，就必须创建新的改编命题，并通过 `AdaptationDecision` 连接，不能让原文证据直接为改写后的事实背书。事件、角色信念、观众信念、台词、动作和镜头只能引用相应叙事域中的命题，不能各自创建同义 ID。

格式正确不代表引用正确。所有引用必须验证：

- 目标存在；
- 属于当前项目和剧集；
- 所引用内容与当前叙事关系一致；
- 来源与改编权限明确；
- 没有把事件 ID、动作 ID、命题 ID 和镜头 ID 混用。

### 4.2 因果顺序不变量

事件必须形成有向无环图：

```text
preconditions + causal_parent_ids
    -> trigger/actions
    -> effects
    -> resulting state
```

分镜顺序必须是事件图的合法拓扑排序。修复不得把结果放到原因之前，也不得让角色在获得信息之前依据该信息行动。

### 4.3 唯一主要交付不变量

- 每个在当前叙事作用域内必须呈现的事件、原子动作，以及每一次明确的观众信念转移，只有一个主要交付窗口；
- 其他镜头只能提供支持证据、人物接收、验证、对照或唤回；
- 支持性重复必须声明新增的观众、角色、情绪或戏剧增量；
- 同样动作、同样结果、同样理解不得保留。

主要交付所有权绑定 `ExperienceIntent.audience_paths[].target_deltas`，不永久绑定命题本身；因此同一命题可以分别完成“未知→怀疑”和“怀疑→确认”，但同一观众路径中的同一次转移不得被多个窗口重复交付。被导演意图标记为 `withheld` 或由 `NarrativeArcContract` 带入未来作用域的命题，不要求在本集主要交付，但必须记录隐藏理由、未来揭示锚点或继续保留的戏剧问题。原文中的“必须保留”只约束改编不可无因丢失，不自动等于“本集必须向观众揭示”。

### 4.4 状态单调不变量

同一时间域内，已完成动作和已成立事实不能无原因回到未发生状态。合法回退必须有显式的时间域、回忆、梦境、重置或叙事视点依据。

### 4.5 角色知识不变量

角色只有在可感知、被告知或可合理推断后才能更新信念。重要行动必须存在：

```text
感知证据
→ 角色判断
→ 目标/情绪/关系变化
→ 选择
→ 行动
```

步骤可在同一节拍中完成，但不能只存在于作者解释中。

### 4.6 观众认知不变量

系统并行维护：

```text
StoryTruthGraph       世界实际成立什么
CharacterBeliefGraph 角色分别相信、怀疑或误解什么
AudienceBeliefGraph[prior]
                      每类目标观众根据此前分镜能够相信、怀疑或误解什么
```

每条观众先验路径的目标状态必须分别满足：

```text
prior audience state
    + presented storyboard evidence
    + permitted inference path
    -> intended audience state
```

若创作意图是悬念，观众至少应形成正确的问题；若创作意图是理解，关键命题必须在第一次被后续剧情依赖前达到目标置信度。

### 4.7 镜头功能不变量

每个镜头必须声明观众功能，并至少改变以下一项：

- 故事世界状态；
- 角色知识、目标、关系或情绪；
- 观众信念、疑问、期待或注意；
- 空间或时间理解；
- 场景压力、价值极性或节奏。

`primary_action_id` 可以为空。空间建立、反应、停顿、人物接收、关系变化和信息唤回均可成为合法镜头，但其非动作贡献必须非空。

### 4.8 分镜边界不变量

相邻分镜必须满足：

```text
previous.planned_state_out
    --narrative_boundary_contract-->
next.planned_state_in
```

允许变化必须来自当前动作、明确时间/地点变化、视点变化或可见范围变化。边界还必须说明观众为何此时需要切换注意，以及上一镜留下的问题如何被下一镜承接。

世界状态只需一条事实承接链；观众状态必须对每个 `AudiencePriorContract` 分别承接，不能用一个平均观众状态覆盖所有先验。

### 4.9 容量不变量

单镜容量同时考虑：

- 动作阶段最短可读时间；
- 对白和文字阅读时间；
- 注意目标切换成本；
- 新命题推断与人物反应时间；
- 空间重新定向时间；
- 镜头开始建立与结束落稳时间。

容量不能通过“识别到几个动词”代替。动作阶段和认知负荷由 AI 提议，结构校验器验证状态效果、时间非负、总量和关系一致性。

### 4.10 证据与不确定性不变量

- 故事真值、角色信念、观众信念和盲审观察分开保存；
- AI 无法确认时必须输出 `unknown/needs_review`；
- 盲审观察不得回写故事真值；
- 所有自动修复必须保留问题证据、候选方案和选择理由；
- 不得用来源原文或目标答案证明分镜已经让观众理解。

### 4.11 上游变化触发重审不变量

上游命题、改编决策、事件因果、角色目标、观众意图或镜头所有权发生变化后，所有受影响的剧本、分镜、认知状态和盲审结论都必须重新验证，未重验结果不得继续作为叙事就绪依据。

---

## 5. 统一领域模型

### 5.1 来源证据 `SourceEvidence`

```json
{
  "source_evidence_id": "SE-...",
  "source_span": {
    "chapter_id": "...",
    "start": 0,
    "end": 0
  },
  "verbatim_excerpt": "原文片段",
  "confidence": 1.0
}
```

来源证据只负责证明原文说了什么，不直接代表观众已经获得该信息。

### 5.2 统一叙事命题 `NarrativeProposition`

```json
{
  "proposition_id": "P-...",
  "canonical_statement": "不可再拆的叙事命题",
  "narrative_domain": "source_canon | adapted_story",
  "entity_ids": ["entity_id"],
  "direct_source_evidence_ids": ["SE-..."],
  "domain_truth_status": "true | false | undetermined | not_applicable"
}
```

原先分散表达的来源事实、信息交付和观众命题统一使用 `NarrativeProposition`：

- `SourceEvidence` 只能直接证明 `source_canon` 命题；
- 内容未变的保留也要通过 `AdaptationDecision` 声明；
- 内容实质改变时，`adapted_story` 命题必须使用新 ID，不能继承原文的直接证据；
- `NarrativeEvent` 改变命题或状态；
- `CharacterBeliefSnapshot` 表示角色如何看待命题；
- `AudienceStateSnapshot` 表示观众如何看待命题；
- `ShotContribution` 表示镜头提供了哪些可感知证据。

### 5.3 改编决策 `AdaptationDecision`

```json
{
  "adaptation_decision_id": "AD-...",
  "source_proposition_ids": ["P-source-..."],
  "adapted_proposition_ids": ["P-adapted-..."],
  "relation": "preserve | condense | split | combine | transform | omit | invent | other",
  "custom_relation": null,
  "creative_reason": "改编理由",
  "protected_causal_effect_ids": ["E-... | P-..."],
  "affected_event_ids": ["E-..."],
  "uncertainty": null
}
```

`relation` 描述命题之间的结构关系，不按题材或剧情内容分类；未覆盖的关系使用 `other + custom_relation`，不能因为不在枚举中而丢失。AI 负责判断语义关系并给出理由；结构校验只保证两个叙事域不混写、引用存在且受保护的因果结果没有被无因破坏。

### 5.4 通用状态事实 `StateFact`

```json
{
  "fact_id": "F-...",
  "proposition_id": "P-...",
  "subject_id": "entity_id",
  "predicate_id": "project_canonical_predicate_id",
  "value": {
    "kind": "entity_ref | scalar | text | spatial | boolean",
    "data": "..."
  },
  "time_scope": "timeline_id@logical_time",
  "visibility": "visible | offscreen | unknown",
  "provenance": "source | screenplay | storyboard",
  "confidence": 0.0
}
```

`StateFact` 是某个 `NarrativeProposition` 在特定时间域中的状态实例，不是第二套事实真值。`predicate_id` 由项目内语义归一和等价聚类形成，不要求命中全局固定词表；核心校验只比较同一命题及谓词在时间域内的增删、值和来源。

### 5.5 叙事证据 `NarrativeEvidence`

```json
{
  "evidence_id": "EV-...",
  "anchor": {"type": "event | beat | scene | shot", "id": "..."},
  "observable_claim": "角色或观众实际能够感知的内容",
  "perceivable_by": ["character_id | audience"],
  "supports_proposition_ids": ["P-..."],
  "planned_salience": 0.0,
  "planned_duration_s": null,
  "competing_attention_ids": []
}
```

证据可以在剧本阶段锚定到事件、节拍或场景，在分镜阶段进一步锚定到镜头。角色信念和观众信念只能引用其感知范围内的证据；`ShotContribution` 只引用已定义证据，不在镜头内部另造一套 `EV-*`。

### 5.6 戏剧问题 `DramaticQuestion`

```json
{
  "dramatic_question_id": "DQ-...",
  "question_text": "观众当前应追问的问题",
  "target_proposition_ids": ["P-..."],
  "open_anchor": {"type": "event | scene | sequence", "id": "..."},
  "intended_resolution_scope_id": "...",
  "desired_state_while_open": "unknown | suspected | contested",
  "resolution_anchor": null,
  "status": "open | resolved | carried"
}
```

戏剧问题引用一个或多个命题，但它本身不是事实命题。场景问题、尾钩和整集核心问题统一引用 `DQ-*`，避免把问句伪装成 `P-*` 真值。

### 5.7 叙事事件 `NarrativeEvent`

```json
{
  "event_id": "E-...",
  "proposition_ids": ["P-..."],
  "causal_parent_ids": ["E-..."],
  "precondition_fact_ids": ["F-..."],
  "action_ids": ["A-..."],
  "effects_add": ["F-..."],
  "effects_remove": ["F-..."],
  "character_goal_effects": [],
  "downstream_dependency_event_ids": [],
  "salience": 0.0,
  "irreversibility": 0.0,
  "must_keep": true,
  "delivery_scope_id": "episode_or_sequence_id",
  "delivery_policy": "deliver | withhold | carry",
  "primary_delivery_window_id": null
}
```

事件必须描述可验证的状态或认知变化，不能只是原文摘要。`must_keep` 表示改编结构不能无因丢失该事件；`delivery_policy` 才决定本作用域是否向观众交付，二者不得互相替代。

### 5.8 角色戏剧状态 `CharacterDramaticState`

```json
{
  "character_state_id": "CDS-...",
  "character_id": "entity_id",
  "anchor": {"type": "event | beat | scene | shot", "id": "..."},
  "goal_proposition_ids": ["P-..."],
  "stakes_proposition_ids": ["P-..."],
  "relationship_state": {},
  "emotion": {
    "label": "自由文本",
    "intensity": 0.0,
    "observable_evidence": []
  },
  "pressure": 0.0,
  "tactic": "自由文本"
}
```

角色目标在各叙事锚点上的权威状态只保存在这里；其他合同只能引用目标命题，角色信念和观众状态只能表达对该目标的理解差异，不能另写一份目标真值。

### 5.9 角色信念 `CharacterBeliefSnapshot`

```json
{
  "character_belief_id": "CB-...",
  "character_id": "entity_id",
  "anchor": {"type": "event | beat | scene | shot", "id": "..."},
  "perceived_evidence_ids": ["EV-..."],
  "beliefs": [
    {
      "proposition_id": "P-...",
      "stance": "believed | suspected | rejected | unknown",
      "confidence": 0.0
    }
  ],
  "misbelief_proposition_ids": [],
  "decision_proposition_ids": [],
  "decision_basis_ids": ["P-... | EV-..."]
}
```

### 5.10 原子动作 `AtomicAction`

```json
{
  "action_id": "A-...",
  "actor_ids": ["entity_id"],
  "target_ids": ["entity_id"],
  "semantic_intent": "动作命题",
  "precondition_fact_ids": ["F-..."],
  "effects_add": ["F-..."],
  "effects_remove": ["F-..."],
  "completion_condition": "可观察完成条件",
  "temporal_phases": [
    {
      "phase_id": "A-.../P1",
      "start_condition": "...",
      "end_condition": "...",
      "estimated_min_s": 0.0
    }
  ],
  "splittable_boundaries": []
}
```

### 5.11 观众状态 `AudienceStateSnapshot`

```json
{
  "audience_state_id": "AS-...",
  "audience_prior_id": "AP-...",
  "anchor": {"type": "event | beat | scene | shot", "id": "..."},
  "beliefs": [
    {
      "proposition_id": "P-...",
      "stance": "believed | suspected | rejected | unknown",
      "confidence": 0.0,
      "evidence_ids": ["EV-..."]
    }
  ],
  "causal_hypotheses": [],
  "character_goal_hypotheses": {},
  "spatial_model": {
    "location_id": null,
    "landmarks": {},
    "entrances_exits": {},
    "character_positions": {},
    "movement_paths": {},
    "orientation_confidence": 0.0
  },
  "temporal_model": {
    "timeline_id": "...",
    "relative_time": "...",
    "jump_basis_proposition_ids": [],
    "orientation_confidence": 0.0
  },
  "active_question_ids": ["DQ-..."],
  "working_memory": [
    {
      "proposition_id": "P-...",
      "retention_confidence": 0.0
    }
  ],
  "attention_residue_ids": [],
  "affective_state": {
    "tension": 0.0,
    "empathy_targets": [],
    "expected_emotion": "自由文本"
  }
}
```

每个观众状态只属于一个 `AudiencePriorContract`，从而允许不同先验拥有不同起始信念和处理路径。观众状态可以在剧本阶段锚定到事件或场景，在分镜阶段锚定到镜头，不能强制依赖尚未生成的 `shot_id`。观众状态只记录观众能形成的状态；“故意不告诉观众什么”属于导演意图，只保存在 `ExperienceIntent`，不能泄漏到盲审输入。

### 5.12 目标观众先验 `AudiencePriorContract`

```json
{
  "audience_prior_id": "AP-...",
  "scope_id": "project_or_episode_id",
  "audience_description": "目标观众的一次观看前提",
  "assumed_known_proposition_ids": [],
  "assumed_unknown_proposition_ids": [],
  "familiarity_assumptions": [
    {"dimension": "自由语义维度", "level": 0.0, "reason": "..."}
  ],
  "language_and_context_assumptions": [],
  "attention_memory_assumptions": {},
  "calibration_source": "human_panel | research | needs_review"
}
```

“大众”不能是未定义的平均人。关键意图至少使用多个具有不同知识、题材熟悉度和记忆假设的先验合同；这些维度由当前项目和真人校准产生，不维护固定人群或题材白名单。

### 5.13 观众体验意图 `ExperienceIntent`

```json
{
  "experience_intent_id": "XI-...",
  "scope_id": "scene_or_sequence_or_episode_id",
  "anchor_event_ids": ["E-..."],
  "director_objective": "这一段希望观众经历什么",
  "attention_target_ids": ["entity_id | P-..."],
  "audience_paths": [
    {
      "audience_path_id": "XP-...",
      "audience_prior_id": "AP-...",
      "audience_state_in_id": "AS-...",
      "audience_state_out_target_id": "AS-...",
      "target_deltas": [
        {
          "target_delta_id": "XD-...",
          "dimension": "belief | character_goal | spatial_temporal | affective | question | attention | other",
          "proposition_ids": ["P-..."],
          "description": "该先验观众需要发生的状态变化",
          "from_state": {},
          "to_state": {},
          "target_confidence": null,
          "required_processing_s": 0.0,
          "deadline_event_id": "E-...",
          "primary_delivery_window_id": null,
          "custom_dimension": null
        }
      ]
    }
  ],
  "withheld_propositions": [
    {
      "proposition_id": "P-...",
      "reason": "导演意图",
      "future_disclosure_anchor": null,
      "carried_question_id": "DQ-..."
    }
  ],
  "forbidden_misconceptions": []
}
```

`ExperienceIntent` 只回答“观众状态应如何变化”，不规定用什么镜头完成。共享的导演目标通过多条 `audience_paths` 落到不同观众先验；每条路径拥有自己的入场状态、目标出场状态、所需变化、处理时间和截止点。`target_deltas` 必须等于该路径入/出状态的结构差，不得另写一套目标真值。同一命题可先从未知变为怀疑，再从怀疑变为确认；每次信念转移分别拥有主要交付窗口，因此不与验证性重复冲突。`dimension=other` 保证未预设的观看意图仍能表达。`withheld_propositions` 必须记录隐藏理由，以及未来揭示锚点或整集合同中的继续保留问题。

### 5.14 认知吸收任务 `AssimilationTask`

当目标观众状态无法从现有证据推出时才创建：

```json
{
  "assimilation_task_id": "AT-...",
  "experience_intent_id": "XI-...",
  "audience_path_id": "XP-...",
  "target_delta_id": "XD-...",
  "required_prior_proposition_ids": [],
  "downstream_dependency_event_ids": ["E-..."],
  "satisfaction_criteria": "可从盲审观察验证的达成条件",
  "status": "open | planned | satisfied | needs_review"
}
```

`AssimilationTask` 只引用某条观众路径中尚未弥合的目标变化，不重复保存目标内容、截止点或具体镜头方案。

### 5.15 镜头证据贡献 `ShotContribution`

```json
{
  "shot_contribution_id": "SCN-...",
  "experience_intent_ids": ["XI-..."],
  "target_delta_ids": ["XD-..."],
  "assimilation_task_ids": ["AT-..."],
  "evidence_ids": ["EV-..."],
  "story_delta_fact_ids": ["F-..."],
  "character_state_delta_ids": ["CDS-... | CB-..."],
  "audience_state_delta_ids": ["AS-..."],
  "affective_delta": {},
  "spatial_temporal_delta": {},
  "dramatic_pressure_delta": 0.0
}
```

目标需求保存在 `ExperienceIntent`，待解决缺口保存在 `AssimilationTask`，实际镜头证据和二者的镜头级绑定只保存在 `ShotContribution`。`ShotTask` 通过唯一的 `shot_contribution_id` 引用它，避免双向所有权和字段漂移。

### 5.16 独立可读窗口 `ReadabilityWindow`

```json
{
  "readability_window_id": "RW-...",
  "event_ids": ["E-..."],
  "proposition_ids": ["P-..."],
  "target_delta_ids": ["XD-..."],
  "shot_ids": ["SH-..."],
  "attention_target_ids": [],
  "evidence_ids": ["EV-..."],
  "scheduled_processing_s": 0.0,
  "planned_available_s": 0.0,
  "competing_attention_ids": [],
  "readability_reason": "...",
  "status": "planned | satisfied | needs_replan"
}
```

可读窗口可以位于一个镜头内部，也可以跨多个相邻镜头；它表达“观众有独立注意和处理机会”，不等同于强制切镜。`target_delta.required_processing_s` 是某条观众路径的目标需求，`ReadabilityWindow.scheduled_processing_s` 是方案分配，`planned_available_s` 是扣除注意竞争后实际可用的规划时间；验收按各先验路径分别计算，再取低分位结果。

### 5.17 铺垫—兑现合同 `SetupPayoffContract`

```json
{
  "setup_payoff_id": "SP-...",
  "setup_proposition_ids": ["P-..."],
  "setup_event_ids": ["E-..."],
  "payoff_event_ids": ["E-..."],
  "intended_inference_ids": ["P-..."],
  "retention_deadline_event_id": "E-...",
  "minimum_retention_confidence": 0.0,
  "recall_needed": null,
  "status": "open | preserved | paid_off | intentionally_carried"
}
```

是否需要唤回根据观众工作记忆、间隔和中间信息负荷推导，不按某类物件或设定写规则。

### 5.18 场景戏剧合同 `SceneDramaticContract`

```json
{
  "scene_id": "SC-...",
  "applicability": "applies | not_applicable",
  "not_applicable_reason": null,
  "alternative_dramatic_function": null,
  "scene_question_id": "DQ-...",
  "point_of_view_character_id": null,
  "audience_state_paths": [
    {
      "audience_prior_id": "AP-...",
      "audience_state_in_id": "AS-...",
      "audience_state_out_target_id": "AS-..."
    }
  ],
  "character_state_in_ids": ["CDS-..."],
  "goal_proposition_ids": ["P-..."],
  "obstacle_proposition_ids": ["P-..."],
  "stakes_proposition_ids": ["P-..."],
  "pressure_curve": [
    {"anchor": {"type": "event | beat", "id": "..."}, "value": 0.0}
  ],
  "turn_event_ids": ["E-..."],
  "value_polarity_in": "自由文本",
  "value_polarity_out": "自由文本",
  "relationship_deltas": [],
  "character_state_out_ids": ["CDS-..."],
  "scene_button": "场景结束时留下的动作、决定、问题或冲击"
}
```

场景的时空目标不另存一份空合同：每个观众先验的入口状态和目标出口状态引用的 `AudienceStateSnapshot` 是唯一权威；中间变化必须由 `ShotContribution.spatial_temporal_delta` 提供可感知证据。

目标、阻力、stakes、转折等是审计维度，不是所有段落都必须套用的情节模板。氛围、蒙太奇、抒情或观察性段落可以标记 `not_applicable`，但必须说明理由、替代戏剧功能以及它对观众状态或整集节奏的贡献。

### 5.19 段落与整集戏剧合同 `NarrativeArcContract`

```json
{
  "arc_id": "ARC-...",
  "scope": "sequence | episode",
  "applicability": "applies | not_applicable",
  "not_applicable_reason": null,
  "alternative_dramatic_function": null,
  "core_question_ids": ["DQ-..."],
  "promise_proposition_ids": ["P-..."],
  "escalation_event_ids": ["E-..."],
  "climax_event_ids": ["E-..."],
  "payoff_contract_ids": ["SP-..."],
  "pressure_curve": [
    {"anchor": {"type": "event | beat", "id": "..."}, "value": 0.0}
  ],
  "information_density_curve": [
    {"anchor": {"type": "event | beat", "id": "..."}, "value": 0.0}
  ],
  "processing_beats": [
    {"anchor": {"type": "event | beat", "id": "..."}, "purpose": "消化、停顿或转向"}
  ],
  "ending_hook_question_ids": ["DQ-..."],
  "resolved_question_ids": ["DQ-..."],
  "carried_question_ids": ["DQ-..."]
}
```

该合同防止“每场都通顺，但整集没有升级、高潮和兑现”。不采用传统高潮结构的段落同样可以标记 `not_applicable` 并声明替代功能，不能因题材或形式不同被强行改造成固定戏剧模板。

### 5.20 镜头任务 `ShotTask`

```json
{
  "shot_id": "SH-...",
  "scene_id": "SC-...",
  "sequence_index": 1,
  "event_ids": ["E-..."],
  "primary_action_id": null,
  "supporting_action_ids": [],
  "shot_contribution_id": "SCN-...",
  "planned_state_in": ["F-..."],
  "planned_delta_add": ["F-..."],
  "planned_delta_remove": ["F-..."],
  "planned_state_out": ["F-..."],
  "audience_state_paths": [
    {
      "audience_prior_id": "AP-...",
      "audience_state_in_id": "AS-...",
      "audience_state_out_target_id": "AS-..."
    }
  ],
  "completed_before_action_ids": ["A-..."],
  "reserved_future_event_ids": ["E-..."],
  "duration_s": 0,
  "shot_size": "...",
  "camera_intent": "...",
  "visual_action": "...",
  "dialogue_narration_sound": []
}
```

`primary_action_id` 允许为空，但 `shot_contribution_id` 不得为空。每个目标观众先验在镜头中拥有独立的状态入/出路径；观众意图和认知任务经 `ShotContribution` 绑定到镜头，不在 `ShotTask` 重复保存。

### 5.21 叙事边界合同 `NarrativeBoundaryContract`

```json
{
  "boundary_id": "B-SH1-SH2",
  "previous_shot_id": "SH1",
  "next_shot_id": "SH2",
  "narrative_relation": "自由语义 + 稳定关系结构",
  "required_state_invariants": ["F-..."],
  "allowed_state_deltas": ["F-..."],
  "forbidden_replay_action_ids": ["A-..."],
  "handoff_action_phase_id": null,
  "spatial_orientation_contract": {},
  "temporal_orientation_contract": {},
  "audience_state_handoffs": [
    {
      "audience_prior_id": "AP-...",
      "previous_state_out_id": "AS-...",
      "next_state_in_id": "AS-..."
    }
  ],
  "affective_handoff": {},
  "cut_motivation": "为什么此时应切换注意"
}
```

### 5.22 认知桥接方案 `CognitiveBridgePlan`

```json
{
  "bridge_plan_id": "BP-...",
  "assimilation_task_ids": ["AT-..."],
  "candidate_changes": [],
  "expected_audience_delta": {},
  "affected_shot_ids": [],
  "added_shot_ids": [],
  "removed_shot_ids": [],
  "estimated_screen_time_delta": 0.0,
  "deletion_test_result": {},
  "marginal_gain_result": {},
  "selection_reason": "..."
}
```

### 5.23 冷观众观察 `BlindAudienceObservation`

```json
{
  "observation_id": "BAO-...",
  "audience_prior_id": "AP-...",
  "anchor": {"type": "scene | sequence | shot", "id": "..."},
  "spontaneous_recall": {
    "recognized_entities": [],
    "inferred_propositions": [],
    "causal_hypotheses": [],
    "character_goal_hypotheses": [],
    "active_question_ids": ["DQ-..."]
  },
  "neutral_followup_observations": [],
  "noticed_attention_target_ids": [],
  "spatial_temporal_model": {},
  "felt_affective_state": {},
  "perceived_relationship_deltas": [],
  "perceived_stakes": [],
  "experienced_pressure_curve": [
    {"anchor": {"type": "scene | shot", "id": "..."}, "value": 0.0}
  ],
  "experienced_rhythm": {
    "momentum": 0.0,
    "processing_sufficiency": 0.0,
    "drag_or_rush_observations": []
  },
  "next_event_expectations": [],
  "uncertainties": [],
  "supporting_evidence_ids": ["EV-..."],
  "confidence": 0.0
}
```

首轮自由复述和自由因果推断必须先完成并冻结，之后才允许无目标暗示的中性追问。被追问后才出现的答案不得计入首轮理解率。

### 5.24 叙事审读报告 `NarrativeReviewReport`

```json
{
  "narrative_review_report_id": "NRR-...",
  "scope_id": "scene_or_sequence_or_episode_id",
  "experience_intent_ids": ["XI-..."],
  "observation_ids": ["BAO-..."],
  "target_delta_results": [
    {
      "audience_prior_id": "AP-...",
      "target_delta_id": "XD-...",
      "result": "satisfied | missed | contradicted | needs_review"
    }
  ],
  "character_goal_readability_result": {},
  "attention_alignment_result": {},
  "spatial_temporal_orientation_result": {},
  "affective_alignment_result": {},
  "relationship_change_result": {},
  "stakes_readability_result": {},
  "pressure_rhythm_result": {},
  "next_expectation_result": {},
  "intentional_ambiguity_result": {},
  "low_percentile_result": {},
  "inference_variance": 0.0,
  "evidence_gap_ids": ["AT-..."],
  "unintended_inference_ids": [],
  "decision": "pass | revise | needs_human_review",
  "reason": "..."
}
```

审读报告比较的不只是真假命题，还包括情绪、关系、stakes、压力曲线、节奏处理和下一步预期，使场景与整集戏剧合同也能进入盲审闭环。

---

## 6. 全链路叙事流程

### 6.1 原文事实与命题归一

1. 从原文抽取 `SourceEvidence`；
2. 在 `source_canon` 叙事域中将同义事实归一为来源命题；
3. 为采用、压缩、拆分、合并、改写、省略或新增的内容创建 `AdaptationDecision`；
4. 在 `adapted_story` 叙事域创建改编命题，内容改变时必须使用新 ID，不能让原文证据直接证明改写事实；
5. 保留原文定位、改编理由和受影响的因果关系；
6. 建立实体引用，不通过普通称谓猜造新角色；
7. 语义不确定时标记 `needs_review`，不通过关键词规则强行归类。

### 6.2 事件图与剧本

剧本阶段按顺序产出：

1. 由改编命题和时域状态实例组成的故事真值图；
2. 事件因果图；
3. 剧本阶段可感知的叙事证据；
4. 角色戏剧状态和角色信念图；
5. 原子动作及状态效果；
6. 场景戏剧合同；
7. 段落/整集戏剧合同；
8. 目标观众先验、观众体验意图和关键认知截止点；
9. 面向创作者阅读的完整剧本。

剧本校验必须检查：

- 当前作用域必交付事件和目标信念转移覆盖；
- 原文命题与改编命题之间存在合法改编决策；
- 因果父子顺序；
- 人物决策依据；
- 适用场景的目标、阻力、stakes、转折与离场新局面，或不适用时的替代戏剧功能；
- 适用段落的升级、高潮、兑现和尾钩，或不适用时的替代结构功能；
- 有意隐藏的信息与开放问题；
- 未绑定事件的新剧情；
- 压缩或改编是否保留主因果链。

存在未解决的本作用域必交付事件、因果断裂、人物动机断裂或未按合同处理的整集承诺时，剧本不得发布给分镜规划。

### 6.3 AI 观众意图导演

AI 导演同时读取故事真值图、角色信念图、当前观众状态、目标观众先验和戏剧合同，输出：

1. 本段首先希望观众注意什么；
2. 看完后应相信、怀疑、拒绝或仍不知道什么；
3. 应理解哪个人物目标、关系或判断变化；
4. 应形成或关闭哪些问题；
5. 应感受到怎样的压力和情绪变化；
6. 哪些事实必须故意保留；
7. 这些变化最迟在哪个后续事件前完成。

导演目标可以共享，但系统必须为每个 `AudiencePriorContract` 生成独立的入场状态、目标出场状态和目标变化路径。认知缺口、处理时间和盲审结果逐路径计算，不能先平均再规划。

“新事物注入”通过图差分识别：凡是任一目标先验的 `AudienceBeliefGraph[prior]` 尚不能推出、但即将成为后续事件前置的命题，均成为潜在认知任务。风险综合：

- 与既有认知的距离；
- 推断路径长度；
- 与观众既有预期的冲突；
- 下游依赖程度；
- 同镜注意竞争；
- 证据显著性和可读时间；
- 铺垫到使用之间的记忆衰减；
- 当前意图要求理解、怀疑还是未知。

这些是开放语义特征，不是内容类别。

### 6.4 分镜是多层约束图划分

规划器把事件图划分为镜头时必须同时满足：

- 合法事件拓扑；
- 主要交付所有权；
- 角色动机链；
- 独立可读窗口；
- 动作、对白、文字、注意和推断容量；
- 状态链和叙事边界；
- 场景空间、时间和视点；
- 情绪压力和段落节奏；
- 铺垫—兑现和观众记忆；
- 每镜非空功能贡献。

禁止先按字数或标点切段，再为切出的片段补状态。

### 6.5 单镜容量

规划时长由以下需求共同决定：

```text
required_duration
= action_phase_readability
+ spoken_and_text_readability
+ attention_switch_time
+ inference_processing_time
+ reaction_or_emotional_registration_time
+ spatial_reorientation_time
+ entry_and_exit_settle_time
```

时长估计来自结构和跨题材盲审校准。若超载：

1. 先减少同时发生的无关任务；
2. 调整信息交付顺序；
3. 在动作阶段或认知阶段的自然边界拆分；
4. 不可拆时标记人工决策；
5. 不通过扩充动作词表解决。

### 6.6 关键转折采用“独立可读窗口”

以下关系会提高独立可读需求：

- 事件成为多个后续事件的共同前置；
- 角色目标或选择发生改变；
- 新因果规则进入观众理解；
- 状态变化不可逆或代价高；
- 信息与其他动作同时发生会无法被注意；
- 人物或观众需要反应时间才能理解意义。

系统输出 `readability_window_reason` 和所需时间。一个完整长镜头可以同时完成揭示和反应；只有容量、注意或构图无法容纳时才增加物理镜头。

### 6.7 最小充分的认知桥接

当目标观众状态无法从现有证据推出时，AI 至少提出多个候选：

- 重构现镜的调度、构图和注意焦点；
- 减少竞争动作、对白或信息；
- 增加证据可读时间；
- 提前埋设自然铺垫；
- 在使用前唤回已衰减的信息；
- 增加只承担认知、情绪或空间贡献的支持镜；
- 用结果、人物接收、对照或后续验证补足因果。

以下过程只是可能需要的认知功能，不是固定镜头模板：

```text
建立上下文
→ 引导注意
→ 提供证据
→ 留出处理时间
→ 展示后果
→ 必要时验证或唤回
```

候选必须通过：

- **缺口测试**：当前证据确实不能可靠推出目标状态；
- **删除测试**：删除该镜后理解、情绪、空间或问题状态显著下降；
- **边际增益测试**：理解增益大于新增时长与节奏代价；
- **最小充分测试**：优先改现镜，容量不足才增镜。

### 6.8 大众观看的整场审计

每个场景和段落都必须经过这些维度的审计；不适用的维度必须说明替代功能，而不是强行套用：

- 人物感知、判断、目标、选择和行动是否可连接；
- 场景问题、阻力、stakes、价值极性和关系变化是否成立；
- 观众是否清楚地点、时间、相对位置、入口出口和行动方向；
- 视点和戏剧反讽是否按意图分配信息；
- 人物情绪强度、关系压力和表演节奏是否连续；
- 铺垫在兑现前是否仍被观众记得；
- 有意留白是否形成正确问题，而非无方向困惑；
- 主要注意目标是否被其他内容遮蔽；
- 高负荷节拍后是否有消化空间；
- 每次切镜是否改变注意、信息、情绪、视点、空间或时间理解；
- 适用的段落和整集是否持续升级并完成承诺；非传统结构是否完成其声明的替代功能。

### 6.9 动作重复与功能性重复

检测分两层：

1. 同一 `action_id` 被多个镜头主要拥有且无重复意图，直接失败；
2. 不同 ID 的动作若前置、主体、目标、效果和完成条件高度等价，进入语义审计。

允许重复必须绑定新增贡献，例如：

- 从猜测推进到确认；
- 从个案推进到可复用规则；
- 改变人物目标或关系；
- 提供对照、升级或必要记忆唤回。

相似文字或相同动作本身既不能证明重复，也不能证明应保留。

### 6.10 冷观众叙事盲审

执行角色必须隔离：

1. **意图导演**读取三张图并产出目标观众状态；
2. **冷观众**只顺序阅读实际剧本或分镜，不读取原文答案、目标状态和未来内容；
3. **证据比较器**在冷观众完成后才比较目标与实际理解，并定位到具体分镜证据。

冷观众先完成不带任何目标暗示的自由复述、自由因果推断和下一步预期，形成并冻结 `BlindAudienceObservation.spontaneous_recall`；之后才允许提出不包含目标答案的中性追问，追问所得不得计入首轮理解率。

比较器随后生成 `NarrativeReviewReport`，逐项比较：信念转移、人物目标、时空定向、情绪体验、关系变化、stakes、压力与节奏、开放问题和下一步预期。结论必须定位到 `NarrativeEvidence`，不能只给总分。

关键任务使用多个 `AudiencePriorContract`，关注低分位和推断方差，不只看平均分。任何根据 `ExperienceIntent` 生成的问题都只能进入冻结后的中性追问阶段，不能反向提示首轮答案。

真人校准采用一次观看：受试者不能回看原文或答案。AI 阈值必须在跨题材样本上与真人理解结果保持稳定相关。

### 6.11 局部修复

诊断码描述问题，不绑定唯一修复：

```text
BEAT_UNASSIGNED
BEAT_UNDERDELIVERED
EVENT_ORDER_INVALID
ACTION_OWNERSHIP_CONFLICT
SHOT_CAPACITY_EXCEEDED
STATE_REGRESSION
AUDIENCE_ASSIMILATION_GAP
ATTENTION_COLLISION
CHARACTER_MOTIVATION_GAP
SPATIOTEMPORAL_ORIENTATION_GAP
INTENDED_AMBIGUITY_BROKEN
SETUP_PAYOFF_MEMORY_GAP
CUT_MOTIVATION_MISSING
OVEREXPLANATION_REDUNDANCY
SEMANTIC_GAP_OTHER
```

AI 可提出未预设的语义缺口。以下操作只是开放示例，不是封闭枚举；AI 可以提出新的结构操作，只要通过同一组叙事不变量和整集复验：

```text
rewrite_shot
split_shot
merge_shots
move_primary_delivery
insert_missing_event_shot
insert_supporting_cognitive_shot
remove_duplicate_or_redundant_shot
reorder_window
retarget_attention
redistribute_evidence
restore_or_defer_assimilation
```

#### 6.11.1 插镜条件

插镜有两种合法原因：

1. 当前作用域必交付事件确实没有主要交付窗口；
2. 事件已有交付，但观众认知、人物接收、空间定向或情绪处理存在缺口，且重构现镜无法解决。

任何插镜都必须：

- 位于合法因果区间；
- 不复制已经完成的动作；
- 拥有非空 `ShotContribution`；
- 通过状态、容量、认知和删除测试；
- 不能只是复制相邻镜再改标题。

#### 6.11.2 欠交付修复

已有主要镜头但表达不足时：

- 优先重写当前窗口；
- 过载时在动作或认知阶段边界拆分；
- 原镜结束状态改为真实中间状态；
- 新镜从该状态继续；
- 重新计算动作所有权、观众状态和铺垫—兑现关系。

#### 6.11.3 候选叙事方案与整集复验

任何局部修改先形成候选叙事方案：

1. 比较修改前后的事件、状态、角色、观众和镜头关系；
2. 验证当前窗口；
3. 重验整集引用、拓扑、状态、认知、场景、段落和兑现关系；
4. 评估受影响范围、新增时长与节奏代价；
5. 全部通过后才能替换正式方案；
6. 未通过的候选不得影响已通过的正式叙事方案。

### 6.12 分镜发布门禁

分镜只有满足以下条件才可标记 `narrative_ready`：

- 命题和引用完整；
- 事件拓扑合法；
- 当前作用域必交付事件、台词和目标信念转移覆盖；
- 角色动机链完整；
- 主要动作所有权唯一；
- 独立可读窗口满足；
- 单镜容量和镜头功能通过；
- 相邻状态、时空、视点和情绪承接通过；
- 所有认知任务在截止点前有证据；
- 冷观众没有稳定形成禁止的错误因果；
- 有意悬念没有被提前解释；
- 铺垫—兑现和整集承诺闭合或显式带入后续；
- 无功能镜头和无认知增量重复为零。

无法确定的项目进入 `needs_review`，不得以“当前最好”静默发布为叙事就绪。

---

## 7. 第 6 集整改示例

本节只说明通用架构如何处理事故，不得转成剧集专用逻辑。

### 7.1 当前结构问题

| 区段 | 肉眼问题 | 结构诊断 |
|---|---|---|
| 现镜 1–2 | 已到洞府后又补“逃离广场” | `EVENT_ORDER_INVALID` |
| 现镜 4–5 | 两镜都主要交付整夜修炼 | `ACTION_OWNERSHIP_CONFLICT` + `STATE_REGRESSION` |
| 灵泉出现 | 揭示、反应、决定和修炼挤在一镜 | `SHOT_CAPACITY_EXCEEDED` + `AUDIENCE_ASSIMILATION_GAP` |
| 铜镜触发 | 触发证据藏在抓鸡动作中 | `ATTENTION_COLLISION` + `BEAT_UNDERDELIVERED` |
| 鹿实验 | 触发、结果、倒地和反应过载 | `SHOT_CAPACITY_EXCEEDED` |
| 鹿实验后 | 鹿生死和位置回退 | `STATE_REGRESSION` |

### 7.2 叙事所有权

1. 若“逃离广场”是当前作用域必交付事件，必须位于到达洞府之前；只有来源改编策略允许省略、`AdaptationDecision` 记录理由且因果影响检查通过后，才能重新分类为不在本集交付，不能为了让当前分镜通过而直接删除，更不能事后补到错误位置。
2. 灵泉揭示拥有主要交付窗口；人物接收、理解价值和修炼决定必须可读。
3. 整夜修炼只由一个动作任务主要交付。
4. 铜镜与灵石的关联必须成为可感知证据，不能隐藏在无关动作中。
5. 山鸡结果把观众推进到“怀疑铜镜异常”；人物检查形成假设。
6. 鹿实验把人物和观众从“怀疑”推进到“确认”，属于合法验证性重复。

### 7.3 推荐事件拓扑

```text
离开前一地点（若必须）
→ 到达洞府
→ 检查随身物品
→ 进入洞府
→ 灵泉揭示
→ 孟浩接收发现并决定修炼
→ 整夜修炼
→ 醒来并离开
→ 明确铜镜与灵石的关联
→ 抓住山鸡
→ 异常证据引导注意
→ 山鸡结果与人物震惊
→ 检查铜镜并形成假设
→ 主动对鹿验证
→ 验证结果
→ 确认规则并形成结尾钩子
```

这是一条事件和认知拓扑，不是固定镜头数。实际镜头数量由容量、注意和独立可读窗口求解。

### 7.4 灵泉认知路径

目标不是“画面中出现灵泉”，而是让观众能够推出：

```text
内室存在异常
→ 异常来源是灵泉
→ 孟浩识别其价值
→ 修炼决定因此产生
```

空间建立、注意引导、揭示、人物接收和决定可以在足够容量的镜头内合并，但不能与整夜时间跳跃同时挤入一个短镜头。

### 7.5 铜镜认知路径

```text
观众看清铜镜与灵石的关联
→ 异常证据引导注意
→ 孟浩感知异常
→ 山鸡结果形成因果候选
→ 人物回看并形成假设
→ 主动对鹿验证
→ 第二结果提高规则置信度
→ 人物确认并形成新目标
```

这不是强制八镜模板。AI 必须证明现有镜头已完成哪些观众状态变化，只对无法完成的部分重构、延时或增镜。

---

## 8. 叙事产物与权责边界

本 PRD 的权威叙事产物只有：

```text
SourceEvidence
NarrativeProposition
AdaptationDecision
StoryTruthGraph
StateFact
NarrativeEvidence
DramaticQuestion
NarrativeEvent
AtomicAction
CharacterDramaticState
CharacterBeliefSnapshot
AudiencePriorContract
AudienceStateSnapshot
ExperienceIntent
AssimilationTask
ShotContribution
ReadabilityWindow
SetupPayoffContract
SceneDramaticContract
NarrativeArcContract
ShotTask
NarrativeBoundaryContract
CognitiveBridgePlan
BlindAudienceObservation
NarrativeReviewReport
```

权责关系如下：

- `SourceEvidence + source_canon NarrativeProposition` 只说明原文成立什么；
- `AdaptationDecision + adapted_story NarrativeProposition + StateFact + NarrativeEvent` 共同组成 `StoryTruthGraph`，后者是组合视图，不是另一套事实库；
- `CharacterDramaticState + CharacterBeliefSnapshot` 说明人物实际目标及其有限认知；
- `AudiencePriorContract + AudienceStateSnapshot + ExperienceIntent` 说明目标观众从何处出发、应发生什么体验变化；
- 只有存在证据缺口时才创建 `AssimilationTask`，由 `NarrativeEvidence + ShotContribution` 说明具体分镜如何补足；
- `BlindAudienceObservation` 只记录盲读所得，`NarrativeReviewReport` 只比较意图与观察，二者不得改写故事真值。

任何上游叙事关系变化后，均按 4.11 对受影响关系重新审读；工程落地方式不在本文展开。

---

## 9. 观测指标

每集至少输出：

```text
proposition_mapping_coverage_rate
event_coverage_rate
unbound_reference_count
event_order_violation_count
duplicate_primary_action_count
state_regression_count
character_motivation_gap_count
readability_window_violation_count
shot_capacity_violation_count
empty_shot_contribution_count
scene_contract_pass_rate
arc_contract_pass_rate
setup_payoff_closure_rate
experience_intent_coverage_rate
assimilation_deadline_pass_rate
cold_audience_target_belief_rate
cold_audience_false_causal_inference_rate
character_goal_readability_rate
spatial_temporal_orientation_rate
cold_audience_affective_alignment_rate
relationship_change_readability_rate
stakes_readability_rate
pressure_rhythm_alignment_rate
next_expectation_alignment_rate
intentional_ambiguity_fidelity_rate
premature_reveal_rate
attention_collision_rate
audience_processing_debt
cold_audience_inference_variance
cognitive_bridge_marginal_gain
ineffective_bridge_shot_rate
blind_ai_human_comprehension_correlation
```

指标锚点和口径统一如下：

- 引用、事件、动作和状态指标按整集汇总，并能下钻到事件或镜头；
- 信念、人物目标、时空、情绪、关系、stakes 和下一步预期按各观众路径中 `target_delta.deadline_event_id` 分别采样；
- 场景压力曲线与段落信息密度曲线的每个点必须锚定事件或节拍，不接受无锚点的主观数组；
- `audience_processing_debt` 为每条观众路径中各目标变化的处理需求减去其关联可读窗口有效可用时间后的正差之和；
- 低分位门禁按 `AudiencePriorContract` 逐路径计算后汇总，阈值只来自跨题材真人一次观看校准，不按剧情类别另设；
- 每次候选修改记录问题证据、候选方案、选择理由、前后关系差异、新增时长、整集复验和人工判断。

---

## 10. 叙事验收方案

### 10.1 结构与流程验收

必须覆盖：

1. 来源命题和内容不变的改编命题使用不同叙事域并由 `AdaptationDecision` 连接；
2. 实质改写后的命题不能让原文证据直接背书；
3. `StateFact` 必须引用命题且只能表达其时域实例；
4. 格式合法但不属于本集的引用失败；
5. 事件乱序被拓扑校验发现；
6. 角色未获得 `NarrativeEvidence` 却据此行动时失败；
7. 戏剧问题使用 `DQ-*`，不伪装成事实命题；
8. 同一动作被两镜主要拥有时失败；
9. 同一命题的“未知→怀疑”和“怀疑→确认”可以分别拥有交付窗口，同一观众路径中的同一次信念转移不能重复交付；
10. 无故事动作但具有空间、认知或情绪贡献的镜头合法；
11. `ShotContribution` 全空的镜头失败；
12. 未见动作表达仍能依靠前置、效果和阶段正确判定容量；
13. 欠交付修改原窗口，不机械复制相邻镜；
14. 支持性认知镜即使不拥有新事件也可在必要时合法插入；
15. 插镜位于合法因果区间并通过删除测试；
16. 关键转折可在一个足够长的镜头中获得独立可读窗口，不被强制拆镜；
17. 每个关键命题都有 `ExperienceIntent` 和截止锚点，只有证据不足时才创建 `AssimilationTask`；
18. 铺垫在兑现前遗忘时产生认知任务，记忆充分时不重复提醒；
19. 同一事实分别设为理解、怀疑和未知目标时得到不同规划；
20. 已经足够清楚的新命题不会因陌生而自动增镜；
21. 删除证据、缩短停留或增加注意竞争后能发现认知缺口；
22. 无认知增量的解释或确认镜通过删除测试被移除；
23. 第二次动作承担假设验证时允许，无状态增量时拦截；
24. 角色不知道、观众知道的戏剧反讽不会被误修；
25. 场景均通过但整集缺少约定的升级或兑现时，整集合同失败；
26. 非传统场景声明替代功能后不会被强制补齐传统结构字段；
27. `must_keep` 事件未经改编决策和因果影响检查不能为通过分镜而删除；
28. 上游叙事关系改变使依赖分镜和盲审结论重新接受验证；
29. 冷观众不获得原文、目标命题和未来内容；
30. 自由复述在任何目标相关追问前冻结，追问所得不计入首轮理解率；
31. 多个观众先验的低分位未达标时，平均高分不能通过；
32. 冷观众结论能定位到具体叙事证据，并覆盖信念、情绪、关系、stakes、节奏和下一步预期；
33. 每个 `AudienceStateSnapshot` 只绑定一个观众先验；
34. 同一体验意图为每个目标观众先验保存独立入场状态、目标出场状态和目标变化路径；
35. 冷观众观察只与同一观众先验的目标路径比较，不得以其他先验或平均状态代替；
36. 每条观众路径的 `target_deltas` 与其入/出状态结构差不一致时失败。

### 10.2 关系属性验收

自动生成随机事件图、角色信念、观众状态和镜头划分，验证：

- 任意合法规划保持拓扑序；
- 任意局部修改后仍满足全图不变量；
- 插入、删除、拆分、合并不会产生悬空引用；
- 同义改写保持命题所有权和认知结论；
- 共享少量字词的不同动作不会被误判为重复；
- 合法的有意未知不会被自动补全。

### 10.3 泛化变形验收

对同一用例执行：

- 同义改写和语序调整；
- 实体、地点和关系重命名；
- 题材替换；
- 把常见动作替换为虚构动作；
- 把一个动作拆为两阶段或重新合并；
- 把同一命题分别设为悬念、惊奇和直接理解；
- 遮挡证据、缩短可读时间或增加注意竞争；
- 删除铺垫、延长铺垫到兑现的间隔并增加中间负荷；
- 保留同一故事动作但改变其观众认知功能；
- 用此前不存在的虚构实体、规则和关系替换原内容。

除明确改变关系图和导演意图的操作外，系统结论必须保持等价。

### 10.4 反白名单验收

- 不得依赖固定动作词、剧情关键词或剧集 ID；
- 不得存在 `issue_code -> 唯一结构修复` 永久映射；
- 不得存在“内容类别 → 固定镜头模板”；
- 任一 `AudienceBeliefGraph[prior]` 不得由故事真值直接复制；
- `AssimilationTask` 不得永久映射到唯一镜头修复；
- 冷观众上下文不得包含目标答案或未来内容；
- AI 不确定时必须显式 `unknown/needs_review`；
- 诊断合同必须允许 AI 返回未预设的 `SEMANTIC_GAP_OTHER`。

### 10.5 黄金回归集

- 《我欲封天》第 6 集：事件乱序、重复修炼、灵泉揭示、铜镜触发和验证性重复；
- 既有第 2 集：人物动机、道具交付、文字信息和状态承接；
- 至少一集纯对白；
- 至少一集多场景追逐；
- 至少一集回忆或梦境；
- 至少一集依赖戏剧反讽；
- 至少一集长铺垫后兑现；
- 至少一集包含合法非动作支持镜；
- 至少一集全新题材和虚构动作。

---

## 11. 叙事能力建设顺序

### P0：先消除结构性叙事错误

1. 分离来源命题与改编命题，以 `AdaptationDecision` 保持可追溯关系；
2. 建立事件拓扑、状态单调和按信念转移划分的主要交付所有权；
3. 建立 `NarrativeEvidence`，让人物与观众认知只依赖可得证据；
4. 删除“主线缺失统一插镜”和固定动作词表主判据；
5. 建立正式 `CharacterBeliefSnapshot` 和人物决策链；
6. 经 `ShotContribution` 将观众体验、认知缺口和镜头证据连接到 `ShotTask`；
7. 允许合法非动作镜头，但要求非空功能贡献；
8. 建立候选叙事方案与整集复验；
9. 完成第 6 集黄金回归。

### P1：建立观众理解与导演闭环

1. 建立 `AudiencePriorContract`、`AudienceStateSnapshot` 和 AI 观众意图导演；
2. 建立独立可读窗口、认知容量和最小充分桥接；
3. 建立场景、段落和整集戏剧合同；
4. 建立铺垫—兑现与记忆置信度；
5. 建立相互隔离的意图导演、冷观众和证据比较器；
6. 加入多个观众先验、低分位门禁和有意留白校验；
7. 用跨题材样本验证泛化。

### P2：校准大众观看体验

1. 强化空间拓扑、时间定向、视点和戏剧反讽；
2. 强化人物情绪、关系压力、价值极性和段落节奏；
3. 用真人一次观看测试校准 AI 盲审；
4. 持续监控认知桥接边际增益、无效镜头和 AI/真人相关性；
5. 用新的题材、虚构动作和复杂留白持续做变形回归。

---

## 12. Definition of Done

- [ ] 本文档不包含视频生成、媒体输入、候选视频、后期合成或声音工程方案；
- [ ] 同一叙事域内命题身份统一，来源命题与改编命题分离并由 `AdaptationDecision` 连接；
- [ ] `StateFact` 只作为命题的时域实例，`NarrativeEvidence` 是角色与观众认知的唯一证据引用；
- [ ] 故事真值、角色信念和观众信念三张图分离；
- [ ] 角色不能依据未获得的信息行动；
- [ ] 事件图拓扑、状态单调和主要交付所有权成为强制验收；
- [ ] 每个被后续剧情依赖的关键命题具有体验意图和截止锚点，仅在存在证据缺口时创建认知任务；
- [ ] 同一命题的不同信念转移可以分别交付，同一观众路径中的同一次信念转移不能重复主要交付；
- [ ] `ExperienceIntent/AssimilationTask/ShotContribution` 职责不重叠；
- [ ] 观众意图、认知任务、证据贡献和状态目标经唯一 `ShotContribution` 绑定到 `ShotTask`；
- [ ] `primary_action_id` 可为空，但每镜必须有非空功能贡献；
- [ ] 关键转折使用独立可读窗口，不机械强制独立切镜；
- [ ] 新信息是否增镜由认知缺口、容量和最小充分测试决定；
- [ ] 人物动机、场景结构、时空、视点、情绪、节奏和铺垫—兑现均有整场校验；非传统段落可声明替代功能而不被强套模板；
- [ ] 功能性重复不会被误删，无观众增量重复不会通过；
- [ ] 支持性认知镜可以合法插入，但必须通过删除和边际增益测试；
- [ ] 所有局部修改先形成候选叙事方案，通过整集复验后才替换正式方案；
- [ ] 冷观众与意图导演隔离，不读取原文答案和未来内容；
- [ ] 冷观众先完成自由复述再接受中性追问，审读覆盖信念、情绪、关系、stakes、节奏和下一步预期；
- [ ] 每个观众先验拥有独立的入场状态、目标路径、镜头状态承接和盲审结果，低分位门禁不使用平均观众替代；
- [ ] “必保事件”不能因当前分镜未拍而删除，省略必须有改编决策和因果影响检查；
- [ ] AI 盲审经过跨题材真人一次观看校准；
- [ ] 同义改写、实体重命名、新题材、虚构动作和有意留白变形测试通过；
- [ ] 方案与实现验收确认没有剧情词白名单、镜号特判和问题码唯一修复映射；
- [ ] 第 6 集黄金回归通过。

完成 P0 后，系统不再从结构上制造乱序、重复和角色动机断裂；完成 P1 后，分镜规划开始以观众理解而不是“信息出现过”为准；完成 P2 后，AI 盲审与真人一次观看结果形成可校准的大众叙事质量闭环。
