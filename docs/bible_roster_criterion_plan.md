# 角色圣经「必收角色名单」判据重设计方案

状态：设计草案，待产品负责人拍板。**本文档不含任何代码改动，仓库当前工作树未被本次任务修改。**
调查范围：`app/stages.py`（人物点名/角色圣经生成/别名回填/别名裁决闸）、`app/production/prep_pack.py`（映射台在场判定的既有先例）、
`app/domain/bible_ops.py`（人物谱定稿/影响预检/画风清理）、`app/db.py`（`character_portraits` 表结构）、`app/schemas.py`
（`Character`/`CharacterAlias` 等）、`tests/test_bible_prompt_and_precheck.py`（现有测试覆盖）。

---

## 0. 根因陈述

假阳性（王伯/周员外/靠山老祖进名单却从未真正出场）和假阴性（李富贵该进却缺席）这两个方向相反的错误，
出自同一行代码：`app/stages.py:2087` 的 `hits = window_raw.count(name)`。它把"重要角色"的判据等同于
"这个字符串在原文窗口里出现的次数"——一个纯词法信号。往假阳性方向看，字符串出现不代表这个人真的在
画面里出场，王伯/周员外/靠山老祖的命中全部来自旁白交代身份或他人台词提及，本人从未登场；往假阴性方向
看，这个信号只统计"点名"阶段模型报出的候选名字本身的出现次数，而候选名字取的是模型认为的"名字"，当
原文通篇用绰号称呼一个人（李富贵在窗口内几乎只以"小胖子"出现，"李富贵"三个字仅出现 1 次）而没有一张
可查的别名表把"小胖子"翻译回"李富贵"时——此刻圣经正文还没生成，结构上不可能有别名表——这个人就被
计数机制判定为不重要。两个方向的修复因此指向同一处调整：把判据从"名字字符串出现次数"换成"经过独立
语义裁决确认为本人在场的证据条数"，并且让"名字"这一列本身允许取原文最常用的写法（可以是绰号），不
强制翻译成正式姓名——这样绰号本身就能直接充当"必收货币"，不再需要一张此刻还不存在的别名表来做转译，
顺带绕开了"计数发生在别名表产生之前"这个时序悖论。

---

## 1. 设计约束逐条对照

| CLAUDE.md 约束 | 本方案如何满足 |
|---|---|
| 禁止黑白名单与枚举穷举 | 判据只从本次输入推导：候选人名单来自模型点名（对本书开放），在场证据来自模型自己在原文里找的引句，裁决闸的候选/段号 enum 全部由代码对**本次**输入结构性算出（§3 步骤 2、4），不含任何预先编好的人名/称谓/动作模式表。裁决闸 `verdict` 字段的三态取值（onstage/mentioned_only/uncertain）是判断题结构本身决定的封闭集合（是/否/不确定），不是从业务数据里枚举出的名单，与"称谓/人名必须动态生成 enum"不是同一维度，不冲突。 |
| 模型提名，代码核验 | 模型负责两层语义判断：①点名+自报在场证据（开放语义）；②裁决闸判断某段是否"在场"（开放语义）。代码只做结构性核验：引句是否逐字命中原文、称呼是否是引句子串、段号是否落在裁决闸自己检索出的卷宗范围内。 |
| 不得兜底填充 | 结构闸/裁决闸任一步不通过，该条证据直接丢弃，不计入 `verified_onstage_count`；点名模型没点出的人，本方案不会"沿用上一版名单"或"按上次结果补全"。 |
| 空集合不等于「无需检查」 | 裁决闸卷宗为空 → 直接判该证据不通过（`reason="no_presence_dossier"`），不是跳过检查；`_bible_covers_name` 扩展后对空 `aliases` 列表的 `any()` 检查天然返回 False（未覆盖），不会因列表为空而误判为"已覆盖"。§5 改动清单里对这两处显式标注了要避免"`if known and x not in known`"这类短路陷阱。 |
| 写完整的正面陈述，不写禁令 | §2 给出的新 prompt 全文只写"什么算数、什么不算数、给不出证据时怎么办"，不写"禁止 XX 写法"这种针对具体案例的封堵。 |
| 判据挂产物信号，不挂状态字段 | 判据挂在"通过裁决闸核验的证据条数"这个产物信号上，不读写 `projects.bible_status` 等状态字段。 |
| 单次长调用会漏掉整个类别 | 点名调用如果漏报某个类别的角色（比如整体跳过配角），后端没有能力事后发现——这是本方案承认的已知限制（§8），缓解手段是 prompt 里"宁多勿漏"的显式指导 + `log_provider_call` 记录本轮点名候选数/裁决通过数，供人工从可观测信号里发现"这次点名明显偏少"的异常，而不是静默通过。 |

---

## 2. 新判据的完整定义

### 2.1 数据结构

替换 `app/stages.py:2010-2013` 的 `_CharacterRollCall`：

