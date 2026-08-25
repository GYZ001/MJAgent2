# 人物认知层设计：状态事实、时间语义与判别式提问（架构设计）

日期：2026-08-25。状态：**设计定稿，本文档只记录决策与数据结构，不包含任何代码/数据改动**；
第 37 轮回归正在跑，代码指纹护栏会因任何 `app/` 改动停轮，本任务只写文档。所有代码引用均逐条
`grep` 核对至当前工作树（`main` 分支，HEAD `7959b48`），行号如与未来改动后的文件不一致，以
`grep -n` 复核结果为准。

本文档是 `docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md`（下称"身份文档"，已实施：视觉实体与命名
权威解耦，`Character.aliases` 已落库）的**后继层**。身份文档解决"这是谁、该用哪张脸"；本文档
解决"这个人现在是什么状态、值不值得投入资源"。两者共用同一套证据锚点纪律，互不重复。

## 0. 一句话问题

原著读者推进到新章节时，脑子里带着"李诗琪是血妖宗的、王有材也是血妖宗的"这类关系性知识，据此
瞬间判断"王师弟"说的是王有材而不是同姓的王腾飞。当前系统没有对应的长期记忆——每次判别都只能
看当前这一章节的文本，看不到几十章之前建立、几十章之后仍然生效的归属/关系事实。

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

## 4. 三层设计

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
