# 人物认知层设计：状态事实、指代链与判别式提问（架构设计）

日期：2026-08-25（首次定稿）；**二次修订：2026-08-25（同日，用户重新定义认知层范围后）**。
状态：**设计文档，只记录决策与数据结构，不包含任何代码/数据改动**。所有代码引用均逐条
`grep` 核对至当前工作树（`main` 分支，写作本次修订时 HEAD 为 `92c9e7a`；另有 agent 正在
并行修改 `app/stages.py`/`app/schemas.py`，行号会继续变动），行号如与未来改动后的文件不
一致，以 `grep -n` 复核结果为准。

**二次修订说明**：本文档 §1-§12 记录的"状态事实（归属/关系）+ 章级认知卡 + 候选判别裁决闸
注入"设计已经**完整实施**（`app/schemas.py` 的 `CharacterAffiliation`/`CharacterRelation`，
`app/stages.py` 的 `backfill_character_status_facts`/`build_chapter_cognition_card`/
`_alias_verdict_call` 注入，commit `641bf59`/`9f2c773`/`92c9e7a`），§1-§12 原样保留作为
**第一期**的设计记录与实施记账，不再改动其历史叙述。但协调层复核确认：第一期做的是"人物属性
百科"（某人属于某宗、某人与某人是什么关系），用户重新定义的认知层核心不是这个——是"阅读
记忆"：跟着文本走、记住同一个人先后被怎么指称，见 §0.5。本次修订**新增 §13-§18**，覆盖
指代链、场景回指、结构化外表三块新范围，不推翻、不删除第一期已实施的内容，两期并存、职责
不同（§13.0/§13.4 给出边界）。

本文档是 `docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md`（下称"身份文档"，已实施：视觉实体与命名
权威解耦，`Character.aliases` 已落库）的**后继层**。身份文档解决"这是谁、该用哪张脸"；本文档
解决"这个人现在是什么状态、值不值得投入资源"（第一期，§1-§12）与"全书范围内，这个人先后被
怎么指称、指称之间如何相连"（第二期，§13）。三者共用同一套证据锚点纪律，互不重复。

## 0. 一句话问题

原著读者推进到新章节时，脑子里带着"李诗琪是血妖宗的、王有材也是血妖宗的"这类关系性知识，据此
瞬间判断"王师弟"说的是王有材而不是同姓的王腾飞。当前系统没有对应的长期记忆——每次判别都只能
看当前这一章节的文本，看不到几十章之前建立、几十章之后仍然生效的归属/关系事实。

## 0.5 范围重新定位（2026-08-25 二次修订）：从属性百科到阅读记忆

用户原话（2026-08-25）："我理解的认知层，是模仿人类阅读小说后记住的各种情节和因果链，每个人
的特点，人物关系演进……所以按道理认知层看了二十章了，肯定是能记住第一章这个胖子就是李富贵"；
随后确认范围："先剔除情绪，重点在情节因果和人物关系，但是人物外表特点还是可以加上的"。

**协调层复核结论**：§0 的一句话问题、§1-§9 的三层设计，做的是"人物属性百科"——某人属于某宗、
某人与某人是什么关系，各带证据锚点与有效区间。这是合法且已经上线的能力（§4，第一期），但不是
用户说的认知层核心。用户要的是**阅读记忆**：跟着文本走，记住同一个人在全书先后被怎么指称，
使得"看了二十章"之后能反过来认出"第一章这个胖子就是李富贵"。这是两件不同的事——属性百科回答
"这个人是什么样的人"，阅读记忆回答"这处提到的是不是同一个人"。

**本次纳入**：①情节因果／场景回指；②人物关系演进（第一期已覆盖，第二期不重做）；③人物外表
特点。**明确排除**：情绪演进、性格特点——对当前画面一致性（认知层最终服务的仍是生产侧判断：
该不该复用同一张脸、该不该发角色卡、判别时该选哪个候选）无直接作用，且比情节/关系更难给出
可机械核验的证据锚点（"这个人当时很愤怒"没有可逐字命中的客观标准），本次不做，理由与判据见
§16。

**召回 vs 判别——本文档一直缺失、现在必须澄清的一条边界**：

- **判别**：候选集已经确定的前提下，从候选中选出正确的一个。第一期的认知卡（§4.2/§4.3）做的
  是这件事——`_alias_verdict_call`（`app/stages.py:3944`）在已经拿到候选集后，用归属/关系
  背景帮模型在候选之间选。
- **召回**：某处文本出现一处指代，该不该把某个角色纳入候选集。第一期的候选集完全由
  `_alias_verdict_candidates`（`app/stages.py:3910`）决定，判据是"角色规范名或其任一已
  登记别名在章节原文里逐字子串命中"——纯字符串匹配。描述性指代（"白白净净身子较胖"）不是
  任何角色的规范名或已登记别名，永远不会被召回进候选集，候选集里根本没有李富贵，判别环节
  写得再好也无济于事。

第一期的"三层设计"标题叫"三层设计"，实际只做了判别这一半，从未处理召回；这不是实现疏漏，是
当时的设计范围本来就没有把"某处未登记表述该不该指向某个人"当成问题——§1.3 的缺口描述也是
"跨章关系性知识如何注入判别"，用词已经预设了候选集是给定的。本次 §13 补的正是召回这一半，
不改判别逻辑本身（§13.4 有完整的边界说明）。

## 1. 问题与真实事故

### 1.1 触发事故：`王腾飞←王师弟`（第 189 章）

原文（第 189 章，血妖宗李诗琪台词）：「虽然你那顶帽子很让人厌烦，但看在王师弟的份上，我血妖宗
也算一个，倒要看看今日，谁敢动你。」——"王师弟"指同属血妖宗的**王有材**（该章已站到孟浩一边）；
**王腾飞**是同章与孟浩敌对、持刀的另一人，二者只是同姓。人能判对是因为知道"李诗琪血妖宗、王有材
血妖宗"——这是**关系性知识**，人物谱的名字表里没有这一项。姓氏类称谓（X 师兄/X 师弟/X 道友）在
中文修仙小说里极常见，这是一类问题，不是个案。

### 1.2 这个事故已经在层一（别名）触发了一次修复，而且刚刚落地

`app/stages.py` 的人物别名回填链路（`backfill_character_aliases`，§4.1 起）今晚（2026-08-24
23:40 起）经历了完整的真实测试与两轮修复，`logs/backfill_character_aliases.log` 完整记录了
过程，最后一次修复已提交（`git log -1 -- app/stages.py` → `7959b48 fix(bible): 别名裁决改候选
判别 + 段号钉证，拦下两例真实误登记`，2026-08-25 01:41:54）：

1. **第一版**（同章共现闸，`_alias_declaration_verified` 条件 3）：只要别名文本与角色规范名在
   同一章出现即通过——过滤力对主角类角色接近零，`日志 00:47:47` 实测显示"王师弟"被**误判为
   王腾飞的别名并写入内存**（`王腾飞：2 条别名，保留 2 条`，含"王师弟"）。
2. **第二版**（裁决闸但候选证据仍收窄，即本文档任务书所述"今晚实测证明"的那次尝试）：把是非题
   （"是不是王腾飞本人"）改成候选判别题，但卷宗检索范围仍只按"离被测别名最近"选段，正确候选
   （王有材）的证据段落因为不在"王腾飞"附近而选不进卷宗——模型看不到王有材的材料，选择题名存
   实亡，同一输入在 `00:38`/`00:42`/`00:44`/`00:46` 四次复核间于 `verdict_uncertain` 与保留
   之间摇摆，非确定性证据的直接体现。
3. **第三版（已落地，`app/stages.py:3903-3914`）**：裁决卷宗的证据锚点扩大为"该章全部候选人
   （`_alias_verdict_candidates` 结构性算出）的规范名与已登记别名"，不再只锚定被测角色一人。
   `2026-08-25 01:34` 起复核结果转为确定性的 `candidate_mismatch`/`candidate_uncertain`，
   最终 `01:39:22` 写库（`bible_version 150→151`）后，"王师弟"从王腾飞名下**正确移除**；
   同批被拦下的还有 `孟浩←虎爷爷`（第 3 章，`candidate_uncertain`）与 `丹鬼←靠山老祖`
   （第 70 章，`candidate_uncertain`）。三条实证均可在
   `logs/backfill_character_aliases.log:518-559`（`dry_run=False` 的最终复核并写库，
   `bible_version 150→151`）核对：`虎爷爷`（526-529，`candidate_uncertain`）、`王师弟`
   （542-544，`candidate_mismatch`）、`靠山老祖`（552-553，`candidate_uncertain`）三条同批
   全部移除。

### 1.3 已经修好的和仍然没有的

层一裁决闸现在**能正确拒绝错误归属**（"王师弟≠王腾飞"三次复核全部确定性判别为
`candidate_mismatch`）——这是安全网，已经生效。但整场测试里，"王师弟"**从未被正确登记为王有材
的别名**：`backfill_character_aliases` 全程只申报过"character_name=王腾飞, text=王师弟"这一种
组合，从未申报"character_name=王有材, text=王师弟"。回看 §1.1，人类判断依赖的是"李诗琪与王有材
同属血妖宗"这条关系事实——裁决闸的卷宗检索范围被有意限定在**触发本次判别的这一章本身**
（`_alias_verdict_dossier`，`app/stages.py:3610` 文档字符串：「只从已定位到的这一章本身取证，
不整章塞给模型」），
如果"血妖宗"归属是在别的章节建立、第 189 章本身没有重复交代，裁决闸看不见，模型只能猜。

