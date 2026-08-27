# 作者的话（paratext）识别重复计算 —— 只读调查 + 方案

调查范围：`app/source_paratext.py` 及其三个（声称的）消费点。**只读调查，未改任何代码。**

结论先行：user 的判断成立——**paratext 是原文的属性，不是某个台子的属性**，应该按章算一次、持久化、
所有下游读同一份。当前实现不是"重复算了两次"这么简单，是**三种互不相同的机制**在判同一件事，
其中一种是死代码，一种是活的但只覆盖一小部分章节，还有一种是完全独立发明的、从未被文档提及的
第三套判据——这一条是本次调查最大的意外发现，见 Q1。

---

## Q1：重复到什么程度

### 集与章的关系
`app/planning.py:4` 明文：`maps each resulting chapter to exactly one episode without an LLM`，
`replan_project_episodes` 的产出记 `"rule": "one_chapter_one_episode"`（`app/planning.py:255,290`）。
这不是数据巧合，是架构约束——**任何时候都是 1 集 = 1 章**。实测 `proj_195be7df1fd6`（我欲封天）：
1616 章、1616 集，`episodes.source_chapters` 全部是单元素数组（`[1]`、`[2]`……`[1616]`），
零例外。`app/domain/common.py:214` 的 `_episode_source_text` 虽然结构上支持多章拼接
（`"\n\n".join(f"【{ch['title']}】\n{ch['content']}" ...)`），但目前从未被多章场景触发。

### 世界书覆盖范围
`_bible_paratext_scope`（`app/stages.py:4457`）= 头部窗口（`BIBLE_HEAD_CHAPTERS=10` +
`BIBLE_LOOKAHEAD_CHAPTERS=10` → 至少前 20 章）∪ 后段抽样章（`_BIBLE_TAIL_SAMPLE_MAX=12`，
均匀分布到全书）。用真实 1616 章数据跑了这个纯函数（未调用任何模型，只用章节字数）：

```
purified chapter idx = [1..20, 146, 280, 414, 547, 681, 815, 948, 1082, 1216,
                         1349, 1483]
count = 31 / 1616 = 1.918%
```

即：世界书只对 **31 个集**（前 20 集 + 11 个散落在全书的抽样集）做过 paratext 判定，
其余 1585 个集，世界书从未触碰。

### 三条消费路径的真实状态
| # | 位置 | 机制 | 触发条件 | 覆盖 |
|---|---|---|---|---|
| 1 | `app/stages.py:4513`（`_chapters_without_paratext`） | `strip_paratext`/`PARATEXT_RULE`，整段锚点式 | 每次生成/重生世界书，对 scope 内的章 | 31 章，且受 120s 预算硬顶（见 latency 报告，`chapters_stripped=8/31` 实测） |
| 2 | `app/production/prep_pack.py:2544`（`_discover_new_characters`） | 同一 `strip_paratext`/`PARATEXT_RULE` | **仅当**本集存在未解析新角色（`unresolved_chars` 非空，`prep_pack.py:4013-4021`） | 条件触发，不是每集必跑；命中率无法从当前数据估算（该项目尚无一集跑完映射台，见下） |
| 3 | `app/domain/screenplay_ops.py:1189`（`_screenplay_character_discovery`） | 同一 `strip_paratext`/`PARATEXT_RULE` | 无——**死代码** | 0 |

**#3 是死代码，证据链：**
- 唯一调用方是 `screenplay_repair.py:2758` 的 `ensure_source_characters_incremental`（`screenplay_repair.py:2751`）。
- 全仓库（`app/` 下排除测试）没有任何地方调用 `ensure_source_characters_incremental`——只有测试文件
  `import screenplay_repair` 做其他用途，没有一处真正调用这个函数。
- 当前活的剧本生成入口 `_screenplay_task`（`app/domain/screenplay_ops.py:1249`）自己的 docstring
  写明："替代原先的蓝图→场次分片→编译→修复回路（**休眠保留于 app/production/screenplay_repair.py 等，
  未从本调用路径引用**）"。`_screenplay_task` 只调用 `run_episode_prep_pack`（`app/production/prep_pack.py`），
  不经过 `screenplay_ops._screenplay_character_discovery`。