```python
class _RosterOnstageEvidence(BaseModel):
    chapter_index: int = -1
    quote: str = ""


class _RosterCandidate(BaseModel):
    primary_appellation: str = ""      # 原文最常用称呼，允许绰号/外号/代称
    formal_name: str = ""              # 原文已揭示的正式姓名；未揭示则空串，不得编造
    onstage_evidence: list[_RosterOnstageEvidence] = Field(default_factory=list)


class _CharacterRollCall(BaseModel):
    candidates: list[_RosterCandidate] = Field(default_factory=list)
```

`_recurring_character_names()` 的返回类型从 `list[tuple[str, int]]`（name, hits）改为
`list[tuple[str, str, int]]`（primary_appellation, formal_name, verified_onstage_count），
formal_name 允许为空串。

### 2.2 新常量

```python
BIBLE_RECURRING_MIN_ONSTAGE_QUOTES = 2   # 取代 BIBLE_RECURRING_MIN_HITS，见难点 B
```

`BIBLE_HEAD_CHAPTERS`、`BIBLE_LOOKAHEAD_CHAPTERS`、`BIBLE_MUST_COVER_MAX` 语义不变、数值不变——
窗口范围没有变化，变化的只是窗口内"怎么判定重要"这一件事。`BIBLE_RECURRING_MIN_HITS` 按 CLAUDE.md
"退场功能要一起退场"整体删除，包括所有引用点（`_recurring_character_names` 内部、以及任何测试里
对它的直接引用）。

### 2.3 新版「人物点名」prompt（替换 `app/stages.py:2039-2052`）

```
任务：从下面的小说正文里找出【出场人物】，为每个人物申报能证明他本人真的出现在画面中的证据，不要只给名字。

要求：
1. primary_appellation：原文里称呼这个人物最常用、最稳定的一种写法——可以是正式姓名，也可以是外号、
   绰号、尊称或代称（如"小胖子""许师姐""靠山老祖"），取原文实际出现频率最高的那一种写法，逐字照抄，
   不得改写、简称或补全。
2. formal_name：这个人物在原文中已经明确揭示过的正式姓名；原文尚未揭示正式姓名（只有外号/代称），
   就填空字符串，不要猜测或编造。
3. onstage_evidence：列出若干条能证明这个人物本人真正出现在画面中的原文证据（本人说话、本人动作、
   或被旁白直接叙述为身处此地），每条给 {"chapter_index": int, "quote": str}：
   - chapter_index 取原文分块【】块头里的数字；
   - quote 必须是该章原文的逐字引句（不超过约 80 字），且这句引文里必须能看到 primary_appellation
     或 formal_name 中的至少一个——原文怎么写就怎么抄，不要自己添加或删除引号；
   - 判断依据是这句话本身的叙述位置：这个人物是不是这句话所描述的那个时空里正在行动、正在说话、或
     被旁白直接叙述为置身其中的人，才算在场；如果这句话里正在行动、说话、被叙述置身其中的是别人，
     这个称呼只是作为被谈论、被指涉、被交代来历或状态的对象出现在别人的叙述或话语里，即使字面提到
     了这个称呼，这个人物本人也没有置身在这句话所写的场景中，不算在场，不要拿这种句子当证据；
   - 尽量把你能找到的在场证据都列出来，不要只列一条；确实找不到任何在场证据的人物，onstage_evidence
     给空列表，不要为了凑数编造。
4. 不要输出"少年""女子""老者""两人"这类无法从人群中单独指认出具体是谁的泛称，也不要输出宗门名、
   地名、法宝名。
5. 同一个人只输出一个条目；戏份很少的人也可以列出来，后端会按核验通过的在场证据条数再筛一遍。

小说正文：
{head_text}

输出 JSON Schema：
{"candidates": [{"primary_appellation": str, "formal_name": str, "onstage_evidence": [{"chapter_index": int, "quote": str}]}]}
```

这段"在场"语义（本人说话/动作/被叙述在场 vs 被提及/回忆/转述/背景交代）不是新发明的判据边界，是
`app/production/prep_pack.py:4389-4392`（`_extract_chunk` 的 `segment_indexes` 判据）已经在生产环境
使用的同一条语义边界，此处只是移植到一个新的调用点，用词按本调用的上下文（整章级引句，而不是分段编号）
做了适配。

---

## 3. 新的后端核验流程