**这正是本文档要补的缺口**：层一的裁决闸解决了"同章证据不足以支撑错误归属"（拒绝假阳性），
但没有、也不该在那个范围内解决"跨章关系性知识如何注入判别"（帮助命中真阳性）——后者需要一份
独立于单章原文、可以跨章节持续生效的结构化状态记忆，即本文档的**认知层**。

### 1.4 决定性真实案例：EP1 李富贵——描述性指代与显式回指（本次二次修订的直接触发点）

三条章节原文（`data/manju.db` 的 `chapters` 表，`project_id=proj_3ac0b627fa46`，只读查询
核对，未做任何写操作）：

- **第 1 章**：「目光落在王有材身上时，看到了他身边的两个少年，一个是那虎头虎脑的家伙，
  另一个则是**白白净净身子较胖**，这二人此刻都身子颤抖，神色恐惧」——四个少年一起被抓上
  靠山宗，这个胖少年**全章无名、无任何已登记别名**，只有一句纯外貌描写。
- **第 3 章**：「他凶狠的看了**孟浩与还在睡觉的小胖子**一眼」——"小胖子"首次出现，与孟浩
  同住一屋，但这一处本身不构成与第 1 章"白白净净身子较胖"的字面连接（没有共同的逐字子串）。
- **第 10 章**：「"**小胖子**、王有材、还有那虎头虎脑的少年，**当初我们四人被一起带上
  靠山宗**，不知此刻他们怎样。"孟浩沉吟片刻」——显式回指第 1 章那一幕：点名"小胖子"是当初
  一起被带上靠山宗的四人之一，而第 1 章那一幕里符合"和王有材、虎头虎脑少年一起被抓、还有
  第四人"这个描述的只有那个"白白净净身子较胖"的无名少年。

读者据此把整条链焊死：白白净净身子较胖（第 1 章，无名）= 小胖子（第 3 章起）= 李富贵（真名，
经既有别名机制核验落库）。现有属性百科（§4）没有任何字段能表达"第 1 章这处无名外貌描写和第
3 章'小胖子'是同一个人"——`Character.aliases`（`app/schemas.py:189`）只收"称谓"，第 1 章
这处是纯描述，不是称谓，从未有机会成为一条别名；`affiliations`/`relations`（本文档第一期
新增，`app/schemas.py:194-195`）记录的是归属和对人关系，同样不是"同一人先后怎么被指称"
这件事。

**已实测证明扩大候选集会产生真实误绑**（`app/production/prep_pack.py:478-497`，
`PREP_PACK_VERSION` 1.8.3→1.8.4 完整回退记录）：1.8.3 尝试把"人物谱登记显示在本集活跃"的
角色也补进候选集（乙类候选），意图正是解决李富贵第 1 章无名的问题；真实落库结果是 EP1/EP2 的
"绿袍男子"被误绑给了**赵武刚**——赵武刚在这两章原文里一次都没被提到，纯粹是"人物谱说他在本集
活跃"这条弱证据被裁决闸误当成了绑定依据。1.8.4 完整回退了这处改动，回退说明（同文件
483-493 行）明确写道："李富贵在 EP1 的真实需求……不属于本集候选判别的职责——那是跨章推理，
本节的候选与证据检索范围本来就限定在本集 source_text 内……不该靠扩大候选集在本集内强行凑出
一个原理上不存在的答案"。

**根因是"证据存在"不等于"证据支持结论"**——人物谱登记的 `ep_start`/外貌描述是关于这个角色的
真实证据，但证不出"本集这处未解析的标签就是他"；扩大候选集只是把弱证据错当强证据用。这正是
本案例揭示的结构性缺口：**李富贵第 1 章的身份不该在"本集候选判别"这个范围内解决**（1.8.4
回退结论正确），但也不能不解决——它需要一种新的、明确限定在"显式回指"这一种硬证据下的跨章
判别机制，范围与本集候选判别互不重叠、不共用同一道闸门。这就是 §13 指代链要解决的问题。

## 2. 与身份三层的关系（边界，不重复造轮子）

| 身份文档已解决 | 本文档要解决 |
|---|---|
| 这是谁（命名权威 `authority_id`）、该用哪张脸（`visual_entity_id`） | 这个人现在的归属/关系是什么、值不值得投入资源做角色卡 |
| `Character.aliases`：别名恒真，一次核验永久生效 | `Character.affiliations`/`relations`：状态事实随章节变化，需要"截至第 N 章"的区间查询 |
| 层三 `display_appellation`：本集只显示本集措辞（剧透纪律） | 认知卡只服务生产侧判断，同样不得泄露给观众（本文档延续同一条纪律，不新开口子） |

**不重复的边界**：
- 身份恒真事实（小胖子=李富贵）用别名机制已经足够，别名回填是**全书范围**扫描
  （`ALIAS_BACKFILL_SOURCE_BUDGET_CHARS=150000`，`app/stages.py:3432`；本项目全书 1616 章，
  见 `logs/backfill_character_aliases.log:3`「章节数: 1616」），本层不重复造一套"全书扫描找
  别名"的机制。
- 裁决庭范式（代码检索卷宗 → 候选判别 → 段号/enum 钉证）已经在层一验证过三次真实事故
  （§1.2），本层**复用同一范式**，不新造一套语义判断机制——差异只在"卷宗证据的来源"：层一
  的卷宗是单章原文段落，本层新增的是跨章持久化的结构化状态事实。
- 视觉稳定（`visual_entity_id`）与显示措辞（`display_appellation`）的语义边界已在身份文档
  §4.3 定稿，本层不改动，只在 §6 说明认知卡与它们各自服务于不同判断。

## 3. 三类事实的时间语义

这是本设计的核心，三类事实的查询方式完全不同，混用会导致过期数据被当作当前状态使用：

### 3.1 身份事实——恒真，不设时间限制

例：小胖子=李富贵。一旦核验通过，对全书任何章节都成立。身份文档的 `Character.aliases`
（`app/schemas.py:112-126`）已经是这个语义，且回填扫描范围已是全书 1616 章（§2），**此项已
超额满足，本层不重复造**。

### 3.2 状态事实——带证据锚点 + 有效区间，按"截至第 N 章"查询

例：王腾飞早期靠山宗、后期南域与孟浩敌对。存单值必然过期——用后期的敌对身份去描写早期在场，
或用早期的宗门归属去解读后期台词，都是同一类错误。查询方式类比 `character_portraits` 表已有的
`ep_start`/`ep_end` 区间惯例（表定义 `app/db.py:348-362`；查询侧 `app/portraits.py:10032-10034`
`portrait_for_episode`：`"ep_start<=? AND (ep_end IS NULL OR ep_end>=?) ORDER BY ep_start
DESC LIMIT 1"`）——本层的归属/关系事实同样"区间开放、`valid_to` 为空代表持续到当前已知范围仍
生效"，不是新发明的查询模式，是同一惯例在人物属性上的复用。

### 3.3 重要性判断——天然前瞻，需要 N+K（K≥10）

例：判断一个未具名角色值不值得做角色卡，必须看他接下来的戏份密度，看当前及以前的文本永远不够。
这个前瞻窗口**已经存在**，本层不新造常量，直接复用：

- `IDENTITY_DISCOVERY_FORWARD_CHAPTERS = 10`（`app/portraits.py:404`）：现有"新角色发现"流程
  里，本集源章节之后再看 10 章，**仅用于姓名消歧**（`_future_chapter_context`，
  `app/portraits.py:473-509`："大汉/老者/黑衣人后来叫什么"），不作为剧情素材传入剧本生成。
- `CHARACTER_IMPORTANCE_FORWARD_CHAPTERS = 20`（`app/portraits.py:405`）：同一流程里，本集
  源章节之后再看 20 章，用于**判断戏份是否够格建卡**（`_forward_fragments`，
  `app/portraits.py:456-470`）。

两个常量服务不同问题（消歧 vs. 重要性），当前只在"剧本里出现、人物谱里没有的具名角色"这一
反应式分支里生效（`app/portraits.py:398-402` 注释：「人物谱只在进项目时谱写一次；之后由剧本
阶段触发——剧本里出现、人物谱里没有的名字，向后检索若干章原文判断戏份」）。**这个反应式机制
本身没有问题，缺口在于它只覆盖"具名角色"，未具名/功能性角色从未进入这条判断路径**（身份文档
§2.5 已证：`functional_extras` 从未进入查图路径；同理，它也从未进入这条重要性判断路径）——
这正是 §7 的关键收益要补的那道口子。

## 4. 三层设计（第一期：状态事实与判别式提问，**已完整实施**）

**实施记账**（写作本次二次修订时的工作树状态，`grep -n` 核对）：`app/schemas.py` 的
`CharacterAffiliation`（129 行起）/`CharacterRelation`（160 行起）/`Character.
affiliations`（194 行）/`Character.relations`（195 行）已落地（另一 agent 正在此文件
并行修改 `valid_from_is_fallback`/`valid_to_is_fallback` 相关逻辑，见 §14.1，行号会继续
变动）；`app/stages.py` 的 `backfill_character_status_facts`（4810 行起）、
`build_chapter_cognition_card`/`ChapterCognitionCard`/`ChapterCognitionEntry`
（3601/3617/3669 行起）、`_alias_verdict_call` 的认知卡注入（3944 行起）均已实现并接入。
下述 §4.1-§4.3 的代码示例是**设计时的示意代码**，与当前实现在字段细节上有出入（如
schema 实际多出两个回落标注布尔位，用于修复 §14.1 记录的"状态事实回填 100% 拒绝"事故），
示意代码保留作为设计意图记录，不再逐字同步，实际实现以 `grep -n` 现场核对为准。

### 4.1 层 A · 档案增维（数据字典）