- `app/narrative_blueprint.py` 里更精细的 `narrative_layer=paratext`/`render_policy=exclude_from_spine`
  蓝图分类器同属这套休眠子系统——`app/production/prep_pack.py` 对 `narrative_blueprint`/`NarrativeBlueprint`
  零引用，实测确认（`grep` 全文件无命中）。这解释了 #3（下）为什么要另起炉灶自造一套粗糙判据：
  真正精细的分类器已经随旧管线一起休眠，映射台拿不到它的产出。

**结论：user 已知的两个消费点（世界书 / 映射台）之外，实际只有这两个是活的；第三个（screenplay_ops:1189）
确认死亡，0 次调用。**

### 意外发现：映射台内部还有第三套、完全独立的 paratext 判据

映射台真正喂给分镜台的 `coverage_ledger.paratext`（`storyboard_pack.py:445-466` 直接读取，
不重新判定，见 `storyboard_pack.py:429-433` 的注释）**不是来自 `strip_paratext`**，而是来自
`_extract_chunk`（`app/production/prep_pack.py:4326`，注释自称"映射台的唯一模型调用"）内嵌的
自报字段 `paratext_segments`（`prep_pack.py:4428-4431`）：

```
另外给出 paratext_segments：本段编号中，属于"非故事内容"的编号列表——章节标题、
作者对读者说的话（求收藏/求推荐/月票/上架/加更/催更等）、网站公告，这些不是故事
叙述本身……你自己就能判断哪些是——按内容本身判断，不用管它们在本段的位置。
```

这段措辞和 `PARATEXT_RULE`（`source_paratext.py:40-47`）**不是同一份文案**，`temperature`
也不同（`_call_structured` 默认 `temperature=0.2`，`prep_pack.py:4278`；`paratext_spans` 固定
`temperature=0.0`，`source_paratext.py:183`），粒度也不同（#3 是"这个编号段是不是 paratext"的
布尔自报，#1/#2 是整段文本里的首尾锚点定位）。且 #3 对**每一集、每一个 chunk 都无条件执行**——
它是主抽取调用自带的字段，不像 #2 那样有 `unresolved_chars` 门槛。

`章节标题`这部分不是模型判的：由 `app.source_excerpt.chapter_title_segment_indexes`
（数据库锚点 `chapters.title` 确定性算出，`prep_pack.py:4708`：
`paratext_indexes = set(deterministic_title_indexes) | declared_paratext_segments`）算出后
和模型自报的部分取并集。这部分是对的，不用动。

**修正后的机制清单（活的）：**
1. 世界书 `strip_paratext`（`PARATEXT_RULE`），scope=31/1616 章。
2. 映射台角色发现 `strip_paratext`（同一 `PARATEXT_RULE`），条件触发（未解析新角色时）。
3. 映射台主抽取调用自报 `paratext_segments`（**独立措辞、独立温度**），每集每 chunk 无条件执行，
   产出 `coverage_ledger.paratext`，是分镜台唯一读到的账。

### 量化：全流程跑完 1616 集，paratext 判定会发起多少次模型调用

- 机制 1：31 次（一次性，跟着世界书生成走，不随集数重复）。
- 机制 2：≤1616 次，实际次数取决于每集是否有未解析新角色——**当前数据库里这个项目还没有
  任何一集跑完映射台**（`episodes.screenplay_status` 全部 `pending`，`shots` 表 0 行），
  无法从实测数据估计命中率，如实标注"查不清"。
- 机制 3：不是独立的"paratext 专用调用"，是主抽取调用（本身就是映射台唯一的、必须发生的模型调用）
  自带的一个字段，不产生额外调用次数，但产出的判断结果是三者中唯一被下游（分镜台）信任的。

**重复/浪费的部分**：机制 1 覆盖的 31 章，机制 3 会**再判一次**（因为机制 3 对每一集无条件执行，
不管机制 1 是否已经判过）。这 31 次是结构上确定的重复——同一段原文，同一件"是不是作者的话"的
判断，被两种不同措辞、不同温度的机制各判一次，而且**互不知道对方的存在，结果也从不对照**。
其余 1585 集，机制 1 从未触达，机制 3 是唯一判据，这部分不是重复，是唯一来源（见 Q4）。