```
generate_bible()
 └─ _recurring_character_names(chapters)
     ├─ 步骤 1【模型·点名】：按 §2.3 prompt 调用一次，得到 candidates（每人 primary_appellation +
     │   formal_name + onstage_evidence[]）。调用失败 → 返回空名单，不阻断人物谱本身（沿用既有"失败
     │   兜底"行为，见原 docstring）。
     │
     ├─ 步骤 2【代码·结构闸，逐条证据】：对每个 candidate 的每一条 onstage_evidence：
     │   G1 chapter_index 必须落在本轮统计窗口内（valid[:BIBLE_HEAD_CHAPTERS+BIBLE_LOOKAHEAD_CHAPTERS]
     │      的真实章节序号集合，防止模型编造窗口外的章号）；
     │   G2 quote 必须是该章原文的逐字子串（复用 `_quote_comparison_variants`，处理模型自行加/脱一层
     │      引号的噪音，与别名结构闸同一套工具）；
     │   G3 primary_appellation 或 formal_name（非空的那个）必须是 quote 的子串。
     │   任一条不满足 → 这条证据直接丢弃，不进入步骤 3（省模型调用，也是"不确定不登记"的第一道闸）。
     │
     ├─ 步骤 3【代码检索 + 模型裁决，逐条证据】：结构闸通过的证据才发起裁决：
     │   3a 代码用 `index_source_segments` 在该章原文里定位 quote 所在的自然段，连同前后各 1 段
     │      上下文拼成一份小卷宗（`_roster_presence_dossier`，无先例可查/无候选竞争，不需要别名裁决闸
     │      那套三层保底配额）；定位不到（quote 跨段落边界等极端情况）→ 判该证据不通过
     │      （`reason="no_presence_dossier"`），不是跳过检查。
     │   3b 低温（temperature=0）独立模型调用（`_roster_presence_verdict_call`，新函数，enum 注入参照
     │      `_alias_verdict_call`）：把卷宗原文段落 + 称呼 交给模型，只问"这段文字里，这个称呼指代的
     │      人物本人是不是真的出现在画面中"，输出 `{"verdict": "onstage|mentioned_only|uncertain",
     │      "supporting_segment_index": int}`；`supporting_segment_index` 的合法取值 enum 收紧到卷宗
     │      自己的段号集合。
     │   3c 结构性钉证：`supporting_segment_index` 必须落在卷宗段号集合内——这一步直接复用
     │      `_alias_verdict_pin_segment`（该函数本来就是通用的"段号是否在给定卷宗集合内"判断，不含任何
     │      别名专属逻辑，不需要新写）。
     │   3d `verdict == "onstage"` 且钉证通过 → 计入该角色的 `verified_onstage_count`；
     │      `verdict` 为 `mentioned_only`/`uncertain`，或裁决调用失败，或钉证失败 → 该条证据不计入
     │      （不确定不登记，与别名裁决闸同一安全默认）。
     │
     ├─ 步骤 4【代码·聚合排序】：按 `verified_onstage_count` 降序（同分按 primary_appellation 字典序
     │   打破平局，与原排序惯例一致）排序，取 `verified_onstage_count >= BIBLE_RECURRING_MIN_ONSTAGE_
     │   QUOTES` 的候选，最多保留 `BIBLE_MUST_COVER_MAX` 个，返回
     │   `list[(primary_appellation, formal_name, verified_onstage_count)]`。
     │
     └─ 记账：`log_provider_call` 记录本轮候选总数、通过结构闸的证据总数、通过裁决闸的证据总数——
         供人工从数字上判断"这次点名是不是明显偏少/裁决通过率是不是异常低"（呼应约束 7）。

generate_bible() 主体
 ├─ must_cover_part：改为展示 primary_appellation（+ 已知 formal_name）+ verified_onstage_count；
 │   显式指示模型：formal_name 非空时 character.name 用 formal_name、并把 primary_appellation 登记
 │   为一条 aliases；formal_name 为空时 character.name 直接用 primary_appellation（§4 难点 C）。
 ├─ `_run_with_agent_loop` 产出候选人物谱（不变）。
 ├─ `_verify_character_aliases_in_place`（不变，逻辑复用）。
 ├─ `missing = [item for item in must_cover if not _bible_covers_name(bible, item)]`——
 │   `_bible_covers_name` 扩展为同时匹配 primary_appellation/formal_name 与 bible 角色的 name 及其
 │   已核验 aliases（§4 难点 C）。
 └─ `_supplement_bible_characters`：prompt 与 schema 都新增 aliases 输出（§4 难点 C），append 成功后
    对新增角色单独跑一次别名核验（复用 `_verify_character_aliases_in_place` 内层循环，改造成可传入
    "只核验这些角色"的子集，避免对已核验过的角色重复发起模型调用）。
```

---

## 4. 难点 A–E 逐条结论

### 难点 A：结构闸拦不住伪造的在场证据

**结论：新写一道低温在场裁决闸（§3 步骤 3），不采用"额外申报动作/台词片段"方案。**

评估过的三条路：

1. **要求每条证据额外申报角色在这句里的动作/台词片段，后端核验该片段也逐字在 quote 内。**
   放弃。这道闸在结构上等价于"要求模型从 quote 里再截一段子串"——模型完全可以拿 quote 的任意子串
   （包括"的儿子"这种非动作片段）冒充"动作"，后端没有任何独立于模型自证的手段核验"这个片段真的是
   动作/台词"而不是别的什么词，除非引入一份"合法动作模式"的规则表去识别，而这本身就是 CLAUDE.md
   明确禁止的枚举穷举/黑名单式修复——跟"小胖子→李富贵"映射表是同一形状的缺陷（拿一份人工维护的
   模式表去猜语义）。用王伯的真实案例验证：quote="县城木匠铺王伯的儿子"，模型完全可以把"王伯的儿子"
   本身报成"动作片段"，三条件全过，闸门形同虚设。