**新增结构**（`app/schemas.py`，紧邻 `CharacterAlias` 之后、`Character` 之前，与身份文档
§4.1 的 `CharacterAlias` 同构，复用同一套 Pydantic 校验风格）：

```python
class CharacterAffiliation(BaseModel):
    """一条阵营/宗门归属证据：模型申报 + 代码核验后才允许落库（不确定不登记）。
    与 CharacterAlias 的区别：这是状态事实（有效区间），不是恒真事实——需要
    valid_from_chapter/valid_to_chapter 支持"截至第 N 章"查询（见 §3.2）。"""

    org: str                        # 归属对象逐字文本（宗门/阵营/势力名，如"血妖宗""靠山宗"）
    relation_kind: str              # 归属性质自由文本（如 membership/allegiance/hostility），
                                     # 不设 Literal 枚举——与 CharacterAlias.name_kind 同一
                                     # 宽松校验风格，避免模型申报值卡在硬枚举上被整条拒绝
    evidence_chapter_index: int     # 证据锚点：原著章节序号
    evidence_quote: str             # 证据锚点：逐字引句，核验规则与 CharacterAlias 完全一致
                                     # （逐字子串命中 + 角色本人在同段/同章共现）
    valid_from_chapter: int         # 有效区间起点（含）；未申报时代码回退为 evidence_chapter_index
    valid_to_chapter: int | None = None   # 有效区间终点（含）；None=尚无证据表明已失效


class CharacterRelation(BaseModel):
    """一条对人关系证据（与既有 Relationship 不同：Relationship 是无证据锚点的静态叙事关系，
    供人物谱正文可读性使用；这是有证据锚点 + 有效区间的结构化状态事实，供 §4.3 判别式提问使用，
    两者并存、互不替代，与 aliases 新增时"不改写既有字段"的纪律一致）。"""

    to: str                         # 关系对象：人物谱规范名（必须是 bible.characters 中已有的名字）
    relation_kind: str              # 关系性质自由文本（如 ally/rival/hostile/master_disciple）
    evidence_chapter_index: int
    evidence_quote: str
    valid_from_chapter: int
    valid_to_chapter: int | None = None


class Character(BaseModel):
    name: str
    role: str
    appearance_canonical: str
    personality: str = ""
    speech_style: str = ""
    relationships: list[Relationship] = Field(default_factory=list)   # 不变：静态叙事关系
    ref_image_path: str | None = None
    portrait_prompt_override: str | None = None
    aliases: list[CharacterAlias] = Field(default_factory=list)               # 不变（身份文档已落地）
    affiliations: list[CharacterAffiliation] = Field(default_factory=list)    # 新增（本层）
    relations: list[CharacterRelation] = Field(default_factory=list)          # 新增（本层）
```

**可确定性推导的不入库**：姓氏、称谓惯例（"X 师兄"这类形态本身）由名字与已登记关系在查询时
现算，不作为字段存储——存冗余早晚不一致，且直接违反 CLAUDE.md 的黑白名单禁令（任何"称谓后缀
→ 姓氏 → 关系"的映射表都是变相名单）。`org`/`relation_kind` 只存**证据里明确写出的归属/关系
本身**，不存从姓氏、称谓形态推导出的猜测。

**证据来源与核验管线**：新增窄口径回填函数（`app/stages.py`，与 `backfill_character_aliases`
并列同构，复用同一套已经过三次真实事故验证的管线）：

- 证据核验三闸完全复用（不重新实现）：`_alias_declaration_verified` 的判据模式（逐字子串命中
  + 同段/同章共现）、`_find_alias_bridge_chapter` 的全书桥接检索、`_alias_verdict_dossier` +
  `_alias_verdict_call` + `_alias_verdict_candidates` 的候选判别裁决（§1.2 第三版修复，已用
  三条真实事故验证）。**实现纪律**：这几个函数当前的参数与文档字符串是按"别名"语境写的
  （`text`/`true_name` 等命名），归属/关系回填直接调用而非复制粘贴——若参数名需要泛化（如
  `claim_text` 替代 `text`），属于实现期的重命名重构，不改变判据逻辑本身，符合"优先修改现有
  代码"的项目纪律。
- 候选集额外要求：归属/关系的裁决闸候选集**必须包含关系对象本人**（`CharacterRelation.to`）
  或**归属组织的其它已知成员**（若已有归属记录），否则退化为"这句话是不是在说某某"的是非题，
  重蹈 §1.2 第二版的覆辙。
- 不确定不登记：与别名机制同一默认——证据不足、桥接章找不到、候选判别选中"都不是/无法确定"，
  一律拒绝，不登记任何猜测值。

**迁移**：纯增量字段，`default_factory=list`，旧 `bible_json` 缺这两个键时反序列化不受影响，
与 `aliases` 字段落地时的迁移方式完全相同（无需数据库列变更——`Character` 整体通过
`projects.bible_json` 序列化持久化，不是独立表）。

### 4.2 层 B · 章级认知卡（确定性拼装）

**新增函数**（建议位置 `app/portraits.py`，与既有 `_forward_fragments`/`_future_chapter_context`
同级，复用同一对前瞻窗口常量；也可独立为新模块 `app/character_cognition.py`——放哪个文件属于
实现期判断，不改变下述数据结构）：

```python
class ChapterCognitionEntry(BaseModel):
    name: str                              # 人物谱规范名
    matched_surface_forms: list[str]        # 命中的称谓：规范名或已确认别名，逐字子串命中，
                                             # 零语义、不针对具体称谓特判（复用
                                             # _alias_verdict_candidates 的判据模式）
    affiliations_as_of: list[str]           # 截至本章生效的归属摘要（org + relation_kind 拼装
                                             # 只读字符串，供提示词展示，不是新的存储字段）
    relations_as_of: list[str]              # 截至本章生效的关系摘要，拼装方式同上
    forward_appearance_hits: int            # 前瞻窗口内该角色规范名/别名的逐字出现次数


class ChapterCognitionCard(BaseModel):
    chapter_idx: int                        # 本卡对应的原著章节序号（进度锚点）
    forward_window_chapters: int            # 本次使用的前瞻窗口大小 K，记账供审计复现
    present_characters: list[ChapterCognitionEntry]
```

**组装规则（代码零语义，纯字符串/区间运算，不发起模型调用）**：

1. **在场判定**：遍历 `bible.characters`，角色 `name` 或其任一 `aliases[].text` 在
   `chapters_by_idx[chapter_idx]`（`app/stages.py:3435-3447` 已有的章节原文查找表）中逐字
   子串命中即判定"在场"——与 `_alias_verdict_candidates`（`app/stages.py:3706-3717`）同一判据，
   零语义、不针对任何具体人名/姓氏特判。
2. **状态事实解析**：对每个在场角色，从 `character.affiliations`/`character.relations` 中筛选
   `valid_from_chapter <= chapter_idx and (valid_to_chapter is None or valid_to_chapter >=
   chapter_idx)` 的条目，按 `valid_from_chapter` 降序取最新一条（区间重叠时"最近生效的一条
   优先"，与 `character_portraits` 表 `ORDER BY ep_start DESC LIMIT 1` 的既有惯例
   ——`app/portraits.py:10032-10034` `portrait_for_episode`——完全同构）。
3. **前瞻密度**：在 `chapter_idx+1 .. chapter_idx+K`（`K` 取值场景相关：面向"具名角色重要性"
   复用 `CHARACTER_IMPORTANCE_FORWARD_CHAPTERS=20`；面向"未具名角色是否值得建卡"这一新消费点
   同样复用该常量，不新造第三个窗口常量）范围内，统计角色规范名/别名逐字出现次数——与
   `_recurring_character_names`（`app/stages.py:3267`）里 `window_raw.count(name)`
   （`app/stages.py:3332`）同一统计方式，不重新发明。
4. **体积可控、可复现**：整卡只含结构化字符串与数字，不重复塞入原文大段；同一
   `(bible 快照, chapter_idx, K)` 输入任何时候重建结果逐字节相同（无模型调用、无随机性）。

### 4.3 层 C · 判别式提问

**不新造裁决机制**——把层 B 的 `ChapterCognitionCard` 作为**额外上下文**注入已经存在、已经用
三条真实事故验证过的裁决庭范式（`_alias_verdict_call`，`app/stages.py:3740-3818`）：候选集
（`_alias_verdict_candidates`）不变，卷宗（`_alias_verdict_dossier`，单章原文段落）不变，
**新增**一段结构化的"候选人已知状态"文本块，取自 `ChapterCognitionCard.present_characters`
中每个候选对应条目的 `affiliations_as_of`/`relations_as_of`：

```
候选人已知状态（截至本章，供参考，若与原文段落冲突以原文段落为准）：
- 王有材：归属 血妖宗（第 X 章证据）
- 王腾飞：归属 靠山宗（第 5 章证据）
```

**这是 §1.3 缺口的直接修复**：裁决闸原先只能看"这一章本身"的原文，现在额外看到"这些候选人
截至这一章分别是什么归属"——不需要模型凭训练记忆猜测跨章关系，结构化数据直接摆出来。**选择题
解决提问方式（层一已修复），认知卡解决回答依据（本层新增），缺一不可**：§1.2 第二版的教训
证明，只改提问方式而回答依据仍局限于单章文本，选择题会因为看不到正确候选的材料而形同虚设。

**同样适用于任何消费候选判别的场景**：不止层一的别名回填裁决闸，身份文档 §2.4 提到的
`extract_current_identity_candidates`（`app/portraits.py:3665`）等剧本阶段身份预检流程，
在需要区分"同章出现的多个同类角色指代谁"时，同样可以复用同一份认知卡注入——本文档不要求
一次性接完所有消费点（见 §8 P0/P1 拆分），但数据结构与注入格式对所有消费点通用。