---

## Q2：两次判定会不会给出不一致的结果（本任务最重要的问题）

### 提示词/模型/温度对照

| | 机制 1（世界书） | 机制 2（映射台·角色发现） | 机制 3（映射台·主抽取自报） |
|---|---|---|---|
| 提示词 | `PARATEXT_RULE`（`source_paratext.py:40-47`） | 同左，逐字共用 | 独立措辞（`prep_pack.py:4428-4431`） |
| 温度 | 固定 `0.0`（`source_paratext.py:183`） | 同左 | 默认 `0.2`（`prep_pack.py:4278`，未显式覆盖） |
| 判定粒度 | 整章文本里首尾锚点定位一段区间 | 同左（对象是 `【标题】\n正文` 拼接后的整段） | 按 `index_source_segments` 切出的编号段，逐段布尔自报 |
| 模型选择 | 项目级 `bible_text_provider`（`app/domain/bible_ops.py:722-730` 用 `stage_text_provider(...)` 包裹） | 项目级 `script_text_provider`（`app/domain/screenplay_ops.py:1292-1295` 同样用 `stage_text_provider(...)` 包裹） | 同机制 2（同一次 `run_episode_prep_pack` 调用内，共享同一个 `stage_text_provider` 作用域） |

**关键事实：世界书和映射台的文本模型是项目级独立配置的两个下拉框**
（`app/model_registry.py:93-104` `text_model_choices()`，UI 是"世界书/映射台/分镜台"三个环节各自选
provider；`app/db.py:1745`、`app/domain/projects.py:977` 注释同源）。如果用户给这两个环节配了
不同的 provider/model，机制 1 和机制 2 会用**两个不同的模型**回答同一份措辞完全相同的提示词——
这不是假设，是这套配置机制的设计初衷（分环节独立选模型）。机制 3 更进一步，连措辞和温度都不同。

### 集的边界会不会把一段作者的话切成两半

不会——因为集边界=章边界（1:1，见 Q1），而每章的 `content` 在导入时是一次性写入、原子的
（见 Q3"存量项目"一节的导入路径分析），一段作者的话不可能横跨两个 `chapters.content`。
`_episode_source_text` 给映射台看到的文本 = `【title】\n` + 该章 `content`，只是加了个标题包装，
和世界书看到的 `chapter["content"]` 几乎是同一段字符串（差一个固定前缀）。**在当前 1:1 架构下，
这条担心不成立**——但代码结构上仍然支持多章拼一集（`source_chapters` 是数组），如果未来真的
出现多章集，一段作者的话理论上仍然只会落在某一章的 `content` 内部（不会跨章），因为它是原文本身
的一部分，切章逻辑（`app/ingest.py`）不会把同一段连续文字拆进两条 `chapters` 记录。所以"切成两半"
在本仓库现有的分章模型下不是一个真实存在的风险，不用为它设计防御。

### 有没有实际证据

查了 `data/manju.db`（只读连接）的 `provider_calls` 表，按 `operation_id LIKE '%paratext%'`
和 `meta LIKE '%paratext%'` 检索：

```
24 条 kind='chat'，operation_id 全部形如 bible.paratext:<chapter_id>（含 :structured-attempt: 重试）
1 条 kind='character_bible_paratext'（世界书净化步骤的汇总记账）
0 条 operation_id LIKE '%discovery.paratext%'
```

即：**当前数据库里，映射台的 paratext 相关调用（机制 2、机制 3）一次都没有发生过**——因为这个项目
目前还没有任何一集跑完映射台（`episodes.screenplay_status` 全部 `pending`，`shots` 表 0 行，
唯一跑过的是世界书生成，且这次世界书生成本身刚刚失败，见另一份延时调查报告）。

**如实结论：当前数据里查不到机制 1 vs 机制 2/3 对同一段原文判定结果是否一致的实证——不是"查过没发现
问题"，是"这条数据链路还没有被实际执行过一次，无从比对"。** 风险判断只能基于代码结构（措辞/温度/
模型三处都可能不同），不能基于观测证据，这一点必须明确告知——不能因为"查不到反例"就说"没问题"。

