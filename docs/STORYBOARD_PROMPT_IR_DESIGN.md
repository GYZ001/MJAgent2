# 分镜提示词架构决策：结构化 IR + 双供应商编译器（P1 设计输入）

日期：2026-08-24。用户提供 Seedance / MiniMax H3 两套提示词 skill（已归档
docs/prompt-skills/），问：分镜台生成提示词前先选视频模型，还是直接生成两套？

## 决策：都不是——单一 IR 真源，供应商编译器按需渲染

- 分镜台产出**供应商无关的结构化分镜 IR**；
- 每个供应商一个**确定性编译器**（Seedance / H3），派发时（生成台）选模型并编译；
- 需要"两套"时对同一 IR 跑两个编译器，成本为纯计算。

依据：
1. 两模型提示词哲学相反（中文散文暗示 vs 英文结构化字段），手维护两套 = 内容层
   E 类重复真源，必然漂移（P0 期间刚因版本常量重复真源挨过独立 Review 的 P0 判）。
2. skill 自带的 seedance-vs-h3.md 证明双向迁移是**机械七步**（拆头/镜头改写/翻译/
   运镜升维/音频拆分/约束移位/引用改编号）——机械变换是编译器职责，不是模型创作。
3. 决策④（HiAgent 主力、H3 版权兜底）要求切换零重做；IR+编译器把切换降为派发开关，
   且支持将来混合策略（私有 H3 跑草稿、Seedance 出终版）。

## IR 字段超集（每镜，冻结草案，P1 细化）

| 字段 | Seedance 编译 | H3 编译 |
|------|---------------|---------|
| duration_s + cut_intent | 括号软提示；节奏靠镜头数（3-4 镜/段） | `At MM:SS.mmm` 精确切点，严格递增 |
| camera{type, amplitude, speed} | 降维单个中文运镜词（一镜一词） | 三维英文句嵌进动作 |
| shot_size（六级景别） | 中文词 | 英文并入构图描述 |
| action（单一具体动词核心） | 成分顺序：运镜→主体→动作→场景→光影 | 沿时间线写进 integrated_multimodal_description |
| subjects[]（identity_id→定妆照） | `@角色名` | `Picture N` + 职责声明行 |
| dialogue[]{speaker, line} | 融进音频描述散文 | `(S1)` + `<d>[Chinese] 原话</d>` |
| on_screen_text | **能力缺失**：编译为"无字"+ 后期合成标记 | 双引号原文（官方能力） |
| soundscape / bgm | 合写"全片贯穿"一段 | 拆 overall_soundscape / non_diegetic_music 两字段 |
| style_anchor | 首句预告片质感暗示（多镜触发器） | `[Shot 1]` 开头风格词 |
| frame_chain_intent（首尾帧链） | **能力缺失**：降级为参考图+文字重锚 | I2VA/FL2VA/L2VA 固定指令行（逐字符，不许改写） |

**降级纪律**：单侧能力编译到不支持侧时按 skill 的反向迁移规则降级，且降级项必须
写入编译产物元数据（`degraded_capabilities[]`）——不做静默降级（家规 A 类）。

## 模型无关内容层（进 IR 生成 prompt，只维护一份）

情绪写成面部肌肉动作；关键道具锚定为构图约束；连续性元素逐镜重复；群像写死人数；
特效用物理描述不用文化词；收尾用格局镜；一次只改一处再重跑。
（源：novel-to-storyboard/references/failure-modes.md）

## QC

成片质检（切点、色温、响度、面部一致性）模型无关，直接收编
docs/prompt-skills/novel-to-storyboard/scripts/qc_video.py，接入生成台验收链。

## 与既有代码的衔接

- `app/video_prompt_profiles.py` 已是"每供应商一个 profile"的正确接缝，P1 在此扩展
  为完整编译器（现 SEEDANCE_2_PROFILE 的单镜头规则整段重写，见冻结方案 P1 节）。
- `app/compiler.py` 时长夹取（5-10s）放开到两家实测上限 15s。
- H3 编译器的固定指令行（模式信标）必须逐字符照抄 official-format.md——加结构性
  断言测试锁字面量，防止被"顺手改写"（C 类守卫）。