**防幻觉纪律照搬（§1.2 的教训固化为规则，不得放松）**：模型申报 → 代码核验（所报章节内共现
+ 引句逐字命中）→ 候选判别裁决（段号钉证、候选集与段号均用 enum 收紧，参照
`app/stages.py:3791-3792` 对 `supporting_segment_index`/`selected_candidate` 注入 enum 的
写法）→ 不确定不登记。**属性错了比没有更糟**：没有 `affiliations`/`relations` 只是判别力不够，
错误的归属会确定性地导向错误判别——§1.2 的三例实证（孟浩←虎爷爷、王腾飞←王师弟、丹鬼←靠山
老祖）已经证明这套裁决闸能拦下错误的**别名**申报；本层的归属/关系申报必须过同一等级的闸门，
不能因为"只是辅助信息"就降低核验标准——错误的辅助信息一旦进入认知卡，会把裁决闸的判别力反向
带偏，比没有认知卡更危险。

## 5. 两条硬边界

### 5.1 前瞻只服务生产判断，绝不泄进渲染

`display_appellation`（身份文档 §4.3）已经是这道闸：第一集观众看到小胖子的脸、字幕叫"小胖子"，
第十章他自报家门才变"李富贵"——脸不变、称呼变，系统全知、观众不剧透。本层新增的
`ChapterCognitionCard` 全部字段（`affiliations_as_of`/`relations_as_of`/
`forward_appearance_hits`）**只出现在生产侧内部 payload**（裁决闸提示词、重要性判断输入），
**不得出现在** `asset_manifest.characters[].display_appellation`、分镜文本、字幕、或任何面向
观众的渲染路径——这是对身份文档既有边界的延续，不是新开的口子。

### 5.2 记忆是生产侧资产，不进视频模型上下文

参照 `docs/STORYBOARD_PROMPT_IR_DESIGN.md`（P1 设计输入，尚未实施）确立的架构：视频模型只收
分镜台编译出的**供应商无关结构化 IR**，经确定性编译器渲染为最终提示词；"模型无关内容层"进 IR
生成阶段，只维护一份（该文档 §"模型无关内容层"）。认知卡属于这一层的**输入**（帮助分镜台判断
"这个角色该不该出现在分镜里、该配什么归属背景描述"），不是 IR 本身的**输出字段**——除非认知卡
推导出的结论已经通过既有机制固化为视觉锚点串（如 `appearance_canonical`），那属于已有机制的
职责，不是本层新开的通道。跨集视觉一致性由 `visual_entity_id → 参考图` 承载，不可能靠文字
描述实现（写一百遍"银袍女子"也不是同一张脸）——这一点认知层不改变，只是不再让"该不该发角色卡"
这个决策继续无据可依。

## 6. 与分镜台/视频台的接口

### 6.1 现状：`visual_entity_id` 已贯穿到 `asset_manifest`

身份文档层三已落地：`app/production/prep_pack.py` 的 `asset_manifest.characters[]`（组装点
`:1612`/`:1828`）与 `functional_extras[]`（组装点 `:2324`）均已写入 `visual_entity_id`
（经 `app.identity_authority.visual_entity_id_for_resolution` 计算，`app/production/
prep_pack.py:209` 导入）。这是当前唯一贯穿到生产管线的视觉稳定键，本层不改动其语义。

### 6.2 结构性拆分：两个独立判断，不能合并

- **该不该复用同一张脸**：看 `visual_entity_id`（已实现）。任何重复出场角色都该复用，不论
  重不重要——否则观众觉得世界在闪。这条判断与角色是否"值得做角色卡"无关：一个只出场两次的
  龙套，第二次出场时依然要用第一次分配到的 functional 视觉实体，不能因为"戏份不够"就允许
  系统换一张新脸。
- **该不该发正经角色卡**：看认知卡的 `forward_appearance_hits`（前瞻戏份密度），是资源投放
  决策——值不值得为这个人单独调用 Seedream 生成定妆照、建立具名人物卡。这条判断本层新增。

两者独立、不耦合：即使认知卡判定某功能性角色不值得建卡，`visual_entity_id` 的分配与复用逻辑
完全不受影响，继续按身份文档 §4.2 的规则执行。

### 6.3 面向未来 IR 的接口建议（不修改 `STORYBOARD_PROMPT_IR_DESIGN.md`，只记录衔接契约）

`docs/STORYBOARD_PROMPT_IR_DESIGN.md`「IR 字段超集」表中的 `subjects[]（identity_id→定妆照）`
一行，字段命名沿用的是"identity_id"（命名权威）。身份文档 §3 的核心结论是"命名权威"与"视觉
实体"必须是两个独立键空间，画面稳定性只能挂 `visual_entity_id`。**衔接建议**：该 IR 落地
（P1，当前尚未实施——`app/compiler.py`/`app/video_prompt_profiles.py` 当前均未出现
`identity_id`/`visual_entity_id`/`subjects` 字段，逐条 grep 核实为空）时，`subjects[]` 的
取图键应改为 `visual_entity_id`；命名权威（`authority_id`/`identity_id`）与本层的
`display_appellation` 一起，只决定 `dialogue[]`/`on_screen_text` 等文本侧显示称谓。这与身份
文档 §3"两个独立键空间"的结论一致，不是本文档新引入的判断，此处只是把该结论显式记录为对未来
IR 设计的接口约束，供 IR 落地时对照。

认知卡本身**不作为 IR 字段**：视频模型不需要知道"这个角色是否值得做角色卡"这类生产决策信息，
也不该知道角色的宗门归属这种可能构成剧透的信息——除非该信息已经通过既有机制（`appearance_
canonical`/`scene_canonical` 等锚点串）沉淀为纯视觉描述，那是既有机制的职责范围，本层不新开
通道（呼应 §5.2）。

## 7. 关键收益

接上前瞻窗口后，第一集处理到未具名角色时即可判定其重要性并触发角色卡/定妆照生成——这正是身份
文档 §6 标为 P1 未做的第 11 项（「未具名角色首次出场即触发参考图生成的具体触发点……当前架构里
功能身份从未进入生成流水线"）当时缺的依据："凭什么在第一集断定他重要"。§3.3 已经证明现有的
`CHARACTER_IMPORTANCE_FORWARD_CHAPTERS` 前瞻窗口机制对**具名**角色是生效的（`_forward_
fragments`），只是从未覆盖到**未具名/功能性**角色——层 B 的认知卡把同一套前瞻密度统计能力
接到 `functional_extras` 分支，补齐这道缺口。触发逻辑本身（何时真正发起 Seedream 调用）仍然
是独立范围，见 §9 不实现清单——本文档只提供"够不够格"的判据，不实现"触发什么"的具体代码。

## 8. 模块边界

| 层 | 文件 | 改动性质 |
|---|---|---|
| A | `app/schemas.py` | `CharacterAffiliation`/`CharacterRelation` 新类 + `Character.affiliations`/`Character.relations` 新字段（紧邻 `CharacterAlias`/`aliases`） |
| A | `app/stages.py`（新函数，与 `backfill_character_aliases`@4019 同级） | 归属/关系回填函数：全书扫描 + 复用 `_alias_declaration_verified`/`_find_alias_bridge_chapter`/`_alias_verdict_dossier`/`_alias_verdict_call`/`_alias_verdict_candidates` 核验管线（视需要泛化参数命名） |
| A | `app/stages.py:4247`（`generate_bible`） | P1：提示词同步申报归属/关系（面向新项目，比照 aliases 落地时"规则 5"先例） |
| B | `app/portraits.py`（新函数，与 `_forward_fragments`@456/`_future_chapter_context`@473 同级，或独立新模块） | `ChapterCognitionCard` 组装：在场判定 + 状态事实区间解析 + 前瞻密度统计，复用 `IDENTITY_DISCOVERY_FORWARD_CHAPTERS`/`CHARACTER_IMPORTANCE_FORWARD_CHAPTERS` 两个既有常量 |
| C | `app/stages.py:3740`（`_alias_verdict_call`） | 提示词新增"候选人已知状态"文本块，来自认知卡的 `affiliations_as_of`/`relations_as_of` |
| C | `app/portraits.py:3665`（`extract_current_identity_candidates`）及其提示词构造链（`_project_current_identity_response`@1529） | P1：剧本阶段身份预检的候选判别场景同样接入认知卡（P0 只接层一裁决闸） |
| — | `app/production/prep_pack.py` | 不改动：`visual_entity_id`/`display_appellation` 语义边界不动（§5、§6） |

## 9. P0/P1/P2 拆分

### P0（本层必须完成——直接支撑判别式提问的核心闸门）

1. `app/schemas.py`：`CharacterAffiliation`/`CharacterRelation` 结构定义 + `Character.
   affiliations`/`Character.relations` 字段（纯增量，向后兼容，无需数据库迁移）。
2. `app/stages.py`：归属/关系回填函数，复用层一已验证的核验管线（§4.1），用于当前项目一次性
   回填；候选集必须包含关系对象本人或组织已知成员（§4.1），不确定不登记。
3. `app/portraits.py`（或新模块）：`ChapterCognitionCard`/`ChapterCognitionEntry` 组装函数，
   代码零语义确定性拼装（§4.2），复用既有两个前瞻窗口常量，不新造常量。
4. `app/stages.py:3740`：`_alias_verdict_call` 提示词接入认知卡的"候选人已知状态"文本块
   （§4.3），修复 §1.3 指出的"卷宗证据不含跨章关系性知识"缺口。

### P1（架构完整性所需，不阻断"判别式提问看得见跨章知识"这条核心判据本身）