---

## Q3：正确的架构

### `chapters.cleaned_lines` 是不是已经有半个实现——查证：不是

`chapters.cleaned_lines INTEGER DEFAULT 0`（`app/db.py:122`）从项目初始骨架提交
（`f2a0fd4`，"漫剧Agent 2.0：真实链路 + 一致性三层机制"）就存在。全仓库检索：
- `app/` 下没有任何一处 `SELECT`/`UPDATE` 涉及这一列。
- 唯一出现在 `tests/test_storyboard_repair_v2.py:101` 的一条 `INSERT` 语句里，作为占位值 `0` 填入，
  测试本身不断言它的行为。

**结论：这是一个从未被接上的遗留列，不是 paratext 的半成品实现，也不是任何其他功能的半成品**——
只是建表时留下的空位，从未被任何代码读写过。新方案不应该复用它（名字、类型都不贴合：`INTEGER`
存不了 span 列表），应该新增专用列，但要在方案里记一句"这张表已经有一个从未使用的遗留列"，
避免以后又有人误以为它是伏笔。

### 存哪里

新增 `chapters.paratext_json TEXT`（可空，`NULL` = 尚未计算，区别于"算过但没找到任何 paratext"
= `'{"content_hash":"...","spans":[],"computed_at":...}'`）。存**已解析的字符偏移**，不存锚点字符串：

```json
{
  "content_hash": "<blake2b(chapters.content), 复用 source_paratext._cache_key 的算法>",
  "spans": [{"start": 123, "end": 456}, ...],
  "computed_at": 1787810000.0
}
```

理由：
- 锚点字符串（`ParatextAnchor.start/end`）只是模型交付判断结果的**传输格式**，是为了绕开"模型报不出
  长文本逐字复述"这个已知限制（`source_paratext.py:57-60` 的注释）。锚点定位到区间（`_anchor_region`）
  是一次性动作，没必要每次消费都重新做字符串查找——查找结果本身（`begin, end` 整数对）才是需要长期
  持有的事实。
- `content_hash` 是防御性字段：当前 `chapters.content` 在本仓库确认是写入后不再原地修改的
  （导入 `app/domain/projects.py:252-278` 一次性 `INSERT`；重新导入走整项目删除重建
  `app/domain/projects.py:1654` `DELETE FROM chapters WHERE project_id=?`，新行拿新 `id`，旧 span
  连同旧行一起消失，不存在"同一行、内容变了、span 还留着旧的"这种情况）。所以这个字段今天不会被
  触发，但按 CLAUDE.md 的所有权纪律，任何"缓存 vs 源"的关系都不该留默认成立的假设——加一个廉价的
  校验字段，为将来万一出现的章节编辑功能兜底，代价只是多存一个哈希。

`spans` 为空数组是合法的"确定没有 paratext"，和 `paratext_json IS NULL`（"还没算过"）在语义上
必须严格区分——这正是 CLAUDE.md"空集合不等于无需检查"那条纪律在这里的具体落点：调用方判断
"要不要发起计算"时，必须查的是 `paratext_json IS NULL`，不能查 `spans` 是否为空。

### 什么时候算：惰性，首次被需要时

不在导入时（`app/ingest.py`/`app/domain/projects.py` 的导入路径）批量算：一本 1616 章的书，
世界书只会读其中 31 章，映射台会读全部 1616 章但**只在这一集真正被生产时**才需要。导入时全量算
等于把"净化了 643 章、真正读的只有 33 章"这个 `_chapters_without_paratext` 文档里已经骂过的反模式
（`app/stages.py:4491-4498` 的事故记录）在更早的阶段重犯一次。

改成：任何消费方（世界书的 `_chapters_without_paratext`、映射台的角色发现、映射台的主抽取账目
投影）需要某一章的 paratext 结果时，先查 `chapters.paratext_json`：
- 非空且 `content_hash` 匹配 → 直接用，零模型调用。
- 为空或哈希不匹配 → 调用 `paratext_spans`（沿用 `PARATEXT_RULE`，不再需要机制 3 那套独立措辞）算一次，
  `UPDATE chapters SET paratext_json=? WHERE id=? AND content_hash 匹配`原子写回，返回结果。

