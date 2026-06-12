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
- shots[3].duration_s=6，但固定视频生成时长必须为 10s
- shots[7] 旁白+台词仅 18 字，低于 10s×4=40 字下限，请补充关键画面信息或台词
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
**注意**：`synopsis` 只用于前端展示和人工理解，不得作为分镜阶段的剧情依据。
**温度**：0.7

```
任务：将小说规划为竖屏漫剧剧集（每集 40~60 秒成片，常规集优先 50 秒）。

漫剧节奏铁律：
1. 每集开头 3 秒必须是钩子：冲突爆发点/悬念/反转，绝不从平铺直叙开场。
2. 每集只讲一个核心事件，有一个情绪高点。
3. 每集结尾留下一集的悬念钩。
4. 节奏宁快勿慢：删除原著中的过渡性内容，跳跃叙事靠旁白补缝。
5. 成本优先：不要把简单动作或场景交代拉长；一集宁可短而密，不要慢而水。

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
     "source_chapters": [int], "synopsis": str,          // 80~150字，仅前端展示
     "cliffhanger": str, "target_duration_s": int}       // 40/50/60，常规集优先 50
  ]
}
```

**业务校验**：source_chapters 连续且不重叠；target_duration_s ∈ {40,50,60}；episode_no 连续递增。

## C1. 节拍链（分镜的戏剧骨架，2026-06-12 新增）

> 动机：一段式分镜的产出是"把原文均匀切块塞满字数"——格式约束管得住密度，管不住戏剧结构。
> 紧凑的本质是**每 10 秒都有一次局势变化**，连贯的本质是**拍与拍构成因果链**。

**输入**：本集源文本 + hook/cliffhanger + 角色圣经。**输出**：正好 N 拍（N=集时长÷10，与固定 10s 视频段一一对应）。

每拍字段：`day_offset + time_of_day + location`（时间数值化，**代码校验单调递增→机制性禁闪回**；场景标签由代码渲染如"次日清晨，出租屋"）、`characters`（实际在场）、`event`（谁做了什么）、`turn`（局势变化/新信息）、`carry`（留给下一拍的钩子）、`beat_type`。

结构校验：第 1 拍=钩子、末拍=尾钩、中段 ≥1 反转/高潮、禁止连续两拍铺垫、因果链（i+1 拍由 i 拍 carry 触发，prompt 约束）。

## C2. 对拍展开（原 C 阶段）

第 i 镜实现第 i 拍。关键变化：
- `scene_setting` 必须逐字等于节拍表渲染标签（代码校验）→ 时间线与场景标签稳定
- `continuity_from_prev` / `transition` **由代码从场景标签推导覆盖**（同场景=接上镜+硬切，跨场景=叠化/黑场），不再依赖模型自觉，消除一整类返工
- 旁白纪律：≤45 字、只写画面拍不出的信息（时间跳跃/内心）、前史只允许第 1 镜一句——剧情信息压进台词和画面，治"小说朗读感"
- shot.characters ⊇ 该拍在场角色（代码校验）

## C. 单集分镜脚本（核心阶段）

**输入**：该集 source_chapters 原文全文 + hook/cliffhanger + 角色圣经 + 上一集结尾摘要（衔接用）。
**禁止**：分镜阶段不得使用 episode.synopsis；它只是前端展示字段，避免概要压缩导致细节丢失。
**温度**：0.7。**这是最长的输出，max_tokens 给满。**