2. **新写低温在场裁决闸（选中）。** 与本仓库已经在别名裁决闸上验证过的分工模式同构：模型做语义
   判别，代码做结构钉证。且"在场判断题"与 `app/production/prep_pack.py` 里 `_extract_chunk` 现有
   生产提示词用的是同一条语义边界，不是自创全新判据，是把已经在生产环境跑过的边界移植到新调用点。
3. **复用映射台（prep_pack）未来会产出的"画面出场"段号判定。** 放弃，时序不成立：角色圣经生成发生
   在任何一集的 prep_pack 运行之前，此刻这一章节根本没有 prep_pack 产物可读。

**失败模式（诚实列出，不是零风险方案）：**
- 裁决闸本身仍是语义判断，不是 100% 确定性，可能在边界案例（自由间接引语、同段多人物视角切换）上
  判错——错误率未经真实运行验证。
- 与别名裁决闸"事故2"（王腾飞/王师弟误判）同构的确认偏误风险仍然存在：如果某段落里反复出现该称呼、
  且伴随其它人物的动作描写，模型有可能把"反复出现的名字"误判为"在场"。缓解手段是像别名裁决闸一样
  只给该称呼锚定检索出的段落（不整章塞给模型），但不能保证完全杜绝，这是语义判断固有的残余风险。
- 调用量上升：点名阶段一次调用产出的证据，每一条通过结构闸的都要单独发起一次裁决调用，量级预计与
  别名回填相近（几十次级别）——模型调用免费但仍占用生成人物谱的总耗时，需要实测记录一次真实耗时
  （未验证）。

### 难点 B：阈值重定

**结论：`BIBLE_RECURRING_MIN_ONSTAGE_QUOTES = 2`，作为首刀取值，需要用真实 dry run 校准（P1）。**

依据（基于已查实的原文引句做定性推理，不是运行新流水线得到的实测数字——**这部分标注"未验证"**，
需要实现后跑一次真实 dry run 才能拿到准确的 `verified_onstage_count`）：

| 角色 | 已查实的原文命中形态 | 预期新判据下的 `verified_onstage_count` |
|---|---|---|
| 王伯 | 3 条命中全部是"县城木匠铺王伯的儿子""木匠铺的王伯只有这一子"这类身份/归属交代 | 预期 0——没有一条包含王伯本人的动作或台词 |
| 周员外 | 3 条命中全部是他人台词里的债务交代（"还欠了周员外三两银子"） | 预期 0 |
| 靠山老祖 | 10 条命中全部是"失踪四百余年"式历史背景交代 | 预期 0 |
| 李富贵（primary_appellation="小胖子"） | 原文称他"戏份仅次于男主"，且有胖、梦游、啃斧头留牙印等大量本人细节描写 | 预期远高于阈值，只要点名模型正确识别"小胖子"作为其 primary_appellation |
| 王有材 | 已知首章出场、19 章被交代摔死，未拿到逐条引句 | **未验证**，需要真实 dry run |

**为什么阈值可以比旧的 `BIBLE_RECURRING_MIN_HITS=3` 低：** 旧阈值的"次数够多"本身是在拿"出现频率"
当"不是偶然提及"的代理信号（次数越多，偶然提及的概率直觉上越低）——但王伯/周员外/靠山老祖三个反例
已经证明这个代理信号会整体失效（3 次、3 次、10 次命中可以 100% 是非在场提及）。新判据用裁决闸直接
核验"是不是在场"，不再需要靠"次数多"去间接对抗假阳性噪声，因此阈值的作用从"过滤噪声"变成"过滤
证据单薄、不足以支撑一整套跨镜定妆照的边缘角色"（呼应难点 D），阈值可以设在更低、更贴近语义的量级：
"至少两次独立的、经核验的在场"。

**待办（P1）：** 用 proj_3ac0b627fa46 项目做一次真实 dry run，核对表中五个角色的真实
`verified_onstage_count`；若李富贵拿到的数字明显偏低（比如 < 5，没有安全冗余）或王伯/周员外/靠山
老祖任一个 > 0，回头调整常量或裁决闸提示词。

**`BIBLE_MUST_COVER_MAX=12` 结论：保留数值不变。** 本项目在新判据下预期不会触顶——12 人里 3 个假
阳性归零、1 个假阴性转正，净人数很可能降到 12 以内，不需要新增名额。该常量仍是防止"多主角小说撑爆
生成预算"的安全阀，不建议只凭这一个项目的观测结果改变全局常量；留作 P2 观察项，等未来某个项目真的
观测到它成为瓶颈再评估。

### 难点 C：绰号做主称呼后的连锁反应