谁先问就谁先算、算完落库，后来者白捡。31 章会被世界书第一次问到时算掉，其余 1585 章会在各自集第一次
进映射台时算掉——不需要额外一次"预热全书"的批处理，也不会有任何一章被算了却没人用。

### 映射台拿本集切片时，怎么映射回章内偏移

现状 1:1 架构下最简单：映射台的 `source_text` = `f"【{title}】\n{content}"`，相对该章
`paratext_json.spans` 里的偏移只差一个固定前缀长度 `len(f"【{title}】\n")`。取出该章持久化的
`spans`，整体加上这个前缀长度，就是这些 span 在 `source_text` 里的偏移——纯算术，零模型调用。

要为将来可能出现的多章集打好底子（`source_chapters` 数组结构上允许多章），偏移换算要做成通用函数：
按 `_episode_source_text` 的拼接顺序（`"\n\n".join(...)`，每章前缀 `f"【{title}】\n"`），累加每一章
"标题前缀 + 该章 content 长度 + 分隔符长度"，得到每章在 `source_text` 里的起点，再把该章持久化的
`spans` 整体平移到这个起点。这是一个和 `_episode_source_text` 的拼接公式**逐字对应**的纯函数，
两处不能各写一份、必须共用同一份拼接口径（否则又是一次"两处判据各自实现导致漂移"的重犯）。

有了 `source_text` 上的绝对偏移之后：
- **需要"净化后的整段文本"的消费方**（世界书渲染源文本、映射台角色发现的 `discovery_text`）：
  直接复用现成的 `remove_spans(text, spans)`（`source_paratext.py:121`，纯函数，不用改），
  传入偏移已经不需要走锚点重新查找那一步（可以给 `remove_spans` 加一个"已经是绝对偏移，不用再
  `text.find()`"的直传分支，或者更简单——`remove_spans` 本身接受的是 `ParatextAnchor`（字符串锚点），
  这里要么保留锚点字符串一并持久化用于兼容旧签名，要么给这个函数加一个接受 `(start,end)` 整数区间的
  重载。倾向后者：新增一个纯偏移版本的删除函数，`remove_spans` 原样保留供其他调用方（如果有）不受影响。
- **需要"这个编号段是不是 paratext"的消费方**（映射台的 `coverage_ledger.paratext`，取代机制 3 的
  模型自报）：对 `index_source_segments(source_text)` 产出的每个 `SourceSegment`（自带
  `start_offset`/`end_offset`，`source_excerpt.py:41-45`），判断它是否被某个持久化 paratext 区间覆盖
  （区间重叠判断，纯算术）——和 `chapter_title_segment_indexes`（`source_excerpt.py:225`）现在做的事情
  同一量级，可以放在同一个模块。

### 现有的进程内缓存要不要保留

保留，但降级为纯优化层，不再是唯一的持久化手段。`source_paratext.py:49-51` 的 `_CACHE`
（`OrderedDict`，容量 256，`blake2b(text)` 为键）在重启后清空、跨进程不共享——今天它是唯一的"避免
重复调用"机制，重启一次就全部作废。新架构下，DB 才是权威来源；进程内缓存只是"本进程本次运行内，
避免同一段文本被问两次"这个更小范围的优化（例如一次批处理里同一章被并发路径各问一次的情况），
继续保留没有副作用，不需要为了新架构去动它。

### 存量项目怎么办

不需要专门的迁移/回填脚本。`chapters.paratext_json` 新增列，默认 `NULL`——对所有存量项目
（包括 `proj_195be7df1fd6` 这个已经导入了 1616 章但还没跑过映射台的项目）而言，`NULL` 就是
"尚未计算"的正确初始状态，惰性计算会在这些章第一次被世界书/映射台问到时自然补上，不存在
"存量数据格式不对"的问题。唯一要注意的是**迁移脚本本身**（`app/db.py:1491` 的 `MIGRATIONS` 元组）
要用这个仓库既有的 `ALTER TABLE ... ADD COLUMN` 幂等追加模式（`app/db.py:1505-1528` 那一段的写法），
不需要新写一套迁移框架。