```
任务：为漫剧第 {episode_no} 集《{title}》编写分镜脚本。

硬性输出规范（以下规则由代码校验，违反会被退回重写；请首轮直接满足）：
1. episode_no 必须等于 {episode_no}；shots 按剧情顺序排列，shot_no 必须从 1 开始连续递增，不能跳号、重复或乱序。
2. 总时长 = 所有 duration_s 之和，必须在 {target}±10% 秒内；target 只能取 40/50/60。
3. 每条 shot 是一个固定 10s 的视频段，本集 shot 数量必须正好 {target/10} 条。
4. duration_s 必须等于 10，这是最终 Seedance 视频生成参数 --dur 10。
5. 10s 内必须尽可能多塞入镜头和情节：每条 shot 的 action_desc 至少写 3 个连续小镜头/动作节点，建议 3~5 个，用"先……，随即……，转而……，最后……"写清顺序。
6. 每个 10s 视频段要像一段紧凑短片：允许快速切景、推近、特写、道具插入和角色反应连续发生；不要只写一个简单动作、凝视、走路、推门或氛围交代。
7. 每镜头文案字数 = narration 字数 + dialogues[*].line 字数。若有文案，只设信息密度下限：文案字数 ≥ 时长×4；不设上限，允许为了保留原文细节写得更充分。本集下限表：10s≥40字。这些字必须提供新信息，不能用空泛情绪词凑数。
8. 每镜头剧情载荷 = action_desc 字数 + narration 字数 + dialogues[*].line 字数，必须 ≥ 时长×12 字。10s 视频段至少要有 120 字有效剧情载荷，不能只有一句动作或一句旁白。
9. action_desc 至少 max(70, 时长×7) 字，不设上限；要写清"触发事件 + 连续动作 + 可见反应 + 新信息/后果"，让视频模型在 10s 内尽力实现多镜头推进。
10. 每个 10s 视频段至少推进三个具体信息点：例如"发现线索 + 角色反应 + 冲突升级"、"动作结果 + 新目标 + 悬念加深"。禁止单纯场景氛围、人物姿态、重复上一镜内容。
11. 禁止纯画面空镜；固定 10s 视频段必须有旁白或台词承载剧情信息，但这些文字不是字幕上限，视频模型只需把信息视觉化。
12. 角色名必须准确：characters 不能为空，只能使用角色圣经里的准确姓名：{角色名列表}。characters 只写本镜头画面中实际可见/实际在场的人物；幕后发消息者、纸条落款、屏幕昵称、AI 软件名不算出场角色，除非镜头真的拍到他本人。不要创造新名字，不要把姓名改成外号/称谓，不要用"无角色"。如果原文出现角色姓名，必须照抄原文和角色圣经中的姓名。
13. action_desc 必须显式写出本镜头主要角色的准确姓名，不能只写"他/她/男人/女人/镜头/纸张"；每个动作节点都优先围绕人物表情、动作、道具反应和剧情后果展开。
14. dialogues 只写人物实际开口台词，dialogues[*].speaker 必须在本镜头 characters 中；不要把纸条文字、屏幕文字、手机通知、内心独白或旁白写成 speaker="旁白"，这些内容放到 narration 或 action_desc。
15. 台词 line 不设字数上限；emotion 只能取：平静|愤怒|悲伤|惊恐|喜悦|讥讽|坚定。台词从原著提炼为口语化短句，但优先保留关键细节和人物说话风格：{各角色 speech_style}
16. scene_setting 只是连续性标签，不是渲染重点，最多 18 字，只写"时间，地点"；能不写氛围就不写，禁止堆砌薄雾、灯光、杂物、墙面、天气等环境描写。镜头主要渲染故事情节和人物。
17. shot_size 只能取：远景|全景|中景|近景|特写；camera_move 只能取：固定|推近|拉远|横摇|跟随；transition 只能取：硬切|叠化|黑场。
18. 同一 scene_setting 的镜头必须连续排列，不能被其他场景打断；同一场景的 scene_setting 必须逐字相同，格式建议："时间，地点"。
19. 连续 3 个镜头不得使用相同 shot_size；情绪高点优先用特写。
20. 相邻镜头必须有明确上下文接力：同场景连续镜头 continuity_from_prev=true，下一镜 action_desc 的开头必须承接上一镜结尾的动作、道具、屏幕内容或情绪；换时间/地点时 continuity_from_prev=false，且 narration 或 action_desc 必须写清转场原因/时间跳跃。
21. 第 1 个镜头必须呈现本集 hook：{hook}
    最后 1 个镜头必须呈现悬念钩：{cliffhanger}

首轮输出前必须逐镜预检（这些就是代码校验器的具体判定条件，不要等返工）：
1. 本集必须正好 {target/10} 条 shot，每条 duration_s=10，总时长={target}s；不要输出 5/6/7/8/9s，也不要多/少镜头。
2. 第 1 镜 continuity_from_prev 必须为 false；第 2 镜开始逐条和上一镜比较 scene_setting。
3. 如果本镜 scene_setting 与上一镜完全相同：
   - continuity_from_prev 必须为 true；
   - transition 必须为"硬切"；
   - characters 至少保留上一镜的 1 个核心人物；
   - action_desc 开头必须承接上一镜结尾的道具/屏幕内容/动作/情绪，不能重新介绍场景或重复上一镜发现。
4. 如果本镜 scene_setting 与上一镜不同：
   - continuity_from_prev 必须为 false；
   - transition 只能用"叠化"或"黑场"，绝不能用"硬切"；
   - narration 或 action_desc 必须写清承接原因、时间跳跃或线索带入，建议出现：次日、第二天、清晨、与此同时、随后、几小时后、带着 等承接词；
   - 如果只是同一段连续动作里从房间走到门口/楼道/桌边/窗前，不要改 scene_setting，继续沿用上一镜主场景标签，把移动写进 action_desc。
5. scene_setting 是稳定短标签，不是镜头内容：同一连续时空统一写同一个"时间，主地点"，例如"当日，出租屋"；不要在相邻镜头里改成"当日，出租屋楼道外/桌前/门口"导致断链。
6. characters 只写本镜头实际可见/在场的人；屏幕发信人、纸条落款、新闻里提到的人、AI 软件名不算 characters。它们只能写在 action_desc 或 narration。
7. 每条 action_desc 必须显式写出 characters 中的准确角色名，并至少包含 3 个连续动作/信息节拍；不要只写纸张、屏幕、镜头、场景自己在动。
8. 每条 shot 都必须有 narration 或真实人物台词；dialogues[*].speaker 必须是本镜 characters 里的角色名，不能写"旁白"。

常见错误 → 正确写法：
- 错：上一镜"当日，出租屋"，本镜"当日，出租屋楼道外"，transition="硬切"，又没有解释。对：若是王浩从房内走到门口，scene_setting 仍写"当日，出租屋"，continuity_from_prev=true，action_desc 写"王浩攥着上一镜的纸页走向门口……"。
- 错：纸条上出现"未署名作者"就把 characters 写成 ["未署名作者"]。对：如果画面只拍到王浩和纸条，characters 写 ["王浩"]，纸条文字放 action_desc/narration。
- 错：下一镜重新说"出租屋昏暗、桌上有电脑"。对：下一镜直接从上一镜结尾继续，写"王浩仍盯着刚弹出的新闻推送，先……随即……最后……"。

分镜前置步骤（只在脑内完成，不要输出到 JSON）：
1. 先按原文顺序列出本集 {target/10} 个连续剧情段，每段对应一个 10s shot；不得把同一事件拆成互相重复的摘要，也不得跳到原文后文再跳回来。
2. 为每个剧情段记录"上一镜尾状态 → 本镜起始状态 → 本镜结尾钩子"。写 action_desc 时直接体现这个接力，让用户能从镜01一路读到最后一镜，不需要猜中间发生了什么。
3. 建立专名锁定表：角色圣经姓名只能逐字使用 {角色名列表}；原文中的地名、书名、软件名、屏幕/纸条文字、人名必须逐字照抄，不要猜新名字、改字、换同音字或把普通称谓升级成新角色。
   注意：专名出现在纸条、屏幕、新闻或旁白里，不等于它就是本镜头 characters；characters 只放实际可见/在场的人。
4. 如果原文用"我/他/她"，必须结合角色圣经和上下文还原为准确角色名；还原不了就用动作主体的普通称谓，不要编姓名。

创作要求：
- 旁白负责时间跳跃与心理描写，台词负责冲突，不要用旁白复述画面内容。
- 场景描述能忽略就忽略：只保留最短时间地点标签；不要让薄雾、灯光、街道、杂物成为镜头主角。每个视频段的主角必须是人物、人物动作、人物反应和故事线索；场景只能服务于人物正在做什么、发现什么、失去什么、决定什么。
- 每个镜头输出前先自检：shot_no 连续、duration_s 全部为 10、characters 非空且姓名准确、action_desc 出现准确角色名、scene_setting 足够短、文案满足信息密度下限且不检查上限、
  剧情载荷足够、action_desc 至少 3 个连续小镜头/动作节点、台词 speaker 在本镜头 characters 中且不能是旁白、与上一镜有动作/道具/情绪/信息承接。

镜头连贯铁律（成片是否连贯取决于此，与 app/stages.py 同步）：
- 同一场景内，除第一个镜头外，所有镜头 continuity_from_prev=true
  （生成时上一镜尾帧将作为本镜首帧输入 Seedance）。
- 相邻成链镜头动作严格承接：上一镜 action_desc 的结束状态 = 本镜起始状态。
- 下一镜不要重新介绍同一场景，不要把上一镜已经完成的发现/动作重新讲一遍；必须推进到"因此发生了什么"。
- 如果必须跨时间或跨地点，transition 用"叠化"或"黑场"，continuity_from_prev=false，并在 narration 或 action_desc 写清"次日/几小时后/与此同时/他带着某线索来到某处"这类承接语。
- 每个场景首镜（链头）优先远景/全景交代环境；链头同时承担注入角色定妆照
  reference_image 的职责（first_frame 与 reference_image 网关互斥）。
- 场景切换 transition 用叠化/黑场，同场景内硬切。
- 角色不得凭空出现，中途登场须写明入场方式。

本集改编源文本：
{source_text}
分镜改编依据：只以以上原文全文、hook、悬念钩、角色圣经和上一集结尾为准；episode.synopsis 仅用于前端展示，禁止作为分镜剧情依据。
角色圣经：{bible_json}
上一集结尾：{prev_ending}

输出 JSON Schema：
{
  "episode_no": int,
  "shots": [
    {"shot_no": int, "duration_s": int,
     "shot_size": "远景|全景|中景|近景|特写",
     "camera_move": "固定|推近|拉远|横摇|跟随",
     "scene_setting": str, "characters": [str], // 画面中实际可见/在场且属于角色圣经的准确姓名
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
| V2 | duration 固定 10s | `shots[i].duration_s={x}，固定视频生成时长必须为 10s` |
| V3 | 字数/信息密度下限；禁止纯画面镜头；不设文字上限 | `shots[i] 旁白+台词仅{x}字，低于 {dur}×4={min}字下限` |
| V4 | 角色合法性；characters 非空；speaker 必须在本镜头 characters 中，旁白只能进 narration | `shots[i].characters 含"{x}"不在圣经中` |
| V5 | action_desc 至少 3 个连续动作/信息节拍；不设文字上限 | `shots[i].action_desc 只有{x}个动作/信息节拍` |
| V6 | 场景连续性；scene_setting 最多 18 字，只作时间+地点标签；同场景必须接上镜，换场必须写承接 | `scene_setting"{x}"在 shots[i] 与 shots[j] 间被打断` / `缺少承接说明` |
| V7 | shot_no 连续 / 景别不三连 / 枚举值合法 | 同模板 |
| V8 | 固定 10s 密集视频段：action_desc 至少 max(70, duration×7) 字；剧情载荷至少 duration×12 字；禁止纯画面空镜 | `剧情载荷不足` / `固定 10s 视频段必须加入旁白或台词` |
| V9 | 目标时长对应精确镜头数：target/10 | `镜头数{x}不匹配；目标{target}s必须正好{n}个镜头` |

> V5 的动作节拍检测用标点分段启发，宁可漏判不可误判；V6 的上下文接力只挡明显断裂，核心仍由提示词要求模型先做连续剧情链。

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
    return f"{text} --ratio 9:16 --dur 10"  # 视频生成统一 10s
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
