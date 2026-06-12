# 提示词链规范（Prompt Spec）

> 对应 PRD §4.2~§4.4。每个 LLM 阶段 = 一个 prompt 模板 + 一个 Pydantic Schema + 一个业务规则校验器 + 修复回路。
> 本文件中的 prompt 是可直接使用的初稿；任何修改必须先跑金样回归（PRD §7）再合入。

## 0. 通用机制

### 0.1 修复回路（所有阶段共用）

```
draft = llm(prompt, temperature=0.7)
for attempt in range(max_repair_attempts):    # 默认 8，监制房可调；校验类失败一直让模型修
    json_obj = extract_json(draft)            # 剥代码围栏、截取首尾花括号
    errors = schema_validate(json_obj) + business_validate(json_obj)
    if not errors:
        return json_obj
    # 反复失败：升温跳出定式 + 加重措辞；错误信息带上违规原文（如超长台词原句）
    draft = llm(repair_prompt(json_obj, errors), temperature=escalating)
raise StageFailed(errors)                     # 失败要响，禁止兜底
```

> 关键原则：**校验类失败（Schema/业务规则不满足）一直让模型自己修**，直到通过或耗尽 max_repair_attempts；
> 只有 **模型不可用**（鉴权失败/参数 400/网关持续故障，即 ProviderError）才立即失败——重试同一 prompt 对这类错误无意义。

修复 prompt 模板：

```
你上一次的输出未通过校验。请修复以下具体问题后重新输出完整 JSON（不要解释，不要 Markdown）：
{逐条错误，含字段路径与期望值，例如：
- shots[3].duration_s=15 超出合法取值 {4,5,6,8,10,12}
- shots[7] 旁白+台词共 86 字，但时长 8s 最多允许 40 字，请精简文案或拆分镜头
- shots[9].characters 含"老者"，角色圣经中不存在，圣经角色为：林风/苏婉/赵天霸}
原输出：
{json}
```

> 关键：错误必须**具体到字段和数值**。1.0 失败教训之一是从不告诉模型哪里错了。

### 0.2 通用 system prompt 前缀

```
你是专业的竖屏漫剧（动态漫画短剧）编剧与分镜师。
输出规则：只输出一个 JSON 对象，无 Markdown 围栏，无解释文字。
所有内容使用简体中文。
```

## A. 角色圣经

**输入**：前 5 章原文（每章截断至 6000 字）+ 如有更多章节则附滚动摘要。
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

## B. 剧集规划

**输入**：全书章节摘要列表（每章 ≤200 字）+ 角色圣经。
**温度**：0.7

```
任务：将小说规划为竖屏漫剧剧集（每集 60~90 秒成片）。

漫剧节奏铁律：
1. 每集开头 3 秒必须是钩子：冲突爆发点/悬念/反转，绝不从平铺直叙开场。
2. 每集只讲一个核心事件，有一个情绪高点。
3. 每集结尾留下一集的悬念钩。
4. 节奏宁快勿慢：删除原著中的过渡性内容，跳跃叙事靠旁白补缝。

本次只规划前 {n} 集（默认 10）。每集标注其改编的源章节编号，
源章节必须连续覆盖、集与集之间不重叠、不跳章。

章节摘要：
{chapter_summaries}
角色圣经：
{bible_json}

输出 JSON Schema：
{
  "key_timeline": [str],          // 全书 10~20 条关键事件时间线（防伏笔丢失）
  "episodes": [
    {"episode_no": int, "title": str, "hook": str,      // hook=开头3秒画面+一句话
     "source_chapters": [int], "synopsis": str,          // 80~150字
     "cliffhanger": str, "target_duration_s": int}       // 60~90
  ]
}
```

**业务校验**：source_chapters 连续且不重叠；target_duration_s ∈ [60,90]；episode_no 连续递增。

## C. 单集分镜脚本（核心阶段）

**输入**：该集 source_chapters 原文全文 + synopsis/hook/cliffhanger + 角色圣经 + 上一集结尾摘要（衔接用）。
**温度**：0.7。**这是最长的输出，max_tokens 给满。**