---

## Q4：映射台那次到底还需不需要——直接回答

**不需要"再发一次模型调用"，但需要"做一次确定性投影"，这两者是完全不同量级的操作，新架构下
这个区别本身就是答案。**

- 如果世界书已经覆盖了该集对应的章（31/1616）→ 映射台**不再需要任何模型调用**：
  `chapters.paratext_json` 已经被世界书那次计算写好了，映射台直接读，做一次偏移换算。
  在**当前实现**里，这 31 集是纯重复——机制 1 和机制 3 各判一次，互不知道对方，这是本次调查
  确认的真实浪费点。
- 如果世界书没有覆盖该集（1585/1616，第 21 集及之后大部分集）→ 在**当前实现**里，映射台的
  机制 2/3 是这些集唯一的一次判定，不能删，删了这些集永远没有 paratext 账目。

新架构下，"覆盖 vs 没覆盖"这个区分在**调用点**上消失了：映射台永远只做"查 `paratext_json` 是否
已算过 → 没有就自己算一次并落库 → 用"，不用关心这次是不是世界书已经替它算过。如果世界书替它算过，
它读到的是非空值，跳过模型调用；如果没有，它自己算，跟今天的行为在"总归有一次判定"这一点上等价，
唯一的区别是这次判定的结果会被持久化、供后续任何消费方（包括未来可能新增的消费点）复用，而不是
算完就扔。

---

## 新架构设计（汇总）

1. `chapters` 表新增 `paratext_json TEXT`（可空）。
2. `app/source_paratext.py` 新增两个纯函数：
   - 一个"给定 chapters 行 + 已缓存的 `content_hash`，判断是否需要重算"的判定（读列即可，不用改
     `paratext_spans`/`strip_paratext` 本体的模型调用逻辑，那部分继续用 `PARATEXT_RULE`）。
   - 一个"按绝对偏移应用/投影"的纯函数族：给文本删区间（偏移版 `remove_spans`）、给一组
     `SourceSegment` 打 paratext 标记（偏移区间与 segment 区间求重叠）。
3. 新增一层"取某章 paratext 账目"的入口函数（建议放在 `app/source_paratext.py`，因为这是它现有职责
   的自然延伸）：接收 `conn` + 章节行，查列→命中返回，未命中→调用模型→写回→返回。这一层需要处理
   并发写回的幂等性（多个消费方同时问同一章时，`UPDATE ... WHERE content_hash 匹配`即可保证后写者
   不覆盖先写者的等价结果，不需要额外加锁——两次算出的结果即便字面不同也都是对同一份 `PARATEXT_RULE`
   的合法回答，谁先落库都行）。
4. `app/stages.py:4477`（`_chapters_without_paratext`）：改为调用新入口函数，不再直接调
   `strip_paratext` 拿到手就丢；保留现有的并发批量结构（`BIBLE_PARATEXT_CONCURRENCY`、
   `BIBLE_PARATEXT_BUDGET_S` 那套超时保护逻辑不变，只是把"净化"这一步换成"查/算/落库"）。
5. `app/production/prep_pack.py:2544`（`_discover_new_characters` 的 `discovery_text` 构造）：改为
   走同一个新入口函数，不再直接调 `strip_paratext`。
6. `app/production/prep_pack.py:4428-4431`（`_extract_chunk` 提示词里的 `paratext_segments` 字段）：
   删除这个字段的提示词文案和 `_ChunkResponse.paratext_segments` schema 字段；`prep_pack.py:4610-4627`
   （`declared_paratext_segments` 的收集逻辑）改为直接调用第 2 点里"给 segment 打标记"的纯函数，
   输入是该集从新入口函数取到的（已经确定性投影好的）paratext 偏移。`prep_pack.py:4708` 的并集逻辑
   （`deterministic_title_indexes | declared_paratext_segments`）保留，只是右操作数的来源从"模型自报"
   换成"确定性投影"。