5. `app/stages.py:4247`：`generate_bible` 主提示词同步申报归属/关系（面向新项目）。
6. `app/portraits.py:3665`：剧本阶段身份预检（`extract_current_identity_candidates`）等其它
   候选判别消费点接入认知卡。
7. 未具名角色首次出场即触发参考图生成的具体触发点（§7 关键收益指向的缺口；本文档只提供"够不够
   格"的判据，触发逻辑设计与成本评估留待专项，范围与风险需单独评估——与身份文档 §6 P1 第 11
   项的立场一致）。
8. 归属/关系核验结果的记账日志（比照 `logs/backfill_character_aliases.log` 的落库核验小节），
   供人工抽查复核拒绝原因分布。
9. 历史项目（非当前回归项目）的归属/关系回填批处理脚本。

### P2（性能/收尾，不阻断功能正确性）

10. `org`/`relation_kind` 同义表述归并词典（如"血妖宗"/"血妖门"）——本次不做，避免过度设计；
    若后续证明确有需要，走独立评估。
11. `ChapterCognitionCard` 的缓存层（若逐集重复构建的调用成本在生产中被证明是瓶颈）。
12. 认证核验管线参数从"别名语境命名"（`text`/`true_name`）泛化为通用命名（`claim_text` 等）
    的重构，提升 A1a/A1b 章节代码在层一/本层间的复用清晰度。

## 10. 本次明确不实现的功能

- 不实现未具名角色首次出场即自动触发参考图生成的具体代码（P1 占位，见 §9 第 7 项）。
- 不新造 `org`/`relation_kind` 归一化/同义词典（P2，§9 第 10 项）。
- **不实现针对"王师弟""许师姐"等具体词或具体姓氏的特判分支**——CLAUDE.md 明令禁止黑白名单式
  修复；判据只能是模型申报 + 代码核验 + 候选判别裁决的结构性证据机制（逐字引句 + 章节序号 +
  有效区间），不允许出现任何具体称谓/姓氏的硬编码分支。
- 不实现跨项目共享归属/关系库——沿用身份文档的 `project_id` 隔离纪律，不做跨项目复用。
- 不改变 `visual_entity_id`/`display_appellation` 的既有语义（§5、§6 边界不动）。
- 不在分镜 IR/视频提示词中传递认知卡任何字段（§5.2 硬边界）。
- 不实现 `docs/STORYBOARD_PROMPT_IR_DESIGN.md` 的完整 IR/编译器——那是独立 P1 设计，本文档
  只记录接口衔接建议（§6.3），不替它下实现细节。
- 不代入本次三条实证误登记（§1.2）的最终清库/重跑决策——是否需要对已回填的项目数据做进一步
  处理，由持有该数据的一方决定，不在本文档内下判断。
- 不对已发布分集数据做任何清库/重跑操作——本任务只写文档，不碰数据、不重启服务、不发起生成
  run。

## 11. 可机械判定的验收判据

1. **状态事实区间查询确定性**：同一 `(character, chapter_idx)` 任何时候查询
   `affiliations_as_of`/`relations_as_of` 结果一致（无随机性、无模型调用）。
2. **认知卡可复现**：同一 `(bible 快照, chapter_idx, K)` 输入，任何时候重建
   `ChapterCognitionCard` 逐字节相同（机械回归测试）。
3. **证据可追溯**：`CharacterAffiliation`/`CharacterRelation` 每条记录的 `evidence_quote` 都
   能在 `evidence_chapter_index` 对应的原著章节原文中逐字命中（机械字符串包含检查，与身份文档
   §8 第 3 条对 `aliases` 的判据同构）。
4. **三例实证不倒退**：`孟浩←虎爷爷`、`王腾飞←王师弟`、`丹鬼←靠山老祖` 三条错误归属/别名在
   引入认知卡后的裁决闸复核中必须继续被拒绝——不能因为认知卡的引入放松已有防线（回归覆盖
   `logs/backfill_character_aliases.log:518-559` 的三条 `candidate_mismatch`/
   `candidate_uncertain` 结论，即最终写库版本）。
5. **无黑白名单**：代码审查判据——`grep` 归属/关系回填与裁决闸相关代码，不得出现任何具体人名/
   姓氏/称谓字符串的 `if` 特判分支。
6. **前瞻不泄露渲染**：`asset_manifest`/字幕/分镜文本中不出现 `affiliations_as_of`/
   `relations_as_of`/`forward_appearance_hits` 等认知卡专属字段的内容（机械字段名扫描）。
7. **视觉/生产分离**：认知卡字段不出现在 IR/视频模型提示词的任何输出路径中（待 IR 落地后可
   机械核对：IR 编译产物中不含认知卡字段名）。
8. **命名权威与视觉实体不受影响**：`authority_id_for_resolution`/`visual_entity_id_for_
   resolution` 的既有行为逐字不变，本层改动不触碰这两个函数。

## 12. 风险与回滚

| 风险 | 说明 | 缓解 |
|---|---|---|
| 归属/关系回填召回率低 | 候选判别裁决闸门严格（§1.2 教训），可能导致大量真实归属因证据不足而不登记（如"王师弟→王有材"本身在本次测试中也从未被正确申报过） | 符合"不确定不登记"的安全默认；召回率不足只是判别力不够（比没有更安全，见 §4.3"属性错了比没有更糟"），不是本层验收的硬指标；P1 可补充人工标注通道，本次不做 |
| 状态事实区间标注错误 | `valid_from_chapter`/`valid_to_chapter` 若被模型申报错误，会导致"截至第 N 章"查询返回错误结果 | 复用与别名同等级的三闸核验（§4.1），且 §11 判据 4 要求三例实证不倒退；区间本身也需要证据锚点支撑，不能凭空申报 |
| 认知卡注入提示词导致 token 成本上升 | `_alias_verdict_call`（`app/stages.py:3740`）等裁决闸提示词新增"候选人已知状态"文本块 | 认知卡只含结构化摘要字符串（org+relation_kind 拼装），体积远小于原文卷宗；小流量验证后再全量启用 |
| 认知卡与既有 `Relationship`（静态叙事关系）字段混淆 | `Character.relationships`（无证据锚点）与新增 `Character.relations`（有证据锚点+区间）字段名相近，后续开发者可能混用 | 两者docstring 互相注明区别（§4.1 已写明）；命名上刻意保留both 并存而非合并，避免为了统一命名而破坏 `relationships` 现有消费方（`app/validators.py:6184-6186` 等） |
| 改动面涉及裁决闸核心提示词 | `_alias_verdict_call` 是三次真实事故验证过的稳定机制，注入新内容有回归风险 | P0 判据 4（§11）要求三例实证不倒退；提示词新增内容作为独立文本块追加，不改写既有候选集/卷宗构造逻辑 |

**回滚**：P0 全部改动均为新增字段/新增函数/提示词追加文本块，不删除、不改写既有字段语义。
`Character.affiliations`/`relations` 为空列表时，`ChapterCognitionCard` 组装函数应当优雅
降级为"无归属/关系信息"（不报错、不阻断），裁决闸提示词的"候选人已知状态"文本块为空时省略
该段落——因此即使本层回填从未运行过，裁决闸行为完全回退到身份文档已验证的现状。回滚成本低，
可按文件逐个 revert。

---

# 第二期：指代链、场景回指与结构化外表（2026-08-25 二次修订新增）

以下 §13-§18 是本次二次修订新增的内容，不改动、不删除上方 §0-§12 的第一期记录。第一期解决
"判别"（候选之间选谁），第二期解决"召回"（谁该进候选集）——两者边界见 §0.5、§13.4。

## 13. 指代链层设计（第二期核心）

### 13.0 定位：这一层补的是"召回"，不是"判别"

§0.5 已经点出边界：第一期候选集完全由 `_alias_verdict_candidates`（`app/stages.py:3910`）
决定，判据是"角色规范名或其任一已登记别名在章节原文里逐字子串命中"——纯字符串匹配，不做语义
联想。这不是这个函数的缺陷，它的职责范围本来就限定在"已确认表述的逐字命中"。指代链要新增的
是：一处**描述性指代**（不是任何角色的规范名或已登记别名）在满足严格的硬证据条件时，也能让
对应角色进入候选集，而不改变候选集确定之后的判别逻辑本身（§4，第一期，原样复用）。

### 13.1 一、指代链（核心新增）

**新增结构**（`app/schemas.py`，紧邻 `CharacterAlias` 之后，与其同构，复用同一套证据锚点
纪律）：

```python
class CharacterReferenceLink(BaseModel):
    """指代链单节点（本次核心新增）。与 CharacterAlias（app/schemas.py:112）的关系：
    CharacterAlias 是"某段文本可以被逐字复用于命中判定的称谓"（层一，已实施），进
    CharacterAlias 的每一条天然也是指代链的一个节点（connection_basis 落在
    direct_naming/registered_alias）；指代链新增的是 CharacterAlias 覆盖不到的第三类：
    描述性指代（不是称谓，不能被复用于逐字命中判定），只有满足下方三种硬证据之一才能进链。
    """

    surface_form: str
    # 逐字指称文本：称谓（"小胖子"）或描述性短语（"白白净净身子较胖"）均可，不再限定
    # "称谓"——这正是本次要补的召回缺口（§13.0）

    connection_basis: Literal[
        "direct_naming", "registered_alias", "explicit_backreference",
    ]
    # 连接依据——只允许这三种，不接受第四种（§13.1"严禁"清单）。用 Literal 而非
    # CharacterAlias.name_kind/CharacterAffiliation.relation_kind 那种宽松 str：
    # 后两者是模型自由声明的分类文本，用 str 避免模型用词稍有出入就被硬枚举整条拒绝；
    # connection_basis 不是模型自由声明的字段，是核验管线跑完后由代码根据"走的是哪条
    # 核验路径"结构性写入的结果（direct_naming/registered_alias 由字符串匹配确定性
    # 判定，explicit_backreference 由 §13.2 判定流程第 3 步的裁决闸结果确定性判定），
    # 不存在"模型自由用词"的风险，用 Literal 收紧更安全。

    evidence_chapter_index: int
    evidence_quote: str
    # 逐字引句，核验规则与 CharacterAlias.evidence_quote（app/schemas.py:112 起）完全
    # 一致：必须能在该章节原文中作为子串命中

    backreference_anchor_id: str | None = None
    # 仅 connection_basis="explicit_backreference" 时必填：回指的场景锚点 ID
    # （见 §13.2 SceneAnchor.anchor_id）；其余两种 connection_basis 下必须为 None

    verdict_segment_index: int | None = None
    # 裁决记账：candidate_verdict 裁决通过时钉住的卷宗段号，供审计追溯（与别名裁决闸
    # supporting_segment_index 同一记账习惯，见 app/stages.py:3944 起 _alias_verdict_call）
```

