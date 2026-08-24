# H3 社区实践与常见错误

汇总自 2026 年 8 月各接入平台（fal、RunDiffusion、Luma、deAPI、Morphic、Pixo 等）发布的实践文章与 HuggingFace 社区讨论。H3 发布不满一个月，这里的经验成熟度低于官方规范——与 `official-format.md` 冲突时以官方为准；标注"未实测"的条目在依赖它做决策前先自己验证。

---

## 一、被多个来源独立重复的实践（可信度高）

### 1. 每个参考素材显式派职责
所有实践文章重复次数最多的一条。"Image 1 锁角色身份与服装；Video 1 控制镜头路径与剪辑节奏；Audio 1 提供人声音色"——一句职责声明省掉三轮盲改。当一个素材承担多个职责时，把每个职责分别点名。

### 2. 时长预算：每条指令都在争时间
5-15 秒里塞四个运镜、三句台词、一次变身、若干次切镜，模型只能全部压缩执行，结果是每样都做了一半。写完先删：一个主动作、一次主运镜、最多两句台词。跑通了再逐项加回。

### 3. 身份锚：`the <特征> from Shot 1`
跨镜头维持同一人物，用"the young man in the dark-grey robe from Shot 1"这种回指写法，给模型一个显式的身份链接。不写回指，切镜后的人物是重新采样的。

### 4. 重复性事件当时间锚
长镜头容易漂移（画面内容缓慢偏离设定）。放一个周期性事件——旋转的吊灯、闪烁的霓虹、规律驶过的列车——给模型一个节拍参照，实测能明显压漂移。

### 5. soundscape 从画面倒推
写完画面后逐项自问"这东西是什么声音"：碎石上的脚步、布料蹭过皮肤、金属刮金属。给 overall_soundscape 多写两句物理声，成片质感差别显著。反过来，**不写声音的代价是模型自己发挥**——音画联合生成没有"默认静音"这回事，不受控的音轨常表现为含混的语音状杂音。

### 6. 一次只改一个字段，固定种子
三个字段对应不同的生成子系统。固定 seed、只改 non_diegetic_music 重跑，就能单独判断配乐描述是否起效。三个字段一起改等于放弃归因。

### 7. 结构完整优于篇幅
官方示例提示词中位长度约 130 个中文字符量级，最长的个例到 600-800 字符。判断标准是六要素齐不齐（主体、动作、环境、运镜、光影/风格、约束），不是字数。

---

## 二、常见错误（错误示范 → 修正）

### E1 把运镜写成句尾标签
错：`A girl walks through the alley, push in, slow motion, cinematic, 4K.`
对：`The camera pushes in with small amplitude at slow speed as she walks deeper into the alley.`
根因：H3 按官方规范把运镜理解为镜头内的动作事件，标签堆无法绑定到具体时刻。

### E2 用"cinematic"代替具体镜头指令
错：只写 `cinematic, high quality`。
对：写可见的东西——`a low-angle medium shot, rim light from the doorway, shallow depth of field`。
根因："高质量"不含任何可执行信息；风格词只在 [Shot 1] 开头声明一次（`Live-action, cinematic`），之后全部用具体描述。

### E3 台词写在 `<d>` 外，或在 `<d>` 里夹动作
错：`她哽咽着说下一站就下车`
对：`The young woman (S1), her voice catching, says: <d>[Chinese] 我下一站就下车。</d>`
根因：`<d>` 内只放语言标签和逐字原话；情绪、动作、身份全部放块外。混写会导致台词被改写或口型错位。

### E4 画外音没关嘴
错：只写 `says in an off-screen voiceover: <d>...</d>`
对：块后补 `while his lips remain completely closed.`
根因：不显式关嘴，模型可能给画面里的人对上口型，画外音变成出镜台词。

### E5 抽象情绪词写进 non_diegetic_music
错：`sad emotional music`
对：`Sparse piano notes at a slow tempo, joined by sustained low strings that swell and fade.`
根因：该字段按官方规范只认乐器、速度、节奏、力度。

### E6 该运镜的地方切了镜
错：`[Shot 2] At 00:04.000, the shot cuts to a slightly closer view of the same table.`
对：删掉 Shot 2，在 Shot 1 内写 `The camera pushes in slowly toward the table.`
根因：官方要求每次切换携带新信息（新主体/新空间/新状态/新视角/新时间）；纯景别变化用运镜。

### E7 FL2VA 把两帧各自静态描述一遍
错：分别复述 Picture 1 和 Picture 2 的画面内容。
对：只写两帧之间的**运动路径**——姿势如何变化、物体如何被操作、构图如何演变。
根因：两帧本身模型已经看到了，提示词的增量信息是路径；复述会挤占路径描述的空间，且诱发中途切镜（FL2VA 官方偏好单镜头插值）。

### E8 屏上文字让模型自由发挥
错：`a shop sign above the door`（模型自己编字，可能乱码）
对：`a wooden shop sign reading "王记木器" above the door`
根因：H3 能渲染指定文字，但不指定时生成的字不可控。要么用双引号给原文，要么写 `a blank wooden sign` 明确无字。

---

## 三、单来源报告（未充分交叉验证，用前自测）

- **原生时长栅格**：有社区讨论称模型内部按 17k+5 帧的栅格出片（约 5 秒起步、362 帧约 15 秒），非整数秒请求会被就近对齐。实际以接入平台的时长选项为准。
- **素材上限**：图片 9 / 视频 3 / 音频 3、合计约 12 个文件的说法在多个平台页面出现，但各接入方的限制可能不同；音频不能作为唯一输入（须伴随图像或视频）出自 ModelScope 的模型说明，可信度较高。
- **In-context 编辑语法**：社区示例用自然语言指定帧区间或画面区域做局部替换（例如保持背景与主体位置、只换外套），具体语法各平台包装不同，未见统一官方文档。
- **H3-Context-IR 增强**：官方 API 可以把粗提示词扩写成规范格式（只返回增强提示词，不生成视频）。预算允许时，拿它的输出对照检查自己手写的提示词，是校准写法的捷径。

---

## 四、迭代策略

1. 768P 低价档跑创意迭代，内容锁定后走 768P→2K 再生成升清（提交时需原样带上生成 768P 时的全部内容，再附源视频）。
2. 局部瑕疵优先 in-context 编辑，其次单镜重跑，最后才整段重生成。
3. 每次生成记录：seed、模式、三字段全文、素材清单及职责。没有这份记录，"一次只改一处"无从谈起。