**结论：`_bible_covers_name` 扩展为同时匹配已核验别名；补录 prompt 同步新增 aliases 输出。**

1. `app/stages.py:2094-2100` `_bible_covers_name(bible, name)` 改造为接受"待匹配称呼集合"（调用方
   传 `{primary_appellation, formal_name}` 过滤空值后的集合），命中条件除了原有的
   `character.name` 子串关系外，新增：待匹配称呼中任一项与该角色 `character.aliases[].text`
   **精确相等**（不用子串——别名本身已经是核验过的精确称谓，用子串关系反而可能对上不相关的短别名，
   比如单字"老"作为子串命中一堆无关别名；相等判断更安全，且 aliases.text 本身就是逐字原文称谓）。
2. 这个改动生效的前提是"别名已核验完毕"，`generate_bible` 现有调用顺序已经满足这个前提——
   `_verify_character_aliases_in_place` 在 `app/stages.py:4258` 先跑，`missing = [...]`
   在 `4259` 才检查——不需要调整调用顺序。
3. `_supplement_bible_characters`（`app/stages.py:2103-2182`）的 prompt/schema 目前完全不产出
   aliases 字段（`Character` 模型虽然支持 aliases，但补录 prompt 展示给模型的 JSON Schema 里没有
   这个键，模型不会主动补），需要新增：告诉模型"如果给定了正式姓名，character.name 用正式姓名，
   并把前面那个原文最常用的称呼登记为一条 aliases（证据必须是能同时看到这个称呼与这个正式姓名的
   段落，找不到就不要申报这条别名）；如果没有正式姓名，character.name 直接用那个称呼，不需要另外
   申报别名"。
4. 补录成功 append 到 `bible.characters` 后，新增角色声明的 aliases 同样是"申报"，必须过与主生成
   同一套核验（`_alias_evidence_resolution`）才能真正登记——当前 `_supplement_bible_characters`
   完全没有这一步，是本方案新增的一处改动点（不是"顺手复用就好"的范围）：需要把
   `_verify_character_aliases_in_place` 的内层循环抽出为一个接受显式 `characters` 子集的辅助函数，
   `_verify_character_aliases_in_place` 用 `bible.characters` 调用它（行为不变），
   `_supplement_bible_characters` 在 append 之后只对本次新增的角色调用它——避免对已经核验过的角色
   重复发起模型调用。

### 难点 D：王有材该不该收

**结论：不做人工特判，用同一套 `verified_onstage_count` 判据无差别决定他的去留；不新增任何针对
"角色死亡/退场"的判据分支。**

基于任务简介给出的信息做定性推断（**未验证**，需要真实 dry run 核实）：他"第 1 章出场、第 19 章被
交代摔死"——首次出场通常伴随互动，大概率能拿到至少 1 条 onstage 证据；"死讯"大概率是叙述/转述形态
（"死了"这类背景交代），不太可能是本人在场的动作段。据此推断他很可能落在 1 条左右，低于阈值 2，
新判据下不进必收名单。

无论真实数字落在哪一侧，设计立场都一样：
- 若 `verified_onstage_count < 2`，他不进必收名单是正确行为——这类"出场一次就死"的角色不该占用一整
  套多视角定妆照的生成预算，退回现有"群演"（`asset_manifest.functional_extras`，prep_pack 已有的
  处理路径）分类是准确的，不是遗漏。
- 若他因为首章有较多细节描写而拿到 ≥2 条 onstage 证据，按同一套无差别判据他就该进——这也是可以
  接受的结果，判据本身就是用来发现"这个人是否值得单独建卡"，不是我们提前认定他该不该进。
- 明确反对的做法：不为"只出场一次的角色"或"已死亡的角色"单独写一条特判规则。判断"死亡"需要引入
  一整类语义识别，等于又造一张隐性黑名单；且"出场一次"与"死没死"是两件独立的事（有的角色只出场
  一次却活到全书结束，有的角色反复出场很多次最后才死），死亡与否跟"是否值得单独建卡"没有必然因果，
  真正该看的只是"有没有反复被要求单独出镜的在场次数"，这正是 `verified_onstage_count` 已经在测的
  东西。

### 难点 E：已有数据怎么办

proj_3ac0b627fa46 当前已有 12 个角色的定妆照（含 3 个假阳性 + 1 个缺失待补的李富贵），
`character_portraits` 没有任何选择性清理路径——唯一的批量删除 `_purge_for_style_change`
（`app/domain/bible_ops.py:1066-1095`）只在全局画风变更时对整个项目无差别清空；
`_classify_bible_changes`（:1142）把角色增删只归为 `text_changed`，`compute_bible_impact_preview`
里的 `rebuild_images` 计数循环（:1249-1259）只遍历新 bible 的角色，被删角色不产出任何信号或成本
预估。

