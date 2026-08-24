# MiniMax H3 官方提示词格式（基础四模式）

依据官方 `VIDEO_PROMPT_WRITING_GUIDE_base_en.md`（MiniMaxAI/MiniMax-H3 仓库）整理。字段名与固定指令行是接口语法，必须逐字符照写；示例为本 skill 自撰，仅示范结构。

目录：
1. 四种模式与固定指令行
2. 三个核心字段
3. Shot 与切点
4. 运镜：类型 + 幅度 + 速度
5. 说话人、对话与歌唱
6. 屏上文字
7. 各模式的关键帧写法
8. Ref2VA（全参考模式）
9. 完整示例（自撰）

---

## 1. 四种模式与固定指令行

| 模式 | 输入 | 用途 |
|---|---|---|
| T2VA | 纯文本 | 从零构建音视频时间线 |
| I2VA | 首帧图 ×1 | 从给定画面向前发展 |
| FL2VA | 首帧 + 末帧 | 在两帧之间生成连续路径 |
| L2VA | 末帧图 ×1 | 反推前置状态，收敛到末帧 |

带图模式的**第一行必须是固定对齐指令**，之后空一行再接三字段。T2VA 没有指令行，直接以字段开头。

I2VA 固定用：

```
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
```

FL2VA 固定用（`S.SS` 填视频实际时长，两位小数；`Shot N` 填末帧所属的实际镜头号）：

```
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.
```

L2VA 固定用：

```
How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.
```

这些句子是模型识别模式的信标，改一个词都可能让图像对齐失效。不要翻译成中文，不要改写。

## 2. 三个核心字段

```
integrated_multimodal_description: ...

overall_soundscape: ...

non_diegetic_music: ...
```

**integrated_multimodal_description（多模态综合描述）** —— 主字段，沿时间线写：视觉风格、构图、主体外观与位置、场景与关键道具、动作与反应、切镜、运镜、说话人与台词、歌唱、剧情内音效。观众能看到或听到的都在这里；看不到听不到的（意图、心理、氛围形容词）不要写。

**overall_soundscape（整体声音景观）** —— 1-4 句英文连续文本，概括全片环境音、物理动作音、非语言人声（风雨、车流、脚步、布料摩擦、撞击、呼吸、笑声）。对话、歌唱、剧情内音乐已属主字段，此处不重复。只有用户明确要全片静音才写 `N/A`——留空不等于静音，音画联合生成的模型会自己补声音，且不受控。

**non_diegetic_music（非剧情音乐）** —— 1-3 句英文，只写观众能听到、角色听不到的配乐。聚焦乐器、速度、节奏、力度变化；不写抽象情绪词，不解释配乐的叙事功能。角色能听到的音乐（收音机、现场演奏、手机铃声）属于剧情内事件，放主字段。没有配乐写 `N/A`。

## 3. Shot 与切点

- `[Shot 1]` 不带时间戳，开头先声明整体风格与初始构图。风格关键词：`Cinematic`、`live-action`、`2D-animated`、`3D CG`、`claymation`、`watercolor`、`vintage film`。带图模式从参考图推断风格，T2VA 从用户意图选。
- 后续镜头编号递增，切点时间**严格递增**且落在总时长内：`[Shot 2] At 00:03.500, the camera cuts to ...`
- 切换动词用 `the camera cuts to` / `the shot cuts to` / `the shot transitions to` / `the shot changes to` / `the shot switches to`。溶解、淡入淡出、划像只在用户明确要求时用。
- **切镜的门槛**：一次切换要带来主体、空间、状态、视角或时间上的新信息。只想换景别或微调角度，用运镜，不要切。

## 4. 运镜：类型 + 幅度 + 速度

完整运镜表达三维：类型（怎么动）、幅度（构图变化多大）、速度（多快）。中等幅度与常速直接省略，只在偏离默认时写。

| 类型 | 含义 |
|---|---|
| Zoom In / Zoom Out | 变焦，机位不动 |
| Push In / Pull Out | 机身前移 / 后移 |
| Pan Left / Pan Right | 机位不动，水平转动 |
| Truck Left / Truck Right | 机身水平平移 |
| Tilt Up / Tilt Down | 机位不动，垂直转动 |
| Pedestal Up / Pedestal Down | 机身垂直升降 |
| Arc Shot | 绕主体弧线移动 |
| Tracking Shot | 跟拍移动主体 |
| Static Shot | 完全静止 |
| Shake Slightly / Shake Strongly | 轻微 / 强烈手持抖动 |
| POV | 主观视角 |
| Roll Clockwise / Roll Counterclockwise | 绕光轴旋转 |

幅度：`with small amplitude` / `with large amplitude`。速度：`at slow speed` / `at fast speed`。

**写成镜头内的自然英文动作句，不要堆在句尾当标签：**

对：`The camera pushes in with small amplitude at slow speed toward the gourd in his hands.`
错：`..., push in, small amplitude, slow, cinematic.`