`Character` 新增字段：`reference_chain: list[CharacterReferenceLink] = Field(default_
factory=list)`（`app/schemas.py`，紧邻 `aliases` 字段之后，纯增量，向后兼容）。

**连接依据只允许三种硬证据**（本设计最重要的约束）：

1. **直接命名**：真名在该处出现。
2. **已登记别名命中**：走现有 `CharacterAlias` 机制核验通过的别名，在该处逐字命中。
3. **显式回指**：某处文本明确回指更早的一幕/一群人（如"当初我们四人被一起带上靠山宗"），
   回指目标必须是一个已登记的场景锚点（§13.2），且该锚点与本处声明的参与者集合能一一
   对应（§13.2 判定流程）。

**严禁**仅凭以下三类弱证据建立连接——都是今晚的真实事故，不是假设：

- **仅凭外表相似**：不允许。"绿袍男子"这类描述在本书能撞上一大片人（同色系服饰的角色不止
  一个），外表只能是候选判别时的辅助排除依据（§13.3 硬规则），不能单独构成连接。
- **仅凭同章共现**：孟浩←虎爷爷（第 3 章）。第 3 章原文核查（`data/manju.db` `chapters`
  表 `idx=3`，只读查询）确认"孟浩"在该章出现 **59 次**，"虎爷爷"只出现在一个杂役大汉的
  台词「不然虎爷爷活撕了你们」里——共现极高不代表指代，裁决闸最终判为 `candidate_
  uncertain` 正确拒绝（`logs/backfill_character_aliases.log:400/443/486/529`，四次复核
  一致）。
- **仅凭"人物谱说他在本集活跃"**：绿袍男子→赵武刚，`PREP_PACK_VERSION` 1.8.3→1.8.4 完整
  回退的真实事故（§1.4 已详述，`app/production/prep_pack.py:478-497`）。同姓/是非题诱发
  的确认偏误还有一例：王腾飞←王师弟（第 189 章），真实指代是同属血妖宗的王有材，裁决闸判
  为 `candidate_mismatch`（`logs/backfill_character_aliases.log:458/501/544`）。

三例结论一致：**"证据存在"不等于"证据支持结论"**——共现、组织成员关系、外表相似都是关于某个
角色的真实信息，但都证不出"这处未解析的表述就是他"。指代链的三种硬证据都要求"这处表述本身"
（或它显式回指的那一幕）与目标角色之间存在**不依赖第三方旁证的直接连接**。

### 13.2 二、场景回指关系

**新增结构**（`app/schemas.py`，紧邻 `Scene` 之后、`Bible` 之前）：

```python
class SceneAnchor(BaseModel):
    """场景/事件锚点：全书范围内某一幕独立可指认的情节单元（"四少年一起被抓上靠山宗"），
    供角色的指代链在此处交汇、供后续文本显式回指。与 app/production/prep_pack.py 的
    event_chain[].source_span（app/production/prep_pack.py:29-30，{from_segment,
    to_segment}）是同一"事件跨度"思路在不同层级的复用，但不合并、不共用同一张表：
    prep_pack 的 event_chain 范围限定在单集（episode_scope），服务分镜编排；本结构范围
    是全书（挂在 Bible 上），服务跨章节的阅读记忆。合并会让单集分镜台背上全书回填的复杂度，
    也会让"全书只算一次"的锚点被按集重复计算——两者职责、生命周期都不同，保持独立。"""

    anchor_id: str
    # 稳定 ID，建议 f"anchor:{起始章节idx}:{序号}"，同一 (bible 快照) 内确定性生成

    summary: str
    # 该幕的简短描述，供人工审阅，非证据本身、不参与任何判定

    source_chapter_range: tuple[int, int]
    # 该幕原著章节跨度（起, 止，含）；通常同一章内，允许跨章

    source_quote: str
    # 逐字引句：该幕在源章节中的核心描述文本，必须能在 source_chapter_range 覆盖的章节
    # 原文中逐字命中

    participants: list[str] = Field(default_factory=list)
    # 该幕已确认参与者：人物谱规范名列表，允许为空（如首次出现时全员未具名）

    unresolved_slots: int = 0
    # 该幕中"存在但未能绑定到规范名"的角色数量（如"四少年"已具名 3 个、还剩 1 个未具名
    # 槽位）——供显式回指判定时核对"回指声明的人数"与"锚点剩余槽位数"是否吻合，只是辅助
    # 校验，不能单独构成绑定依据（同 §13.3 外表纪律，见 §14.1 的拆分纪律）
```

`Bible` 新增字段：`scene_anchors: list[SceneAnchor] = Field(default_factory=list)`
（`app/schemas.py`，紧邻 `scenes` 字段（235 行）之后，纯增量）。

**判定流程（复用既有裁决庭范式：代码检索卷宗 → 模型裁决 → 逐字钉证 → 不确定不连）**：

1. **锚点建立**（回填阶段，模型申报 + 代码核验）：模型从全书原文中识别"多人共同经历、后续
   可能被回指"的情节单元，申报 `summary`/`source_chapter_range`/`source_quote`/初始
   `participants`；代码核验 `source_quote` 能在声明的章节范围内逐字命中，核验通过才建立
   `SceneAnchor`。不要求锚点覆盖全书情节——只在候选判别或人工回填触及到时才建立，避免过度
   设计（无消费点的锚点不产生价值，也不核验）。
2. **回指候选检索**（复用 `_find_alias_bridge_chapter` 同构的确定性检索，`app/stages.py:
   3539`）：某处文本申报"这是一处显式回指"，代码检索该文本提到的关键短语（如"当初""四人""
   一起被带上"）是否能定位到某个已登记的 `SceneAnchor.source_quote` 所在范围，定位不到
   直接拒绝，不发起裁决调用。
3. **候选判别裁决**（复用 `_alias_verdict_dossier`（`app/stages.py:3811`）/
   `_alias_verdict_call`（`app/stages.py:3944`）/`_alias_verdict_candidates`
   （`app/stages.py:3910`）同构管线）：卷宗同时包含锚点原文段落与回指处原文段落，候选集是
   锚点 `unresolved_slots` 对应的未具名角色 + 回指处声明的具名列表，模型需要在两侧文本间
   建立"回指处点名的某个名字，对应锚点未具名槽位中的哪一个"这一具体映射，选段号钉证，
   "都不是/无法确定"一律拒绝。
4. **写回**：裁决通过后，`SceneAnchor.participants` 补上新确认的规范名、`unresolved_
   slots` 减一；对应角色新增一条 `CharacterReferenceLink`（`connection_basis="explicit_
   backreference"`，`evidence_chapter_index` 指向锚点所在章节，`backreference_anchor_id`
   指向该锚点）。

### 13.3 三、外表特点（结构化，带证据锚点）

**新增结构**（`app/schemas.py`，紧邻 `Character.appearance_canonical`（178 行）之后）：

```python
class AppearanceFeature(BaseModel):
    """一条外表特征证据，带证据锚点。**硬规则：外表特征只能作为裁决候选判别时的辅助区分
    依据，绝不能单独构成"这是同一个人"的绑定依据**——见下方硬规则说明与反例。"""

    category: str
    # 体型/肤色/服饰/年龄印象/显著特征等自由文本分类，不设 Literal 枚举——与
    # CharacterAffiliation.relation_kind（app/schemas.py:129 起）同一宽松校验风格，
    # 模型自主归类，避免卡在硬枚举上被整条拒绝；不追求穷尽分类，够用即可

    description: str
    # 近似原文的特征短语（如"身形圆胖皮肤白净"）

    evidence_chapter_index: int
    evidence_quote: str
    # 逐字引句，核验规则与 CharacterAlias.evidence_quote 完全一致
```

`Character` 新增字段：`appearance_features: list[AppearanceFeature] = Field(default_
factory=list)`（`app/schemas.py`，紧邻 `appearance_canonical` 字段之后）；
`appearance_canonical`（现有单块文本字段，`app/schemas.py:178`）**不改动、不废弃**——
它仍是画像生成提示词的合成来源（`refs.portrait_prompt`），`appearance_features` 是它的
结构化补充视图，两者关系与 `Character.relationships`/`Character.relations`（静态叙事 vs
带证据锚点的结构化事实）同一并存模式。