**重跑范围：** 只需重跑该项目的 `generate_bible`（点名 + 主生成 + 别名核验 + 必收补录），不需要重跑
场景圣经或分集内容——本次改动范围就是 `characters` 列表本身。执行前需要确认当前项目是否有在途的
分镜/发布任务在消费现有 12 人阵容（属于执行前置检查，本方案不展开，交给执行阶段处理；参见
CLAUDE.md"改跨台输出结构须先通知对方"纪律）。

**孤儿行清理办法：**
1. 新 bible 定稿写入成功（`bible_version` 递增）之后，再计算 orphan 集合：
   `(project_id, character_name)` 在旧 `character_portraits` 里存在、但在新 bible 的
   `characters[].name` 及其 `aliases[].text` 里都找不到对应项的行。
2. 删除/退场前必须先列出这份清单人工过目（CLAUDE.md"删除或覆盖前先看目标"）。清理动作是否允许物理
   删除，取决于这些角色名是否已经被任何**已发布**分集的 `asset_manifest.characters`/`appellation_map`
   引用过——**proj_3ac0b627fa46 已经查实：`shots`/`episodes.screenplay_json`/`screenplay_drafts`/
   `storyboard_workspace_state`/`reference_assets`/`visual_entity_merges` 全部 0 行，1616 集全部
   `status=planned`、`screenplay_status=pending`，没有任何已发布分集引用过这些定妆照，本项目物理
   删除是安全的**，不需要走逻辑退场。这个结论只对本项目成立，不能当成通用前提：换一个项目、或未来
   在别的项目上执行同一套清理，必须先重新跑同一类只读引用检查再决定物理删除还是逻辑退场（借用现有
   `ep_end` 字段封顶，不再产生新版本，但保留历史行供已发布分集回溯）——这条检查是 §7 P2 通用清理
   入口的必备设计要求，不能因为本项目查出来是空的就整体省掉。
3. 操作原子性：清理必须在新 bible 已经确认定稿成功之后才执行，不能与 bible 写入共享同一个未提交
   事务；中途失败时旧的 12 行必须原封不动（CLAUDE.md"破坏性操作要有原子性"）。

**是否需要新增清理入口：结论是需要，但不在本次 P0 范围。** P0 只做"新判据本身"；针对这个项目，用
一次性脚本（放 `scripts/`，只读列清单 + 人工确认后再执行删除/退场，不做成自动化入口）手工执行一次
（P1）。如果未来发现"角色从圣经里被移除"会反复出现（不只是这次判据修复触发的一次性问题），再补上
一个正式的、带二次确认与已发布分集引用检查的通用清理入口（P2）。

**李富贵缺失的补齐：** 走新判据下的 `generate_bible` 重跑（借助 `previous_bible` 对照上下文避免推翻
已经正确的其余角色外观定稿），新增角色走既有的 `compute_refs_cost_precheck` 触发定妆照生成，不需要
新代码。需要提醒：李富贵重新入谱后的外观定妆照可能与"另有任务处理"的特征漂移缺陷（王有材身上被安上
了本属于李富贵的"胖、梦游、啃斧头留牙印"特征）产生交互，若那个任务与本方案的执行窗口重叠，需要协调
先后顺序，避免相互覆盖对方的修复结果——这不是本方案职责范围，只做提示。

---

## 5. 改动清单

全部改动都在 `app/stages.py`（人物谱台），涉及测试改动在 `tests/test_bible_prompt_and_precheck.py`。
不改 `app/production/prep_pack.py`（只是参考其既有语义边界，不改动它本身）。

| 位置 | 改什么 |
|---|---|
| `app/stages.py:1924-1927` | 新增 `BIBLE_RECURRING_MIN_ONSTAGE_QUOTES = 2`；删除 `BIBLE_RECURRING_MIN_HITS` 及其全部引用（退场功能一起退场）。`BIBLE_HEAD_CHAPTERS`/`BIBLE_LOOKAHEAD_CHAPTERS`/`BIBLE_MUST_COVER_MAX` 不变。 |
| `app/stages.py:2010-2013` | `_CharacterRollCall` 替换为 §2.1 的三个模型（`_RosterOnstageEvidence`/`_RosterCandidate`/`_CharacterRollCall`）。 |
| `app/stages.py:2022-2091` | `_recurring_character_names()` 按 §3 步骤 1-4 重写：新 prompt（§2.3）、结构闸（G1-G3）、裁决闸调用与聚合、返回类型改为 `list[tuple[str, str, int]]`。 |
| `app/stages.py`（新函数，紧邻别名裁决闸代码之后，复用其工具） | 新增 `_roster_presence_dossier`（定位 quote 所在段 + 前后各 1 段上下文）、`_RosterPresenceVerdictResponse`（Pydantic，`verdict`/`supporting_segment_index` 两个 enum 注入字段）、`_roster_presence_verdict_call`（低温模型调用，参照 `_alias_verdict_call` 的 enum 注入与 `chat_structured` 调用惯例）。段号钉证直接复用现成的 `_alias_verdict_pin_segment`，不新写。 |
| `app/stages.py:2094-2100` | `_bible_covers_name` 签名从单一 `name: str` 扩展为接受一组待匹配称呼，新增对 `character.aliases[].text` 的精确匹配分支（§4 难点 C）。 |
| `app/stages.py:2103-2182` | `_supplement_bible_characters`：prompt/schema 新增 aliases 输出（§4 难点 C 第 3 点原文）；append 成功后对新增角色调用别名核验（§4 难点 C 第 4 点，需要先把 `_verify_character_aliases_in_place` 内层循环抽出为可传子集的辅助函数）。 |
| `app/stages.py:3140-3175` | `_verify_character_aliases_in_place`：内层循环抽出为辅助函数（供 `_supplement_bible_characters` 复用），外层行为不变。 |
| `app/stages.py:4147-4273` | `generate_bible()`：`must_cover_part` 拼装文本按 §3 更新（展示 primary_appellation/formal_name/verified_onstage_count，指示 aliases 登记规则）；`missing = [...]` 的判断调用改用新版 `_bible_covers_name`。 |