```
任务：为漫剧第 {episode_no} 集《{title}》编写分镜脚本。

硬性约束（违反将被退回）：
1. 总时长 = 各镜头时长之和，必须在 {target}±10% 秒内。
2. 镜头时长只能取：{合法取值集合，M0验证后填入，如 4/5/6/8/10/12} 秒。
3. 语速预算：每镜头旁白字数+台词字数 ≤ 时长×5，≥ 时长×3。
   纯画面镜头（无旁白无台词）需在 action_desc 中体现完整叙事动作。
4. characters 只能使用角色圣经中的角色名。台词 speaker 必须在该镜头
   characters 中，或为"旁白"。
5. 每个镜头的 action_desc 只描述一个主要动作（一个主语+一个动词短语+
   情绪/神态），禁止"边A边B然后C"式复合动作。15~50 字。
6. 同一场景的镜头必须连续排列；scene_setting 同场景逐字相同，
   格式："时间，地点，氛围"（如"夜晚，废弃工厂内部，阴冷压抑"）。
7. 第 1 个镜头必须呈现本集 hook：{hook}
   最后 1 个镜头必须呈现悬念钩：{cliffhanger}

创作要求：
- 景别交替：连续 3 个镜头不得使用相同景别；情绪高点用特写。
- 台词从原著提炼改写为口语化短句（单句 ≤20 字），保留人物说话风格：
  {各角色 speech_style}
- 旁白负责时间跳跃与心理描写，台词负责冲突，不要用旁白复述画面内容。

镜头连贯铁律（成片是否连贯取决于此，与 app/stages.py 同步）：
- 同一场景内，除第一个镜头外，所有镜头 continuity_from_prev=true
  （生成时上一镜尾帧将作为本镜首帧输入 Seedance）。
- 相邻成链镜头动作严格承接：上一镜 action_desc 的结束状态 = 本镜起始状态。
- 每个场景首镜（链头）优先远景/全景交代环境；链头同时承担注入角色定妆照
  reference_image 的职责（first_frame 与 reference_image 网关互斥）。
- 场景切换 transition 用叠化/黑场，同场景内硬切。
- 角色不得凭空出现，中途登场须写明入场方式。

本集改编源文本：
{source_text}
本集概要：{synopsis}
角色圣经：{bible_json}
上一集结尾：{prev_ending}

输出 JSON Schema：
{
  "episode_no": int,
  "shots": [
    {"shot_no": int, "duration_s": int,
     "shot_size": "远景|全景|中景|近景|特写",
     "camera_move": "固定|推近|拉远|横摇|跟随",
     "scene_setting": str, "characters": [str],
     "action_desc": str, "narration": str|null,
     "dialogues": [{"speaker": str, "line": str,
                    "emotion": "平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定"}],
     "transition": "硬切|叠化|黑场",
     "continuity_from_prev": bool}
  ]
}
```

**业务校验器（代码实现，逐条对应修复反馈）**：

| # | 规则 | 错误消息模板 |
|---|---|---|
| V1 | Σduration ∈ target±10% | `总时长{x}s 超出 {lo}~{hi}s，请调整镜头时长或增删镜头` |
| V2 | duration ∈ 合法取值集合 | `shots[i].duration_s={x} 不在 {set}` |
| V3 | 字数/语速预算 | `shots[i] 文案{x}字 超出 {dur}×5={max}字上限` |
| V4 | 角色合法性 | `shots[i].characters 含"{x}"不在圣经中` |
| V5 | action_desc 单动作 | 动词短语 >2 个时：`shots[i].action_desc 含复合动作，请拆分镜头` |
| V6 | 场景连续性 | `scene_setting"{x}"在 shots[i] 与 shots[j] 间被打断` |
| V7 | shot_no 连续 / 景别不三连 | 同模板 |

> V5 的动词检测用 LLM 不可靠，代码侧用简单启发（顿号/"然后"/"接着"/"边…边"等连接词计数），宁可漏判不可误判。

## D. Prompt 编译（确定性代码，非 LLM——列在此处仅为完整性）

```python
def compile_prompt(shot, bible, style) -> str:
    parts = [
        style.visual_style_canonical,                      # 画风锚点，逐字
        f"{shot.shot_size}，{shot.camera_move}镜头",        # 镜头语言
        shot.scene_setting,                                 # 场景，同场景逐字复用
        *[bible[c].appearance_canonical for c in shot.characters],  # 角色锚点，逐字
        shot.action_desc,                                   # 动作
    ]
    text = "。".join(parts)
    text = enforce_length(text, LIMIT)    # 超长按 动作>场景>风格 之外的修饰语裁剪，
                                          # 角色锚点串永不裁剪
    return f"{text} --ratio 9:16 --dur {shot.duration_s}"  # 其余参数按 M0 验证结果追加
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

输出：{"character_match": float, "action_match": float, "clean_frame": float,
      "overall": float, "issues": [str]}    // issues 用一句话描述具体问题，
                                            // 将被拼入重生成 prompt 的负向区
```

**结果使用**：overall <0.6 且自动重试次数 <1 → 将 issues 翻译为负向描述追加重生成；否则进人工评审墙并展示分数与 issues。

## F. 金样回归（提示词变更的准入门槛）

- `golden/` 目录放 3 本固定测试小说节选（都市/玄幻/悬疑各 1，每本前 5 章）
- 回归脚本：对 3 本各跑 A→B→C 链，输出结构合法率、修复回路触发次数、V1~V7 违规分布、总耗时
- 任何 prompt 修改：先跑回归，三项指标（合法率不降、修复次数不升、耗时不升 20%+）通过才可合入
- 回归结果留档 `golden/runs/<date>.json`，趋势可查