7. `app/production/storyboard_pack.py:445-466`（`_paratext_segment_indexes`）：不需要改——它已经是
   "只读 `coverage_ledger.paratext`，不重新判定"的正确姿态，新架构只是让它读到的那份账更可信。
8. `app/domain/screenplay_ops.py:1146-1246`（`_screenplay_character_discovery`，含 1189 行的
   `strip_paratext` 调用）：确认死代码后，是否连带清理是本次方案的 P2 选项（见下），不在 P0/P1 范围。

---

## 改动清单

| 文件:行号 | 意图 |
|---|---|
| `app/db.py:1491`（`MIGRATIONS` 元组） | 追加 `"ALTER TABLE chapters ADD COLUMN paratext_json TEXT"` |
| `app/db.py:114-125`（`chapters` 建表 DDL） | 新库直接建表时也带上这一列，保持和 `MIGRATIONS` 幂等等价 |
| `app/source_paratext.py` | 新增：按章取/算/落库的入口函数；偏移版删除函数；segment 打标记函数。`PARATEXT_RULE`/`paratext_spans`/`strip_paratext` 本体不用改 |
| `app/stages.py:4477-4552`（`_chapters_without_paratext`） | 改调用点为新入口函数；`log_provider_call("character_bible_paratext", ...)` 的记账字段（`chapters_stripped`/`unfinished` 等）改成同时反映"命中缓存"与"实际发起模型调用"两类计数，便于以后再出现类似浪费时能一眼看出 |
| `app/production/prep_pack.py:2513-2557`（`_discover_new_characters`） | `discovery_text` 构造改调用点为新入口函数 |
| `app/production/prep_pack.py:4266-4450`（`_call_structured`/`_extract_chunk`） | 删除提示词里 `paratext_segments` 相关文案（第 4428-4431 行）；`_ChunkResponse` schema 去掉这个字段 |
| `app/production/prep_pack.py:4590-4630`（收集 `declared_paratext_segments` 的逻辑） | 改为调用确定性投影函数，输入来自新入口函数取到的该章 spans |
| `app/production/prep_pack.py:4474-4495`（`_prep_pack_build_coverage_ledger`） | 不需要改——它已经是"接收两个 set 做并集/差集"的纯函数，只是其中一个 set 的来源变了 |

---

## 迁移方案

不需要批处理回填脚本。新增列默认 `NULL`，惰性计算天然覆盖存量项目（包括当前这个 1616 章、
0 集跑完映射台的项目）。如果未来想加一个"提前预热全书"的管理员动作，是可选的 P2 功能，本次不做。

---

## 测试方案

现有测试清单（供改动时对照，不代表要全部重写）：
- `tests/test_source_paratext.py`：`paratext_spans`/`strip_paratext` 本体单测，本次改动不动这两个
  函数的对外行为，这批测试应该原样通过；新增"取/算/落库入口函数"需要新增对应用例（命中缓存不发起
  模型调用 / 未命中发起并落库 / `content_hash` 不匹配触发重算）。
- `tests/test_bible_prompt_and_precheck.py:692-750`：`_bible_paratext_scope`/`_chapters_without_paratext`
  的既有测试，改调用点后需要重新跑通；注意这个文件当前有另一个 agent 的并发在途改动
  （`git diff --stat` 显示 44 行改动，内容是 `visual_style_canonical` 相关提示词，与本方案无关），
  改动时按 hunk 拆，不要覆盖对方的改动。
- `tests/test_prep_pack_coverage.py:381-580`：这批测试直接构造 `declared_paratext_segments` 参数喂给
  `_prep_pack_build_coverage_ledger`，本次改动不碰这个函数本身，应该原样通过；但"谁产出
  `declared_paratext_segments`"这一段（`_extract_chunk` 提示词删字段后）需要新增测试，确认新的确定性
  投影函数产出的 set 语义与旧测试期望的 `paratext_segments` 语义等价（位置闸/连续尾窗块等既有约束
  见 `test_prep_pack_coverage.py:460` 附近的注释，需要确认新函数是否还需要这些防御——**新函数是确定性
  投影，不存在模型幻觉出不合理段号的可能性，这些防御闸大概率可以简化，但需要逐条对照原有测试用例
  的意图，不能盲删**）。