---

## 6. 测试方案

### 6.1 现有测试会红

- `tests/test_bible_prompt_and_precheck.py::test_recurring_character_names_ranks_by_lookahead_occurrences`
  （216-241）——`fake_chat` 返回旧的 `{"names": [...]}` 结构，且断言 `ranked` 是 `(name, hits)` 二元组，
  接口签名变化后必红，需要整条重写：`fake_chat` 改造成按 `stage_key` 分流（点名 vs 裁决闸两次不同的
  模型调用），裁决闸部分用 mock 固定返回 `verdict="onstage"` 等。
- `tests/test_bible_prompt_and_precheck.py::test_generate_bible_keeps_source_in_repair_rounds_and_supplements`
  （244-314）——`fake_chat` 的 `"character_roll_call"` 分支返回值要改成新结构；断言部分
  （"许师姐" in prompt 等）需要相应改成检查新版 `must_cover_part` 文本格式；还需要新增对裁决闸调用的
  mock（否则该测试原有的"孟浩/王有材/许师姐"三人会因为没有可核验的 onstage_evidence 而全部拿到
  `verified_onstage_count=0`，无法复现原测试想验证的"必收名单缺人时触发补录"场景）。
- `tests/test_bible_prompt_and_precheck.py::test_generate_bible_prompt_explains_bridging_chapter_for_aliases`
  （317-347）——大概率不受影响（fixture 只有一章、不会触发 `must_cover` 非空分支，`must_cover_part`
  应保持空字符串），但接口变化后必须重新跑一遍确认，不能假设不受影响。

### 6.2 需要新增的用例

- **结构闸单测**：quote 不在该章原文里 → 拒绝；appellation 既不在 quote 里也不是其子串 → 拒绝；
  chapter_index 落在统计窗口之外 → 拒绝。
- **裁决闸单测**（mock 模型调用）：`verdict="mentioned_only"` → 不计入 `verified_onstage_count`；
  `verdict="onstage"` 且段号钉证通过 → 计入；`verdict="onstage"` 但 `supporting_segment_index`
  不在卷宗段号集合内（模拟模型编造段号）→ 钉证失败，不计入。
- **`_bible_covers_name` 新逻辑单测**：must_cover 条目是"小胖子"，bible 里角色是
  `name="李富贵"` + `aliases` 含 `text="小胖子"` → 判定为已覆盖，不触发补录（难点 C 的直接验收）。
- **本项目真实数据验收测试（任务要求的重点）**：用已经查实的真实引句构造 fake chapters/fake 模型
  响应——王伯的"县城木匠铺王伯的儿子"等 3 条、周员外的"还欠了周员外三两银子"等 3 条、靠山老祖的
  "失踪四百余年"式引句样本、以及李富贵以"小胖子"为 primary_appellation 的若干条在场引句（需要在
  实现阶段回到原文核实几条真实引句用于测试，本文档尚未逐字摘录）。断言：
  - 王伯/周员外/靠山老祖：结构闸或裁决闸（mock 判 `mentioned_only`）全部挡下 →
    `verified_onstage_count == 0` → 不进 `must_cover`；
  - 李富贯：多数证据通过（mock 判 `onstage`）→ `verified_onstage_count >= 2` → 进 `must_cover`。
  **诚实说明**：这类测试里裁决闸部分必须 mock 模型响应，mock 的返回值本身是测试作者对这几条引句的
  人工研判结论（例如"县城木匠铺王伯的儿子"应该被判 `mentioned_only`），验证的是"结构闸+聚合逻辑在
  给定裁决结果下算得对不对"，不是"真的调用模型会不会判对"。后者需要另外做一次不 mock 的真实集成
  dry run，不适合放进自动化单元测试套件，属于 P0 清单里"实现后的手工验收步骤"（见 §7）。

---

## 7. P0/P1/P2 分级