**硬规则（务必显著遵守）：外表只能作为裁决时区分候选的辅助依据，绝不能单独构成绑定依据。**
理由：本书"绿袍男子""银袍女子"这类外貌型标签能撞上一大片角色（服饰颜色在修仙小说里高度
重复），仅凭外表相似绑定就是下一个赵武刚（§1.4/§13.1 已详述的真实误绑事故，`PREP_PACK_
VERSION` 1.8.3→1.8.4 回退）。落地方式与第一期"候选人已知状态"文本块同构（`_alias_verdict_
call`，`app/stages.py:3944` 起，`cognition_section` 的措辞"以上认知卡只用于辅助区分候选
身份，本身不构成判定依据"）：外表特征进认知卡的背景区，裁决提示词追加一段"候选人外表参考"，
显式声明"仅供参考，若与原文段落冲突以原文段落为准，不得仅凭外表描述下结论"；绑定仍须走
§13.1 的三种硬证据之一，外表证据不参与 `connection_basis` 的判定输入。

### 13.4 四、与既有机制的衔接（不重复造轮子）

- **`Character.aliases`（层一，已实施）**：指代链复用它作为链上"称谓"类节点的来源
  （`connection_basis` 为 `direct_naming`/`registered_alias` 的节点，实质上就是
  `aliases` 里已核验的条目在链上的投影），不重复造一套称谓核验机制——真正新增的只是
  "描述性指代 + 显式回指"这一类 `aliases` 机制原理上覆盖不到的节点（§13.1）。
- **`visual_entity_id`（身份文档层二，已实施）**：指代链解析出的绑定结论属于命名/身份
  范畴，最终仍通过既有 `visual_entity_id_for_resolution`（`app/identity_authority.py:
  138`）落到取图键；指代链不新造视觉键，也不改变该函数的既有语义（沿用身份文档 §5.2 的
  边界）。
- **`asset_manifest.characters[].display_appellation`（身份文档层三，已实施，
  `app/production/prep_pack.py:39`）**：本集显示措辞仍取本集原文用词，指代链只影响生产
  侧判断"该不该把这次提及并入某角色"（召回），不影响面向观众的显示文本选择（哪怕视觉/身份
  侧已经通过指代链确认是同一人，字幕/台词仍只说本集措辞）——同一条剧透纪律延续，不新开
  口子（§5.1 已有表述，本节不重复）。
- **认知卡 / `_alias_verdict_call`（第一期，已实施）**：召回（本节）与判别（§4）是两个
  独立阶段——指代链决定"谁该进候选集"，认知卡决定"候选之间选谁"。指代链新增的召回路径
  （描述性指代 + 显式回指）产出的候选，进入 `_alias_verdict_candidates` 同一张候选表后，
  走的仍是 §4 已实施、已用三条真实事故验证过的同一套候选判别裁决闸——不新造第二套判别
  机制，只是候选表的来源从"纯字符串命中 `aliases`"扩展为"纯字符串命中 `aliases` ∪ 指代链
  显式回指确认的节点"。

现有文档在 §0/§1 的问题陈述里把"跨章关系性知识如何注入判别"（判别问题）和"这处表述该不该
指向某个人"（召回问题）混在一起讨论，容易让人以为第一期的"三层设计"已经覆盖了全部认知层
职责——本节是这条边界的显式澄清，供后续开发者和文档读者对照。

## 14. 教训沉淀（本次修订新增）

### 14.1 一条声明必须拆成"已验证部分"与"未验证部分"分别处置

今晚（2026-08-24 深夜至 2026-08-25）在这条纪律上栽了两次，方向相反：

- **太松**：赵武刚案（§1.4）——把"人物谱登记显示在本集活跃"这条关于角色本人的真实但**未
  核验与本处标签相关**的背景信息，当成了绑定证据来用。人物谱的 `ep_start`/外貌描述本身是
  核验过的事实，但"这条事实支持本集这处标签就是他"从未被核验过，1.8.3 把两者混为一谈，
  直接导致误绑，1.8.4 完整回退（`app/production/prep_pack.py:478-497`）。
- **太紧**：状态事实回填一度**100% 拒绝**——核心事实（角色 + 归属对象 + 证据章 + 引句，
  已经过候选判别裁决核验）与区间边界（`valid_from_chapter`/`valid_to_chapter`，模型对
  "从哪章起/到哪章止"的外推猜测）是两件独立的事，早期实现把两者绑在一起：区间边界核验
  不过，就连已经核验通过的核心事实一并拒绝——这是**用未核验的部分否决了已核验的部分**，
  与赵武刚案的错误方向正好相反，但根子是同一件事：没有把"声明"拆成独立核验的子部分分别
  处置。

修复方式已经在 `app/schemas.py`/`app/stages.py` 落地（本文档撰写时另一 agent 正在这两个
文件并行工作，写作本节时的现场核对如下，后续仍可能变动，以 `grep -n` 复核为准）：
`CharacterAffiliation`/`CharacterRelation` 新增 `valid_from_is_fallback`/`valid_to_is_
fallback: bool = False` 两个标注位（`app/schemas.py:156-157`，`CharacterRelation` 同构
字段在 171-172），`_status_fact_interval_resolution`（`app/stages.py:4584`）改为区间边界
与核心事实矛盾（如终点早于证据章）才整条拒绝，边界外推缺乏独立支撑时只回落该边界并标注
`True`，不再连带拒绝核心事实。

但真实排查（`app/stages.py:4487` 起 `_status_fact_verdict_call` docstring 记录的事故）
揭出这条拆分纪律更深一层的价值：`proj_3ac0b627fa46` 全量回填 22 条申报 0 条通过，最初被
误诊为"区间核验过严"（即误以为是上面这条回落逻辑没做对）；但顺着"先分清楚是核心事实没过，
还是区间边界没过"这条纪律往下查，才发现区间核验环节从未被真正触及——**全部卡在更早的候选
判别环节**：对人物关系事实，提问措辞把 `claim_text`（关系对象 `to`，本身就是候选集里一个
现成的人名）和 `subject_name`（关系发起方，结构上恒不等于 `to`）搞混，模型被问"'{claim_
text}' 这个名字实际说的是候选中的哪一位"时，老实回答"就是它自己"——问题问的和要验证的
根本不是同一件事，与证据是否真实成立完全无关，100% 必然 `candidate_mismatch`。修复是把
提问改成"谁拥有/构成这层关系"（fact-to-person）并把 `claim_text` 从候选枚举里剔除
（`app/stages.py:4724`）。**这恰恰是"拆分已验证/未验证部分"这条纪律的另一重价值**：一个
"回填 100% 拒绝"的单一症状背后可能压着两个独立问题（一个真实存在但从未真正触发的过严
规则，一个结构性问的问题本身就错了），眉毛胡子一把抓地"放宽标准"只会先修对没被卡住的那
一环，修不到症状的真正病灶——必须先把声明拆成独立的核验环节分别排查，才找得到真正卡住的
那一步。这项修复的最终验收结论不属于本文档交付范围，这里只记录教训与写作本节时的现场
代码状态，不代入其上线判断（与 §1 对别名回填修复的记账方式一致：只引用已核实的部分）。

**规则**：本文档 §13 新增的三种数据结构（`CharacterReferenceLink`/`SceneAnchor`/
`AppearanceFeature`）在设计核验管线时必须遵守同一拆分纪律——核心指代（谁、连接依据、逐字
引句）与辅助信息（如 `SceneAnchor.unresolved_slots`、外表特征）的核验状态必须分开记账，
辅助信息核验不过不能拖累核心指代一起被拒绝；反过来，核心指代未核验通过时，辅助信息也不能
单独顶替它成为绑定依据（呼应 §13.1"严禁"清单）。

### 14.2 规模与风险：指代链连错等于合并两个人，污染全书而非一集

单集候选误绑（如赵武刚案）影响范围是这一集的 `asset_manifest`；指代链一旦连错，等于在
`Character.reference_chain` 里把两个不同的人焊成了同一条链——后续任何消费这条链的判别
（认知卡候选集、别名召回）都会在**全书范围**内持续复用这个错误连接，污染面从"一集"变成
"全书"，是质的不同，不是量的不同。

**已知风险基线**（今晚实测，`logs/backfill_character_aliases.log:520-559` 最终写库
记录）：别名回填一次全书扫描共申报 14 条别名声明，其中 3 条经候选判别裁决闸拦下
（`孟浩←虎爷爷`、`王腾飞←王师弟`、`丹鬼←靠山老祖`），即模型初次申报的错误率约
**21%（3/14）**，全部依赖裁决闸事后拦截，不是申报阶段就没有错误。指代链的召回路径
（尤其"显式回指"这一类，判定难度高于单纯的别名核验）没有理由假设错误率会更低——必须把
这条基线当作"最好情况"的参照，而不是终值。

因此必须**先小范围验证准确率，再全书铺开**，不允许第一次实现就直接对全书 1616 章跑批量
回填。分阶段方案与可机械判定的判据见 §17。

## 15. 第二期 P0/P1/P2 拆分（指代链/场景锚点/结构化外表）

本节是对 §9（第一期 P0/P1/P2，已完成）的追加，不重复列出第一期条目。

### P0（第二期必须完成——直接支撑"EP1 李富贵"这类真实案例）

1. `app/schemas.py`：`CharacterReferenceLink`/`SceneAnchor`/`AppearanceFeature` 结构
   定义 + `Character.reference_chain`/`Character.appearance_features`/`Bible.
   scene_anchors` 字段（纯增量，向后兼容，无需数据库迁移，§13.1/§13.2/§13.3）。
2. 场景锚点建立 + 显式回指候选判别管线（§13.2 判定流程 1-4），复用 `_find_alias_bridge_
   chapter`/`_alias_verdict_dossier`/`_alias_verdict_call`/`_alias_verdict_candidates`
   同构管线，不新造第二套语义判断机制。
3. 描述性指代召回接入：候选集构造（`_alias_verdict_candidates` 同构逻辑）扩展为"规范名/
   已登记别名逐字命中" ∪ "指代链中 `connection_basis=explicit_backreference` 已确认的
   节点"，不改判别裁决本身。