- `tests/test_storyboard_pack.py:128-150`：`_paratext_segment_indexes` 只读 `coverage_ledger`，
  不需要改，原样通过即可验证分镜台这一层未受影响。
- `tests/test_prep_pack_asset_discovery.py`：涉及 `_discover_new_characters` 的 paratext 净化路径
  （约 3076-3821 行附近），改调用点后需要重新跑通；这个文件很大，改动前先定位受影响的具体用例。
- 新增：验证同一段真实原文，走"新入口函数直接计算"与"走旧 `strip_paratext` 走两遍（模拟机制 1/机制 3
  当前的不一致风险）"两条路径，结果在**结构上**（覆盖的字符区间）应该完全一致——这是本次改造要
  兑现的核心承诺（消除不一致风险），需要一个显式的一致性测试用例，不能只测"能跑通"。

---

## P0/P1/P2 分级

**P0（本次必须做）：**
- `chapters.paratext_json` 建表迁移。
- `source_paratext.py` 新增取/算/落库入口函数 + 偏移版删除/投影纯函数。
- 世界书（`stages.py:4477`）、映射台角色发现（`prep_pack.py:2544`）改接新入口函数。
- 映射台主抽取调用（`prep_pack.py:4428-4431` 提示词、`_ChunkResponse` schema、
  `declared_paratext_segments` 收集逻辑）改为读确定性投影，不再让模型自报。
- 对应测试更新（见上）。

**P1（跟着 P0 一起验证，但不是硬依赖）：**
- `_prep_pack_build_coverage_ledger` 周边"位置闸/连续尾窗块"这类原本用来防模型幻觉的防御逻辑，
  逐条核对是否因为输入源从"模型自报"变成"确定性投影"而可以简化——不能想当然删，要有对照测试撑腰。
- 抽样几个真实项目（如果有已经跑过映射台的），对比"旧机制 3 的模型自报"与"新确定性投影"在同一批
  历史数据上的差异，作为迁移前的健全性检查（不是阻断性的，只是留痕）。

**P2（本次明确不做）：**
- `app/domain/screenplay_ops.py:1146-1246`（`_screenplay_character_discovery`）连同它唯一的调用方
  `screenplay_repair.py:2751`（`ensure_source_characters_incremental`）是否要整体删除——这属于
  "废止功能要一次删干净"的范畴，但这两个函数所在的休眠子系统（旧蓝图→场次分片→编译→修复回路）
  牵连面明显超出 paratext 这一个问题，需要单独立项核实这套休眠代码是否还有其他隐藏的存在理由
  （比如灾难恢复路径），不在本次范围内草率处理。
- "提前预热全书 paratext"的管理员批处理动作（惰性计算已经足够，不需要）。
- 进程内 `_CACHE`（`source_paratext.py:49-51`）的任何改动——继续保留原样。

---

## 风险与已知限制

- **本方案未经实测验证**：当前数据库里没有一集跑完过映射台，无法用真实数据回归"改动前后
  `coverage_ledger.paratext` 是否等价"这个最关键的对照实验，只能在实现后靠新增的一致性测试
  （见"测试方案"最后一条）间接验证。
- **多章集的偏移换算函数目前没有真实数据可以跑**（该项目 100% 是 1 集 1 章），这部分逻辑只能
  靠单元测试覆盖，不能靠这个项目的真实回归验证。
- **`_extract_chunk` 提示词删掉 `paratext_segments` 字段后，输出 schema 变小，理论上会略微降低
  该调用的输出 token 数**——这是本次改造的副产品，不是刻意追求的性能收益，不应该作为改动的主要
  理由，但值得在改动说明里提一句，避免被误解成"为了省 token 才删的"。
- **世界书使用的措辞（`PARATEXT_RULE`）是否完整覆盖机制 3 原有的判据范围**（机制 3 的提示词明确
  提到"网站公告"，`PARATEXT_RULE` 写的是"活动与更新公告"）——语义上应该等价，但没有做逐条案例
  的实证比对，这一条建议放进 P1 的抽样健全性检查里，不要假设两份措辞天然等价。