**P0（本次必须做）：**
- 新点名 prompt + 新响应结构（§2.1、§2.3）
- 结构闸（G1-G3，§3 步骤 2）
- 新裁决闸（§3 步骤 3，独立模型调用 + 段号钉证）
- 新常量 `BIBLE_RECURRING_MIN_ONSTAGE_QUOTES`，废止 `BIBLE_RECURRING_MIN_HITS` 及其全部引用
- `_bible_covers_name` 扩展为同时匹配已核验 aliases（难点 C）
- `_supplement_bible_characters` prompt/schema 更新 + 新增角色的别名核验补跑（难点 C）
- `generate_bible` 里 `must_cover_part` 拼装文本更新
- §6.1 三个受影响现有测试的更新/重写
- §6.2 结构闸/裁决闸单测 + `_bible_covers_name` 新逻辑单测 + 用真实项目引句做的验收测试（mock 模型响应）
- 针对 proj_3ac0b627fa46 项目手工执行一次真实 dry run（非自动化测试，是实现后的验收步骤），核对
  李富贵进、王伯/周员外/靠山老祖不进，并记录真实的 `verified_onstage_count` 数字，供 P1 校准阈值用

**P1（重要但不阻塞新判据本身上线）：**
- 针对该项目的实际重跑 + 孤儿行人工复核清单 + 一次性清理脚本（难点 E 的落地执行；建议放在新判据
  至少一次成功 dry run 验证之后再对生产数据执行，不要边改代码边动生产数据）
- `BIBLE_RECURRING_MIN_ONSTAGE_QUOTES=2` 的真实数据校准（用 dry run 结果决定是否要调到 1 或 3）
- 王有材真实 `verified_onstage_count` 的复核（目前只有基于任务简介文字的推断，没有逐字核实原文引句）

**P2（本次不做，明确排除）：**
- 正式的、带二次确认 + 已发布分集引用检查的通用"角色移除清理"入口（本次只做一次性脚本，不是给所有
  未来项目复用的机制）
- `BIBLE_MUST_COVER_MAX` 常量本身是否需要随新判据调整——本次没有观察到需要调的证据，留给以后有更多
  项目数据积累后再评估
- 裁决闸对"自由间接引语/同段多视角切换"这类边界案例的进一步加固——目前没有真实回归证据表明这是个
  问题，等出现真实误判案例再按需修，不提前穷举所有可能的误判模式

---

## 8. 风险与已知限制

- **假阳性方向**：裁决闸仍可能被确认偏误击穿，与别名裁决闸"事故 2"（王腾飞/王师弟误判）同构——
  主角频繁出现的段落里，其他人被提及时可能被误判为在场。目前没有专门测试覆盖这类边界，需要真实
  回归观察（未验证）。
- **假阴性方向（新的、独立于旧判据的来源）**：如果点名模型本身没能正确识别某角色的
  `primary_appellation`（没意识到"小胖子"指代李富贵，漏报或报了别的称呼），或者压根没在候选列表里
  点出这个人，结构闸/裁决闸都无从核验——这是"点名"阶段本身的语义能力上限，本方案没有结构性手段保证
  100% 召回，只能靠 prompt 里的"宁多勿漏"指导降低概率。这与旧判据的假阴性根因（字面出现次数太少）
  不同，是本方案引入的一个新的、需要在后续回归里单独观察的假阴性来源。
- **调用量与耗时**：点名从 1 次模型调用变成"1 次点名 + N 次裁决"（N = 通过结构闸的证据条数，预计
  几十次级别），生成人物谱总耗时会上升。模型调用免费但仍占用时间，目前没有实测记录一次真实耗时
  （未验证）。
- **实现细节风险**：`_supplement_bible_characters` 新增角色的别名核验补跑，若实现时图省事对全量
  `bible.characters` 重新跑一次核验（而不是只对新增子集），会产生大量重复的模型调用——浪费但不算
  逻辑错误，需要在代码评审时确认这一点被正确处理为"只核验新增子集"。
- **难点 E 清理方式已按项目查实，但结论不可跨项目复用**：proj_3ac0b627fa46 已确认 3 个假阳性角色的
  `character_portraits` 行从未被任何已发布分集引用（`shots`/`episodes.screenplay_json`/
  `screenplay_drafts`/`storyboard_workspace_state`/`reference_assets`/`visual_entity_merges` 全部
  0 行，1616 集全部 `status=planned`/`screenplay_status=pending`），本项目可以直接物理删除。这个
  "已发布分集是否引用"的只读检查仍然是通用清理入口（§7 P2）的必备步骤——别的项目不一定是空的，不能
  把本项目查实的结论当成所有项目都安全物理删除的默认前提。
- **难点 B/D 的具体数字均为定性推理，不是实测**：王伯/周员外/靠山老祖的"预期 0"是基于已给出的
  引句原文内容做的合理推断（这些引句本身已经过用户核实），可信度较高；李富贵的"预期远高于阈值"与
  王有材的具体数字都需要真实 dry run 才能给出准确结论，本文档已逐处标注"未验证"。