4. 首个可机械判定的验收判据落地（§17.1 判据 1）：EP1"白白净净身子较胖"经第 10 章显式
   回指绑定到李富贵——这是本次修订的决定性真实案例（§1.4），P0 完成的直接证明。
5. 外表结构化字段的核验管线（§13.3），复用现有三闸（逐字子串命中 + 同段/同章共现 + 候选
   判别裁决），且必须显式落地"外表不单独构成绑定依据"这条硬规则（提示词/schema 层面外表
   证据不出现在 `connection_basis` 的候选输入里）。

### P1（架构完整性所需，不阻断"EP1 李富贵能否绑定"这条核心判据）

6. 场景锚点的批量回填（全书扫描，一次性建立历史项目的 `scene_anchors`），比照
   `backfill_character_aliases`/`backfill_character_status_facts` 的窄口径回填先例。
7. `generate_bible` 主提示词同步申报指代链候选节点（面向新项目）。
8. 指代链/场景锚点核验结果的记账日志（比照 `logs/backfill_character_aliases.log`）。
9. 场景锚点与外表特征的人工抽查通道（§14.2 规模风险的直接缓解，抽查比例与准出门槛见
   §17.2）。
10. 历史项目（非当前回归项目）的指代链回填批处理脚本。

### P2（性能/收尾，不阻断功能正确性）

11. `SceneAnchor` 的缓存/索引层（若全书规模扫描的调用成本被证明是瓶颈）。
12. 描述性指代的自动候选发现（当前 P0 只处理"模型已经主动申报的显式回指"，不做主动扫描
    全书寻找"疑似回指"的语言模式识别——避免过度设计，需求明确后再评估）。

## 16. 本次追加的"不实现"清单（叠加 §10，不删除 §10 已有条目）

- **不实现情绪演进、性格特点的结构化记忆**（§0.5 已述理由：对画面一致性无直接作用、且
  证据锚点更难核验）——`Character.personality`/`speech_style` 保持现状（静态自由文本，
  不新增带证据锚点的演进轨迹）。
- **不允许仅凭外表相似度、仅凭同章共现、仅凭"人物谱声明本集活跃"建立指代链连接**——
  §13.1 已列为硬约束，此处重申为不实现清单的负面表述：不做外貌向量相似度匹配、不做共现
  频次阈值判定、不做人物谱在场声明当召回依据。
- **不做任何具体称谓/姓氏/描述短语的黑白名单特判**——CLAUDE.md 明令禁止；连接依据只能是
  §13.1 的三种结构性硬证据，`grep` 相关代码不得出现具体人名/称谓字符串的 `if` 分支
  （§17.1 判据 6 与 §11 判据 5 同构，合并核查）。
- **不做描述性指代的主动全书扫描发现**（P2 占位，§15 第 12 项）——本次只处理模型主动
  申报的显式回指，不主动挖掘"疑似同一人"的候选。
- **不合并 `SceneAnchor` 与 `app/production/prep_pack.py` 的 `event_chain[].
  source_span`**——两者服务不同范围（全书 vs 单集），§13.2 已述理由，保持独立。
- **不对已发布分集数据做任何清库/重跑操作**——本任务只写文档，不碰数据、不重启服务、
  不发起生成 run（与 §7/身份文档 §7 同一纪律）。
- **不代入"状态事实回填 100% 拒绝"修复的最终验收结论**——§14.1 已述，该修复由另一 agent
  在本文档撰写同时进行，具体实现与验收不在本文档职责范围。

## 17. 可机械判定的验收判据与分阶段推进方案（第二期）

### 17.1 可机械判定的验收判据

1. **首个判据（决定性）**：`Character(name="李富贵").reference_chain` 中存在一条
   `connection_basis="explicit_backreference"` 的节点，`evidence_chapter_index=1`，
   `surface_form` 命中"较胖"（或等价的第 1 章原句子串），`backreference_anchor_id`
   指向的 `SceneAnchor` 的 `source_chapter_range` 覆盖第 1 章，且该锚点存在一条来自
   第 10 章的显式回指记录（§1.4 决定性真实案例的机械化表达）。
2. **三例历史误绑不倒退**：孟浩←虎爷爷、王腾飞←王师弟、绿袍男子→赵武刚三条错误连接，在
   指代链引入扩大召回路径后，裁决闸复核中必须继续被拒绝——不能因为候选集变大就放松已有
   防线（与 §11 判据 4 同构，扩展覆盖第二期新增的召回路径）。
3. **证据可追溯**：`CharacterReferenceLink`/`SceneAnchor`/`AppearanceFeature` 每条
   记录的 `evidence_quote`/`source_quote` 都能在对应章节原文中逐字命中（机械字符串
   包含检查）。
4. **回指引用完整性**：任一 `connection_basis="explicit_backreference"` 的节点，其
   `backreference_anchor_id` 必须指向 `Bible.scene_anchors` 中真实存在的一条记录
   （机械引用完整性检查，不允许悬空指针）。
5. **外表不参与绑定**：`grep` 指代链/候选判别相关代码，`AppearanceFeature`/
   `appearance_features` 不出现在任何 `connection_basis` 判定分支的输入条件里，只能
   出现在裁决提示词的背景参考文本块中（机械字段名扫描，同 §11 判据 6 的检查方式）。
6. **无黑白名单**：`grep` 指代链回填与场景锚点裁决闸相关代码，不得出现任何具体人名/
   姓氏/称谓/描述短语的 `if` 特判分支（与 §11 判据 5 同构）。
7. **可复现**：同一 `(bible 快照, 全书原文)` 输入，指代链/场景锚点回填任何时候重跑，
   核验通过的连接结果集合一致（模型调用本身有非确定性，但"同一份已落库结果"的核验判据
   ——即 `evidence_quote` 逐字命中检查——必须是确定性的，机械回归测试覆盖判据 3/4）。

### 17.2 分阶段推进方案

- **阶段 0（先导验证，本次 P0 范围）**：只对已知真实案例（李富贵、EP1-EP10 已回归覆盖的
  章节范围）跑指代链回填，目标是让判据 1（首个判据）与判据 2（三例不倒退）同时通过。不对
  全书 1616 章批量跑。
- **阶段 1（小范围抽查）**：扩大到全书前若干百章（具体范围留待阶段 0 通过后由持有回归
  节奏的一方拍板），人工抽查新增的 `explicit_backreference` 连接，错误率作为准出门槛
  ——参照 §14.2 的别名回填基线（21%，3/14，裁决闸事后拦截），指代链因错误影响范围是全书
  而非一集，门槛应显著严于该基线；具体数值由抽查结果反推，不在本文档预设一个未经验证的
  数字。抽查不通过则回到阶段 0 修裁决闸，不允许跳过重新抽查直接扩大范围。
- **阶段 2（全书铺开）**：阶段 1 抽查通过后，对全书剩余章节批量回填，仍旧保留裁决闸与
  "不确定不连"的默认——阶段 2 不代表放松核验标准，只代表范围扩大。
- 每个阶段推进前必须重新跑判据 2（三例历史误绑不倒退）与判据 3（证据可追溯）作为回归
  门槛，失败即停在当前阶段，不越级推进（与项目"串行回归、失败即停"的既有纪律一致）。

## 18. 第二期风险与回滚

| 风险 | 说明 | 缓解 |
|---|---|---|
| 指代链连错污染全书 | §14.2 已述：候选误绑影响一集，指代链误连影响全书范围内所有消费该链的判别 | §17.2 分阶段推进，阶段 0/1 验证通过前不批量跑全书；§17.1 判据 2 要求历史误绑不倒退作为每阶段回归门槛 |
| "显式回指"判定本身是语义判断，难度高于别名核验 | 别名核验判据是"文本是否指代某人"，显式回指额外要求"跨章节映射未具名槽位到具名列表"，判断链条更长 | 复用同一套裁决庭范式（候选集+卷宗+段号钉证+不确定不连），不确定不连的默认对高难度判断天然更保守；§17.2 阶段 1 的人工抽查专门覆盖这一风险点 |
| `SceneAnchor.unresolved_slots` 被误用为绑定依据 | 该字段只是辅助校验（"回指声明人数是否吻合"），若实现时被误用为独立判据，等同于外表纪律的同类错误 | §13.2 判定流程第 3 步明确该字段不参与候选判别裁决的输入条件，只用于前置检索是否值得发起裁决调用；§17.1 判据 5 的机械字段扫描方式可平行覆盖 |
| 外表结构化字段被下游误用为绑定依据 | §13.3 硬规则的核心风险——一旦被绕过就是下一个赵武刚 | 提示词与代码双重约束（§13.3"落地方式"）；§17.1 判据 5 机械扫描 |
| 与第一期认知卡的候选集扩展存在耦合风险 | §13.4 描述的候选集扩展（∪ 指代链确认节点）若实现时误把"未核验"的指代链节点也纳入候选集，会重蹈 1.8.3 覆辙 | 候选集只能纳入指代链中已经过完整裁决闸核验通过（`connection_basis` 三种硬证据之一 + 逐字钉证）的节点，绝不能纳入"申报中/待核验"的节点；这条边界与 §13.1"严禁"清单同一纪律 |

**回滚**：第二期全部改动均为新增字段/新增函数，不删除、不改写第一期或身份文档已有字段
语义。`Character.reference_chain`/`appearance_features`/`Bible.scene_anchors` 为空
列表时，候选集扩展逻辑应优雅降级为"候选集只有 `aliases` 命中的部分"（即当前第一期行为，
不报错、不阻断）——因此即使第二期回填从未运行过，第一期裁决闸行为完全回退到本次修订前的
已验证现状。回滚成本低，可按文件逐个 revert。