## 5. 说话人、对话与歌唱

- 发声主体（说话、唱歌、画外音）用稳定 ID：`(S1)`、`(S2)`；多人同声用复合 ID `(S1,S2)`。同一人跨镜头保持同一 ID；不发声的角色不给 ID。
- 说话人首次出现时，在 `<d>` **外**给足身份信息：角色类型、年龄、性别、是否出镜、音高、音色、语速、口音。
- `<d>` **内**只放语言标签与逐字原话，不改写、不翻译：

```
The thin teenage scholar with a young, slightly hoarse voice (S1) says: <d>[Chinese] 总要活下去。</d>
```

- 画外音用固定短语 `says in an off-screen voiceover`，且每个画外音 `<d>` 块后立刻说明出镜角色嘴唇闭合（否则会给不该说话的人对口型）：

```
The old carpenter (S2) says in an off-screen voiceover: <d>[Chinese] 那年他才十六。</d> while the boy's lips remain completely closed.
```

- 台词或歌声跨镜头延续：在衔接的两端都放 `<scenetrans>`，并写明声音跨切换持续（`continues seamlessly across the cut` 等表达）。被片尾截断的台词用 `<cutoff>`。

## 6. 屏上文字

画面中实际出现的招牌、横幅、标签、字幕、霓虹字：英文双引号包裹，逐字保留原文与标点，**不翻译**：

```
An ancient stone tablet carved with the characters "靠山宗" stands before the mountain gate.
```

H3 的文字与品牌呈现是官方主打能力，但书法体、密集小字等复杂字形建议先跑一条测试镜验证，通过后再进正片。

## 7. 各模式的关键帧写法

**I2VA**：Picture 1 是 0.00 秒的真实首帧，属于 Shot 1。先确立图中的风格、主体、构图、场景锚点，再写下一个动作；人物身份、服装、颜色、关键物体、空间关系必须与图保持一致。结构：首帧锚定 → 动作启动 → 连续发展 → 结果或反应。

**FL2VA**：Picture 1 开场、Picture 2 结尾，主体写两帧之间的**运动路径**（姿势怎么变、物体怎么被操作、构图怎么演变、光线怎么过渡），不要把两张图各自静态复述一遍。**官方偏好单镜头**，让模型连续插值；只在明确要求时拆多镜。末帧必须由最后一个 Shot 在片尾到达。结构：首帧状态 → 可观察的中间变化 → 差异逐步收窄 → 末帧状态。

**L2VA**：Picture 1 是最后一帧，属于最后一个 Shot（不天然属于 Shot 1）。先反推一个合理的前置状态，再写人物、物体、镜头、场景如何逐步逼近参考图。结构：前置状态 → 动作与过渡路径 → 末镜头逐步收敛 → 末帧落地。

## 8. Ref2VA（全参考模式）

图片、视频、音频共同引导生成时使用，采用与基础四模式不同的**六段式规划格式**，核心原则：

- 每个素材编号并**显式派职责**：这张图锁角色身份、那段视频控制运镜与剪辑节奏、这段音频提供人声或配乐参考。职责不写等于让模型猜。
- 素材数量上限以官方平台当前显示为准（社区普遍报为图片 9 / 视频 3 / 音频 3，合计约 12 个文件；音频不能单独作为唯一输入，须与图像或视频同时提供）。
- 支持 V2V Motion Transfer：参考视频里的动作可以迁移给提示词描述的新主体。

Ref2VA 的完整六段模板本 skill 未收录（官方指南中它独立成篇且仍在更新），使用前从 MiniMaxAI/MiniMax-H3 仓库 docs 目录取最新版。

## 9. 完整示例（自撰，T2VA，约 10 秒两镜）

```
integrated_multimodal_description: [Shot 1] Live-action, cinematic, a medium shot frames a thin teenage scholar in a faded indigo robe standing at the edge of a mountain cliff at dusk, holding a dark yellow gourd. The camera arcs slowly around him with small amplitude as the wind inflates his robe; he draws his arm back and hurls the gourd outward, and the gourd leaves his hand toward the valley below. [Shot 2] At 00:06.000, the shot cuts to a high-angle view tracking the gourd as it tumbles down, remaining clearly visible at the center of the frame, until it strikes the rushing river and throws up a ring of white spray.

overall_soundscape: Strong mountain wind gusts across the cliff, fabric snaps and flutters, followed by a sharp exhale on the throw, the whistle of the falling gourd, and a heavy splash swallowed by the roar of the river.

non_diegetic_music: A single sustained low string note at a slow tempo, swelling slightly at the splash and then decaying to silence.
```

注意示例里的两处手法：道具锚定（`remaining clearly visible at the center of the frame`——防止关键道具在动作镜头里消失）与声音从画面倒推（衣料、抛掷呼气、坠落呼啸、落水，每个视觉事件都有对应声音）。
