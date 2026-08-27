# 外观锚点出处方案（王有材事故复盘 + 设计）

状态：设计文档，未改动任何代码/数据。项目 `proj_3ac0b627fa46`《我欲封天》，角色「王有材」。

本文档所有原文引用均于 2026-08-26 用只读连接（`sqlite3.connect('file:data/manju.db?mode=ro', uri=True)`）
直接查询 `data/manju.db` 验证，未采信任何转述。

---

## 0. 与另一份方案的依赖关系

另一份文档 `logs/bible_roster_criterion_plan.md`（人物谱重要度判据）可能会讨论"王有材是否
本就不该进人物谱"。本文档不重复设计该问题，只指出依赖关系：**本文档的方案不依赖该判据的
结论**——无论王有材该不该在名单里，只要人物谱收了他（现状如此，且他在全书里反复以血妖宗
弟子身份出场，见第 1.3 节，不是纯路人），他的 `appearance_canonical` 就必须诚实反映"原文
有没有写"，这是本文档要解决的问题，与"该不该收他"是两个独立轴。

---

## 1. 根因

### 1.1 一句话根因

`app/stages.py:4197`（及 `:2128`、`app/portraits.py:9019`、`:9107` 的同构副本）在同一段 prompt
里同时放了两条规则——**正向配额**「appearance_canonical 必须包含 1 个标志性特征」和**兜底
授权**「原著未描写的部分，按题材合理补全并保持内部一致」——对一个原文毫无外貌描写的角色，
这个组合等价于强制模型编造：配额不允许模型交白卷，授权告诉模型编造是合法操作；模型选择的
解法是"就近取材"，把同一场景里另一个角色的特征安到了这个角色头上，而不是凭空捏造一个无中
生有的特征——这解释了为什么编造的特征（胖/梦游啃咬）能在原文里精确"对上号"（属于另一个人）。

personality 字段的污染（"灵气都汇聚在牙齿上"）是这条编造链的下游产物：模型为了让自己编的
"牙齿锋利"显得合理，又追加编造了一条修炼设定来自圆其说——这不是独立的编造源头，是同一次
生成调用里对上文编造内容的自我合理化。

### 1.2 校验为什么没拦住

- 落库前唯一生效的业务校验 `app/validators.py:6226 validate_bible()` 对 `appearance_canonical`
  只检查 `20 <= len <= 80`（`app/validators.py:6240-6245`），49 字必过。
- `app/refs.py:75` `production_appearance_anchor()` 与 `app/refs.py:80`
  `missing_production_appearance_dimensions()` 名字像内容校验，实测（`git show 59a31fa --
  app/refs.py`）已在 2026-08-09 commit `59a31fa` 被有意改为空操作：前者现在只做
  `normalize_prompt_text` 标点归一化，后者无条件 `return []`。该 commit 同时删除了
  `app/validators.py` 里配套的 13 行校验代码，并新增了
  `tests/test_bible_prompt_and_precheck.py:25-54` 两条测试，专门断言"appearance_canonical
  含隐私部位词/主观词时 `validate_bible` 依然通过"——这是有意的架构决策，符合 CLAUDE.md
  「禁止黑白名单」，不是误删。
- **即使旧词表没被删，这次事故也拦不住**：我读了 `git show 59a31fa -- app/refs.py` 的完整
  diff，旧的 `_NON_STATIC_APPEARANCE_RE`/`_SEXUALIZED_BODY_EMPHASIS_RE` 等五个正则覆盖的是
  "隐私部位/主观气质/暴露穿着"，词表里没有"梦游""啃咬""锋利突出"这类词，且这类行为描写是
  开放集，任何固定词表都覆盖不到。**所以这不是"该有的过滤被误删"，而是"词表退役后，'这段
  外观到底有没有原文依据'这个问题从未有过任何形式的校验"**——退役前后，这道题都没人管过。
- `app/schemas.py:178` `appearance_canonical: str` 本身无任何字段级约束（Pydantic 层面
  就是自由文本）。

### 1.3 一个必须先澄清的事实偏差（未验证 → 已验证，结论有出入）

任务给出的既定事实是"王有材只在第1章出场（第19章被交代摔死）"。我用只读连接扫了全书
`王有材` 出现的全部 29 个章节（`idx` 1/10/19/45/47/71/72/120/128/130/187-199/265/298/299/
301/311/315/842/883/1562，全书共 1616 章），发现**这个具体表述不准确**：王有材确实在
第 19 章被交代"被风吹下山崖，两个月没找到尸体"，但小说后续（第 130、187-199、265、298-315、
842、883、1562 章）反复让他以"血妖宗弟子"身份**活着出场**，有对话、有互动，第 45 章孟浩
甚至对王伯撒谎说"有材哥还活着"，第 120 章还有一处"与六七年前的少年王有材一模一样，颇为
诡异"的伏笔，暗示这是刻意留的悬念/复活桥段，不是误记。

这个偏差**不影响本文档的核心结论**：我逐一读了这 29 章里王有材出现的上下文（含关键词过滤
后的人工复核），除了第 130 章一句战后"狼狈、满身伤痕"（战斗损伤，非固定外观）和第 193 章
"样子还是七八年前的模样"（无具体视觉信息）之外，**全书任何一处都没有给王有材本人的发型、
体型、着装这类固定外观描写**——"全书没有外貌描写"这个判断依然成立，只是"只在第1章出场"
不成立。这个偏差对方案设计的唯一影响是：证据核验的候选范围不能只框定在人物谱生成时读到的
头部章节窗口内，必须覆盖全书（本来的设计也是如此，见第 3 节），不需要因此调整方案。

也请注意：`BIBLE_HEAD_CHAPTERS=10`（`app/stages.py:1924`）意味着首版人物谱生成时模型完整
通读的只有前 10 章，第 19 章的死亡交代不在这个窗口内——模型当时看到的王有材，就是第 1 章
那个被救、第 10 章被提及一次姓名的配角，窗口内确实没有任何外貌线索，这与"零外貌描写"的
结论是一致的，只是解释了模型为什么会觉得"这是个需要我补全外观的重要角色"（他在【必收角色
名单】里，因为前 10+10 章内出现次数达标，见 `app/stages.py:2022 _recurring_character_names`）。

---

## 2. 新契约与改写后的 prompt

### 2.1 核心设计原则（对应任务方向，逐条论证后采纳）

**辨识性特征必须有出处，通用形态可以生成，两者分开记账**——采纳，理由：

- 通用形态（性别年龄感/发型发色/服装款式颜色）是"具体化"，不是"编造事实"：原文哪怕没写
  王有材几岁，"十五六岁少年"这种题材惯例设定不会制造一个可被"移花接木"的虚假事实——不存在
  另一个角色的年龄段被错误安在他身上这种攻击面，因为年龄段本身就是软性设定。
- 标志性特征（伤疤/体型极端值/习惯动作痕迹）是"具体事实断言"：一旦写上，就等价于宣称"原文
  设定了这个人有这个特点"，后续台词、分镜、定妆照都会把它当真实设定使用（本例中 personality
  字段就把它当真实设定"解释"了一遍）。这类断言必须要么有据可查，要么不写。

**不去分类哪句是"辨识性"哪句是"通用"，而是算覆盖信号**——采纳，理由：分类本身需要对
appearance_canonical 自由文本做子句级语义切分和归类，这是一个开放语义问题（"敦实"算通用
体型还是标志性特征？取决于题材惯例，不存在固定阈值），做不到不出错的自动分类，等于变相
重造一个隐藏的分类词表。改成"模型自己决定要不要在这句话里放一个需要举证的断言，放了就必须
配证据"，把分类责任留给模型（它最清楚自己写的是通用形态还是具体断言），代码只做机械的
"证据是否成立"核验，不替模型分类。

### 2.2 Schema 变更（`app/schemas.py`）

新增一个与 `CharacterAlias`（`app/schemas.py:112-126`）、`CharacterAffiliation`
（`:129-157`）完全同构的证据锚点类型，复用同一套"逐字引句 + 章节序号"范式，不新造校验哲学：

```python
class AppearanceEvidence(BaseModel):
    """appearance_canonical 里"标志性特征"部分的证据锚点：模型申报 + 代码核验后才允许保留
    对应文字（不确定不登记，登记失败时该特征需要从 appearance_canonical 里退回通用形态，
    不是拒绝整条角色——结构与 CharacterAlias 完全同构，见 app/schemas.py:112）。"""

    evidence_chapter_index: int   # 原著章节序号（对应源章节的 idx 字段）
    evidence_quote: str           # 原文逐字短句，必须原样照抄；核验规则见
                                   # app/stages.py._appearance_evidence_verified
```

`Character`（`app/schemas.py:175-195`）新增字段：

```python
    # 外观标志性特征的证据锚点（王有材事故修复新增，见
    # logs/appearance_provenance_plan.md）。只对 appearance_canonical 里"通用形态之外的
    # 标志性特征"部分需要；通用形态允许合理设定，不需要证据。旧 bible_json 没有这个键时
    # default_factory 给空列表，反序列化不受影响；空列表是诚实默认值（"这个角色没有可验证
    # 的标志性特征"），不是缺陷信号，不应触发任何拦截。
    source_evidence: list[AppearanceEvidence] = Field(default_factory=list)
```

### 2.3 改写后的 prompt（正面陈述，可直接用）

#### 2.3.1 `app/stages.py:4197`（`generate_bible` 主生成，第 2 条规则）

原文（现状）：

> 2. appearance_canonical 是该角色的"固定外观锚点串"：40~60 字，必须包含 性别年龄感/发型
> 发色/服装款式与颜色/1 个标志性特征。只写常规完整着装、中性站姿下可直接看见并能跨镜稳定
> 复现的静态形态：五官、发型、体型、外层服装、可见配饰或面部标记。不写性格、欲望、气质、
> 眼神行为、对他人的注视方式，不得写裸体、内衣、私密身体部位或必须暴露身体才能看见的特征。
> 原著未描写的部分，按题材合理补全并保持内部一致。

改写为：

> 2. appearance_canonical 是该角色的"固定外观锚点串"：40~60 字，只写常规完整着装、中性
> 站姿下可直接看见并能跨镜稳定复现的静态形态：五官、发型、体型、外层服装、可见配饰或面部
> 标记。不写性格、欲望、气质、眼神行为、对他人的注视方式，不得写裸体、内衣、私密身体部位
> 或必须暴露身体才能看见的特征。
>    写作分两层：
>    - 通用形态（性别年龄感/发型发色/服装款式与颜色）：原文没有直接描写时，允许你按题材、
>      身份、场景合理设定一个具体、可跨镜复现的写法（例如"十五六岁""粗麻杂役衫"），这是
>      具体化不是编造事实，不需要举证。
>    - 标志性特征（伤疤、体型极端值、面部标记、习惯动作留下的可见痕迹等）：只有原文对**这个
>      角色本人**确实写过这样的描写，才写这一条，且必须逐字取用原文说法；原文没有这类描写
>      时，appearance_canonical 到通用形态为止，把剩余字数用于把通用形态写得更具体（例如
>      补充材质、颜色细节），不要为了凑一个"标志性"而编造。判断"是不是这个角色本人"时要
>      小心：同一段落里描写"另一个人""其余几人""身边的人"这类指代对象的内容，不属于这个
>      角色本人，不能借用到他身上。
>    - source_evidence：数组，只用来给"标志性特征"这一层举证（通用形态不需要）。每条给
>      evidence_chapter_index（原文分块【】块头里的章节序号）与 evidence_quote（支撑该
>      特征的原文逐字短句，控制在 40 字以内、必须原样连续照抄、不得跨句拼接或用省略号
>      连接多处），且这句引文里必须能直接读出是在描写这个角色本人（角色的正式姓名或已
>      确认别名要出现在这同一句短引文里，不是出现在原文的其他地方）。原文没有可举证的
>      标志性特征时，source_evidence 就是空数组——这是诚实的默认值，不会因此被拒绝。

#### 2.3.2 `app/stages.py:2128`（`_supplement_bible_characters` 人物谱补录）

原文（现状）：

> 2. appearance_canonical 是固定外观锚点串：{MIN}~{MAX} 字，必须包含 性别年龄感/发型发色/
> 服装款式与颜色/1 个标志性特征。只写常规完整着装、中性站姿下可直接看见并能跨镜稳定复现的
> 静态形态；不写性格、情绪、眼神行为，不得写裸体或私密身体部位。原著未描写处按题材合理
> 补全并保持内部一致。

改写为（与 2.3.1 同一套分层逻辑，压缩版）：

> 2. appearance_canonical 是固定外观锚点串：{MIN}~{MAX} 字，只写常规完整着装、中性站姿下
> 可直接看见并能跨镜稳定复现的静态形态；不写性格、情绪、眼神行为，不得写裸体或私密身体
> 部位。通用形态（性别年龄感/发型发色/服装款式颜色）原文没写时可按题材合理设定，不需要
> 举证；是否再写 1 个标志性特征取决于原文对这个角色本人是否确有描写——有就写且逐字取用
> 并在 source_evidence 里举证（evidence_chapter_index + 40 字以内的原文逐字短句，短句里
> 要能直接读出是在写这个角色本人，不是同段落里的其他人），没有就不写，不必凑数。

#### 2.3.3 `app/portraits.py:8994/9019/9107`（`assess_new_character` 身份合同/主规则/重试）

`:8994` 身份合同分支原文："原文未给出的可视字段按项目画风作保守补全。" 改为：

> 原文对这个角色本人确有可视描写的字段，逐字取用；原文没写的可视字段，通用形态（年龄段/
> 发型发色/服装款式颜色）按项目画风与身份合理设定，不写需要举证的标志性特征。

`:9019` 主规则原文："appearance_canonical 是……40~60 字，须含 性别年龄感/发型发色/服装
款式与颜色/1 个标志性特征；只写视觉可见信息，不写性格。原著未写处按画风（{style}）合理
补全并保持内部一致。" 改为（与 2.3.1 同构）：

> appearance_canonical 是"固定外观锚点串"：40~60 字，只写视觉可见信息，不写性格。通用
> 形态（性别年龄感/发型发色/服装款式与颜色）原文没写处按画风（{style}）合理设定，不需要
> 举证；标志性特征只有原文对这个角色本人确有描写才写，且要在 source_evidence 里给出
> evidence_chapter_index 与 40 字以内的原文逐字短句（短句本身要能读出是在写这个角色
> 本人）；原文没有就不写，source_evidence 留空数组即可，不是缺陷。

`:9107` 重试指令原文："请重写为 {MIN}~{MAX} 字、含 性别年龄感/发型发色/服装款式与颜色/1
个标志性特征 的完整外观锚点，并固定 important=true。原著未写处按画风保守补全，只写视觉
可见信息。" ——这一条连同它所属的整个重试分支，在新设计里语义变了（见第 5 节改动清单的
`_build_verdict`/重试逻辑重写），不再是"外观太薄就要求凑够特征"，而是"claimed 的证据核验
不通过，要求要么换一条真实可查的证据、要么把这个特征去掉只写通用形态"，具体措辞见第 5 节
对应改动条目，不在此处重复给整段文案（因为它需要动态嵌入具体的失败证据，不是静态模板）。

#### 2.3.4 `app/stages.py:4303`（场景圣经 `scene_canonical`，P1，见难点C）

原文同构问题，改写方向一致，具体文案作为 P1 工作项，不在本次 P0 范围内给出定稿文案（理由
见第 4 节难点 C）。

---

## 3. 后端核验流程

### 3.1 核验函数（新增，建议放在 `app/stages.py`，紧邻 `_alias_declaration_verified`
第 2227 行，复用同一批工具函数：`_chapters_by_idx`(`:2190`)、`_quote_comparison_variants`
(`:2212`)）：

```python
# 见 logs/appearance_provenance_plan.md 难点A：真实回归数据实测，跨人物借用特征的最短
# 可行引句需要 44 字（王有材↔小胖子同句案例，见该文档第 4 节难点A的逐字测算），40 字
# 上限逼迫模型只能引用与角色本人名字直接相邻的短句，无法把名字和别人的描写塞进同一条引句。
APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS = 40


def _appearance_evidence_verified(
    chapters_by_idx: dict[int, str],
    anchor_texts: set[str],   # 角色规范名 + 已确认别名（与 CharacterAlias 核验取同一份 roster 口径一致）
    evidence_chapter_index: int,
    evidence_quote: str,
) -> bool:
    """标志性特征证据核验：结构性判据，不做任何语义分类（禁止黑白名单式修复）。

    两个条件必须同时成立：
    1. evidence_quote 长度 <= APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS，且是
       evidence_chapter_index 对应章节原文的逐字连续子串；
    2. 角色规范名或已确认别名，出现在这条引句本身内部（不是出现在整章的其他位置）——
       这是与 _alias_declaration_verified 条件 3（整章共现）的关键区别：外观证据要求
       "名字和描写在同一条短引句里"，因为整章共现挡不住"同一句里名字属于A、描写属于B"
       这种跨人借用（见难点A的实测案例）。
    """
    quote = (evidence_quote or "").strip()
    if not quote or len(quote) > APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS:
        return False
    chapter_text = chapters_by_idx.get(evidence_chapter_index, "")
    if not chapter_text:
        return False
    for candidate in _quote_comparison_variants(quote):
        if candidate in chapter_text and any(
            anchor and anchor in candidate for anchor in anchor_texts
        ):
            return True
    return False
```

### 3.2 接入点

在 `generate_bible`（`app/stages.py:4147`）内部 `validate_authoritative_bible` 闭包
（`:4238-4243`，已持有 `chapters` 参数）里追加：

```python
def validate_authoritative_bible(candidate: Bible) -> list[str]:
    if visual_style_prompt:
        candidate.world.visual_style_canonical = visual_style_prompt
    errors = validate_bible(candidate)
    errors += _validate_appearance_evidence(candidate, _chapters_by_idx(chapters))
    return errors
```

`_validate_appearance_evidence(bible, chapters_by_idx)`：遍历每个角色的 `source_evidence`，
**只对非空条目**逐条跑 `_appearance_evidence_verified`；核验失败才追加一条 errors（信息里
点名是哪个角色第几条证据、失败原因，驱动 AgentLoop 已有的修复重试，见 `app/stages.py:4221-
4236` 的 `AgentLoop`/`AgentLoopPolicy(repair_all_blockers=True)`，不新建重试机制）。**空
`source_evidence` 数组永远不产生 error**——这是与 `_alias_declaration_verified` 使用方式
的关键差异：别名不确定就不登记，本来就不强制每个角色必须有别名；外观同理，不确定/没有就
不写标志性特征，但这必须是零成本的默认路径，不能让 AgentLoop 因为"这个角色没有可举证的
特征"而反复重试甚至耗尽 `max_iterations` 后判角色整体失败——那会把"老实说没有"变成一个
比"编一个能蒙混过关的"更差的结果，正好复刻了本次事故的激励结构。

校验层级职责保持现状不变：`validate_bible`（`app/validators.py:6226`）继续只做长度/结构
校验，不引入内容核验（`_validate_appearance_evidence` 是独立函数，只在 `generate_bible`
这一处有 `chapters` 上下文的地方调用，不塞进 `validate_bible` 内部——与 `_alias_
declaration_verified` 同样不在 `validators.py` 里的架构一致）。

`_supplement_bible_characters`（`app/stages.py:2103`）与 `assess_new_character`
（`app/portraits.py:8985`）同样接入 `_appearance_evidence_verified`，核验失败时的处理方式
见第 5 节改动清单。`assess_new_character` 需要一处前置基础设施改动（见难点C 第 4 类、第 5
节改动清单）：`_forward_fragments`（`app/portraits.py:528`）目前把多章原文拼接成一整块
文本喂给模型，丢失了章节边界，模型没法准确申报 `evidence_chapter_index`；需要仿照同文件
`_future_chapter_context`（`:545-581`）已经在用的 `【第 N 章】` 分块标记方式重写，并让
`_appearance_evidence_verified` 核验时用同一批被查询到的 `rows` 建 `chapters_by_idx`。

---

## 4. 难点 A-F 逐条结论

### 难点 A：这个方案拦得住这次的事故吗？

**核心测算（已用只读连接验证，见第 1.3 节引用的第 1 章原文）**：

第 1 章原文这一句（`data/manju.db` chapters.idx=1）：

> 当他的目光落在王有材身上时，看到了他身边的两个少年，一个是那虎头虎脑的家伙，另一个则是
> 白白净净身子较胖，这二人此刻都身子颤抖，神色恐惧，似乎快哭了出来。

这是原文里唯一同时出现"王有材"和"胖"的句子（全书 29 个出现王有材的章节里逐一核对过，
其余位置"王有材"和"胖"字都不在同一句/同一段），是模型最可能拿来"钻空子"的证据源。我逐字
定位了"王"字（第 8 位）到"胖"字（第 51 位）的位置，从"王有材"三字开头到"较胖"结尾，最短
连续引句需要 **44 个字符**。

- **40 字上限能拦住**：这句话本身、以及任何从"王"开始到覆盖"较胖"为止的连续子串，长度
  都 ≥ 44 字，超过 `APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS=40`，直接被条件 1 拒绝，不会走到
  "名字是否在引句内"这一步。**结论：拦得住这次事故的具体触发路径。**
- 我推演了另外两种钻空子路径，逐个说明：
  1. 模型缩短引句到 40 字以内、但跳过中间内容只留首尾（例如"王有材身上时……白白净净身子
     较胖"，中间用省略号接起来）——**拦得住**：条件 1 要求 `evidence_quote` 是原文的
     **连续**逐字子串（复用 `_quote_comparison_variants`/子串匹配，不支持跳跃拼接），
     跳跃拼接的引句在原文里找不到逐字命中，直接失败。
  2. 模型换一条完全不同的句子，例如拿第 130 章"血妖宗的少年王有材与宋佳，他二人在出现后，
     都极为狼狈，身体满是伤痕"来编"满身伤痕"这个特征——**拦不住，且这次拦不住是合理的**：
     这句话确实是在写王有材本人（"他二人"直接指代"王有材与宋佳"），角色名与描写在同一条
     不超过 40 字的连续原文里，核验会通过。但这本来就不是误判——这句话真的是原文对王有材
     本人的描写，只是它描述的是"战斗后狼狈"这种临时状态而非固定外观，模型如果把它写成
     "常年伤痕累累"就等于把临时状态曲解成固定锚点，这是**质量问题**（证据真实存在，但
     support 的断言超出了证据本身能证明的范围），不是**归属错误**（安在别人头上）。本
     方案的核验只保证"证据真实存在且指向这个人"，不保证"断言的强度与证据的强度匹配"——
     这是明确的已知限制，见第 8 节。
  3. 模型引用一条只有名字、没有实际描写内容的句子（例如第 10 章"小胖子、王有材、还有那
     虎头虎脑的少年，当初我们四人被一起带上靠山宗"），然后在 appearance_canonical 里
     自己写"胖"——**拦不住这个特定组合的语义关联，但后果有限**：代码核验只能证明"这条
     引句真实存在且提到了这个角色"，不能证明"appearance_canonical 里写的内容真的是这条
     引句能支撑的内容"（这是一个开放语义匹配问题，做不到）。这是本方案最大的已知限制，
     见第 8 节；缓解措施是 P1 的模型二次裁决层（见下）。

**结论**：P0 的纯代码结构核验能确定性拦住"本次实际发生的事故"（有测算证明），能拦住
"跳跃拼接证据"这类变体，但**拦不住"引用一条真实存在但内容无关的证据来支撑一个编造的断言"
这一类更狡猾的钻空子方式**——这是诚实的已知限制，不是本方案能靠结构判据彻底解决的问题。

**P1 缓解（复用既有裁决庭范式，非本次 P0 必须）**：`app/stages.py:3032
_alias_evidence_resolution` 已经实现了"模型申报 → 代码检索候选人名单 → 第二次独立模型
调用裁决'这条引句到底在描述谁'"的完整范式（`_alias_verdict_call`/`_alias_verdict_
candidates`），且该函数的 docstring 里明确记录过一次真实误判案例（`app/stages.py:3093`
就直接点名"第 189 章'王有材默默站起身站在孟浩身后'"）。P1 可以让外观证据核验复用同一个
裁决庭：把 evidence_quote 所在段落交给一次独立模型调用，问"这句话描述的可视特征，说的是
候选人名单里的谁"，只有裁决结果精确指向 true_name 才登记。这能把上面第 3 种钻空子方式也
拦住，代价是每条外观证据多一次模型调用（成本按用户既有备忘"HiAgent 模型调用免费"不构成
阻碍，只有时间成本）。

### 难点 B：0 引句的角色怎么办？

三个方案评估：

1. **仍然生成但打标记**：appearance_canonical 只写通用形态，`source_evidence=[]`，落库
   显式记一个"外观全部由模型生成"的覆盖信号，界面可见。
2. **不生成定妆照，分镜时按群演处理**：退回 `app/production/storyboard_pack.py:548-559`
   那套"素材库没有标准外观，自行确定并跨段自洽"的群演机制，不给这个角色建正式人物卡。
3. **卡住等人工**：生成流程在此暂停，进入待审队列。

**采纳方案 1**，理由：

- 王有材本身是这个方案要拒绝的反例——他是【必收角色名单】成员（原文出现次数达标，见
  `app/stages.py:2022 _recurring_character_names`），且经第 1.3 节验证，他在全书后续
  反复以命名角色身份出场，不是路人。方案 2 会把"这个角色缺一个可举证的外观特征"这个
  局部问题，用"降级成没有身份的群演"来解决，这是用一个不相关的轴（叙事重要度）去修一个
  不相关的问题（外观真实性）——CLAUDE.md 明确警告过这类范围蔓延。而且 0 引句是**绝大多数
  次要角色的正常状态**（大部分小说不会给每个配角写外貌），把"正常"当"异常"处理会让方案 2
  在几乎所有项目上高频触发，产生的破坏（大量本该稳定的角色失去跨集定妆照）远大于它防住的
  风险。
- 方案 3（卡住等人工）同样因为"0 引句是正常状态而非异常"而不成立——如果每个没有外貌描写
  的次要角色都要人工介入，人物谱生成会在几乎每个项目上频繁停摆，这是不成比例的摩擦，且
  违反 CLAUDE.md「拦住用户时必须给出路」的反面教训——不是"给了路但路难走"，而是"把常态
  当异常来拦，路本身就不该设在这里"。
- 方案 1 的下游影响：这类角色的定妆照会是诚实的通用形态（年龄段/性别感/发型/服装），不会
  再出现"编一个具体但张冠李戴的特征"这种更危险的失败模式；界面标记让用户能一眼看出"这几张
  定妆照的细节是 AI 定的，不是原文写的"，用户如果对某个角色不满意，可以走既有的人工编辑
  流程（`frontend/src/pages/BiblePage.tsx:963 saveCharacterDraft`）补充自己认可的细节，
  这个编辑入口已经存在，不需要新建。

### 难点 C：这 10 处兜底授权哪些要改、哪些保持？

| # | 位置 | 判断 | 理由 |
|---|------|------|------|
| 1 | `app/stages.py:2128` 人物谱补录 | **改**（P0） | 与主生成同构，同样的配额+授权组合，同样能张冠李戴 |
| 2 | `app/stages.py:4197` 角色圣经主生成 | **改**（P0） | 本次事故的直接触发点 |
| 3 | `app/stages.py:4303` 场景圣经 `scene_canonical` | **改**（P1，非本次必须） | 同构问题确实存在，但风险量级不同：场景的"标志性陈设"张冠李戴（比如把 A 场景的一个建筑安到 B 场景头上）不会像人物特征那样制造一条被写进 personality 的虚假叙事事实，冲击面更小；且 `Scene` schema（`app/schemas.py:204-230`）目前没有证据锚点字段，改动需要新增字段+改 prompt+改核验，是与本次 P0 同构但独立的一份工作量，不在本次范围内一并做，留作后续用同一套机制平移 |
| 4 | `app/portraits.py:8994` 身份合同分支 | **改**（P0） | 触发条件是"身份消歧已确认真名"，属于正式角色路径，与主生成同风险 |
| 5 | `app/portraits.py:9019` 主规则 | **改**（P0） | 同上 |
| 6 | `app/portraits.py:9107` 重试指令 | **改**（P0，且逻辑重写而非文案微调） | 现在的重试要求"重写为含标志性特征的完整外观"，在新契约下这个要求本身就是错的——重试应该允许"去掉这个特征只写通用形态"作为合法的重试结果，不能继续把"必须有特征"当作重试目标（否则会在验证失败时逼模型换一个新的编造特征，而不是老实退回通用形态，重现同一个事故结构） |
| 7 | `app/production/storyboard_pack.py:548-553` `_NO_CANONICAL_APPEARANCE_NOTE` | **不改** | 见下方统一说明 |
| 8 | `app/production/storyboard_pack.py:555-559` `_NO_CANONICAL_SCENE_NOTE` | **不改** | 同上 |
| 9 | `app/production/storyboard_pack.py:239-278` `SEEDANCE_DIALECT_INSTRUCTIONS` 内联重复 | **不改** | 同上 |
| 10 | `app/production/storyboard_pack.py:1032-1044` `rules` 列表版本 | **不改** | 同上 |

**第 7-10 项为什么不改（统一说明）**：我读了这四处的完整上下文（`app/production/
storyboard_pack.py:598-619 _enrich_asset_manifest_canonical_visuals`）。这套机制的触发
前提是**代码已经确认这个角色/场景在世界书里没有可用的标准外观**（`portrait_id` 未解析
到，或解析到了但 `character_portraits.appearance` 为空），触发对象是 `functional_extras`
（群演/一次性人物，天生不建正式人物卡）或未能解析到 portrait 的条目。这与本次事故的触发
条件完全不同：王有材是有正式人物卡、有 `appearance_canonical`、编造发生在**人物谱生成
这一步**，storyboard_pack 这四处从未被触发过（王有材有 `portrait_id`，能正常解析到
`character_portraits.appearance`，走的是"逐字沿用"分支，不是"自行确定"分支）。

这四处的 prompt 已经满足 CLAUDE.md「写清合法值从哪里来、必须逐字取用、确实没有时该写
什么」——它们明确区分"有标准锚点就逐字沿用"和"没有标准锚点才自行确定"两条路径，且强制
"同一角色/场景在本集/全集所有段落里保持同一套自定特征"（跨段自洽），这正是群演应有的
正确行为：群演不需要跨集稳定身份，但至少要在单集内不换脸。给群演也套用 `source_evidence`
机制没有意义——群演没有 Bible/Character 记录可以挂证据，若为了给群演也建证据链而给每个
一次性人物都建正式角色卡，等于取消了 `assess_new_character`（`app/portraits.py:8985`）
里"only important characters get identity cards"这道闸门存在的意义。

**遗留风险（不在本次方案修复范围，P2 记录）**：如果一个**正式人物谱角色**因为管道 bug
（不是因为他真的是群演）导致 `portrait_id` 解析失败或 `character_portraits.appearance`
意外为空，他会被 `_enrich_asset_manifest_canonical_visuals` 无声降级成走"自行确定"分支，
不会有任何信号提示"这本该是个有身份的角色，但现在被当群演处理了"。这是与本次事故不同的
另一类 bug（静默误分类，不是强制编造），CLAUDE.md「可见信号，不得静默通过」同样适用，
建议未来单独立项：在 `_enrich_asset_manifest_canonical_visuals` 里加一条信号——角色存在
于 `bible.characters` 但解析不到 appearance 时记一条告警，而不是无声落到 `_NO_CANONICAL_
APPEARANCE_NOTE`。本次不做，因为它与王有材事故的根因（bible 生成阶段的配额+授权组合）
无关，是完全独立的另一个问题。

### 难点 D：personality 的级联污染

**结论：本次 P0 只管 appearance，不把 source_evidence 机制扩展到 personality/speech_
style；但会移除促成这次级联的直接诱因，属于间接修复（推断，未独立验证）。**

推理链：`app/stages.py:4197` 一次调用同时产出 `appearance_canonical` 和 `personality`
两个字段。personality 里"灵气都汇聚在牙齿上"这句话，语义上明显是在为上文编造的"牙齿比常人
更锋利突出"找一个修炼设定上的解释——如果 appearance_canonical 里根本没有这个牙齿特征
（因为新契约下没有证据就不会写），模型就没有"需要圆的谎"，这条 personality 里的编造设定
大概率不会出现。**这个推断没有独立验证**（我没有条件重跑一次带新 prompt 的真实生成调用来
对照），如实标注为"未验证但推断成立"。

是否要给 personality/speech_style 单独设计出处约束，是一个**更大的问题**：personality 是
自由散文，充满"聪颖坚毅""爱碎碎念"这类主观性格描述，这类描述本来就不该要求逐字对应原文
（原文很少直接写"这个人很聪颖"，性格判断本来就是题材惯例下的合理推断，不是需要举证的事实
断言）。真正需要类似证据机制的，是 personality 里**混入的具体设定类事实断言**（比如"灵气
汇聚在牙齿上"这种，读起来像世界观设定而不是性格形容词），但"一句话是性格形容还是设定断言"
本身是一个比"appearance 里有没有标志性特征"更难的开放分类问题，不能用本方案的机制直接
平移。这需要独立立项设计（可能需要类似 `_alias_verdict_call` 的裁决庭模式来做语义判断，
而不是结构判据），本文档不展开，列入第 7 节 P2。

### 难点 E：已有数据怎么清

**清理清单（已核对齐全，见下表；本次仅设计，不执行）**：

| 位置 | 主键 | 需要动作 |
|---|---|---|
| `projects.bible_json` | `proj_3ac0b627fa46`（`bible_version=1`） | 修正 characters[name=王有材] 的 `appearance_canonical` 与 `personality` |
| `artifacts` (character_bible) | `art_9a44f7aa5ed8` v1 approved | `content_json` 内同一角色的两个字段需要同步修正 |
| `artifacts` (character_portrait) | `art_f027e7c4e69c` v1 approved, scope=`proj_3ac0b627fa46:王有材:1` | `content_json.appearance` + `content_json.prompt` 需要基于修正后的外观重新生成 |
| `character_portraits` | `portrait_b406ca2e78c9` | `appearance` + `prompt` 同上 |
| `character_portrait_views` | `pview_80229619b2a6`(front_full) / `pview_74403b67c35f`(profile) / `pview_396d456782e5`(three_quarter) | 各自 `prompt` 需要基于修正后的外观重新生成，图片需要重新生成 |
| 落盘图片 | `refs/王有材__candidate_7cdefd86d7b8.jpg`、`refs/views/王有材__profile__ep1__view_b7a3a71256a8.jpg`、`refs/views/王有材__three_quarter__ep1__view_f522cd19e141.jpg`（已核实三个文件确实存在，247KB/250KB/298KB） | 替换为基于新外观重新生成的图片 |

**执行方式（P0，人工走既有流程，不新建脚本）**：`frontend/src/pages/BiblePage.tsx:963
saveCharacterDraft` → `api.saveCharacter(projectId, characterName, ...)` 已经是"编辑单个
角色的 appearance_canonical/personality 并保存"的生产入口，王有材只有一个角色、一个项目，
用这条既有路径即可：人工核对原文后手动改写 `appearance_canonical`（通用形态，不写标志性
特征，因为原文确实没有）与 `personality`（去掉"灵气汇聚在牙齿上"这条编造设定），保存后
触发既有的定妆照重生成流程（三张图会被新生成的图替换）。这符合 CLAUDE.md「破坏性操作要有
原子性」——重生成走已有的"新产出确认成功后才替换旧指针"流程（生成失败时旧图/旧 prompt
不会被污染成半吊子状态，这是 `character_portraits`/`character_portrait_views` 现有落库
流程的既有行为，不需要新写）。

`artifacts` 表的 v1 记录是否会因为编辑走出新版本（v2）、还是原地patch，我**没有独立验证**
——需要在实际执行清理前，先确认 `saveCharacter` 对应的后端接口是否会新建 `artifacts` 版本
（若是，v1 应保留为历史存档并标 `superseded_by_artifact_id`，不应该直接改写已 approved 的
历史版本内容——CLAUDE.md 对"确认前先看清单"的要求同样适用于搞清楚这次编辑落在哪张表的
哪个版本上）。标注为"未验证，执行前需确认"。

**是否做成可复用入口**：

- **P0：不做**。这是一次性的单角色修复，走既有 UI 编辑流程成本最低，且是否要重生成图片
  这类决策本来就需要人工确认，不适合无人值守脚本处理。
- **P1：值得做一个只读审计脚本**（放 `scripts/`，遵守 CLAUDE.md workspace hygiene 规则），
  扫描 `manju.db` 里所有 `bible_json`/`character_bible` artifacts 中 `source_evidence`
  字段缺失（说明是新契约上线前的旧数据，本身不代表有问题，但值得抽查）或字段存在但全部
  条目未过核验的角色，产出一份"疑似受本次事故同类问题影响的角色清单"供人工复核，**不自动
  修改任何数据**。这类审计脚本本质是只读，风险低，价值是能发现王有材之外是否还有同类角色
  （本次没有条件扫描全部项目，未验证是否还有其他受影响角色，如实标注）。
- **P2：不做自动批量回填**。已 approved 的生产数据被脚本无人值守批量改写，风险与"确认前
  先看清单"的原则冲突，即使清单是脚本自动生成的，改写动作也应该逐条人工确认，不属于本次
  方案范围。

### 难点 F：两条空操作函数怎么处理

结论对两个函数**不同**，不能一概而论：

- `app/refs.py:75 production_appearance_anchor()`——**保留，不是名存实亡**。它现在的
  实现是 `normalize_prompt_text(anchor or "").strip()`，这不是空操作，是把"内容过滤"这个
  职责收窄成"标点/空白归一化"这一个真实存在的职责，且 `app/refs.py`/`app/stages.py:1795-
  1802`/`app/portraits.py:9056` 等六处调用方都在正确使用这个收窄后的职责（去重复标点、
  统一格式），docstring "Preserve the approved appearance contract without lexical
  filtering" 准确描述了它现在做什么。它不需要因为这次事故做任何改动。
- `app/refs.py:80 missing_production_appearance_dimensions()`——**删，不填回内容**。
  现状 `return []` 是真正的死壳：调用方 `app/portraits.py:9064`（`card_complete` 计算里
  `and not missing_production_appearance_dimensions(appearance)`，恒为 `True`，这个
  条件永远不生效）和 `app/portraits.py:9099-9107`（重试分支，`missing` 恒为 `[]`，
  `"、".join(missing) if missing else "长度或标志性特征不足"` 恒走 else 分支，"缺少可视
  维度：XX/YY"这条更具体的提示永远不可能被模型看到）——这是任务描述里"名存实亡"的准确
  含义，已核实。

  是否要填回内容：**不填回旧的关键词扫描逻辑**（旧实现按"年龄性别/外形/服装配饰"三类
  关键词判断是否覆盖齐全）。理由：这本质上和被 `59a31fa` 退役的内容过滤是**同一类问题**
  ——用固定词表判断一段开放文本"有没有提到某个维度"，模型换一种说法（"垂髫之年"而不是
  "少年"）就会被误判为"缺失"。虽然 `tests/test_bible_prompt_and_precheck.py:25-54` 那两条
  测试字面上只测 `validate_bible`（拒绝词表），没有直接测 `missing_production_appearance_
  dimensions`，但两条测试背后的设计意图——"不要用固定词表评判开放文本"——同样约束这个
  函数，恢复它等于表面遵守了测试字面意思、违反了测试背后的意图。

  **正确处理是删除这个函数和它的两个调用点，用新的证据核验逻辑取代它们原本想做的事**：
  - `app/portraits.py:9062-9067` `card_complete` 的判定条件从"长度 + 维度齐全 + role
    合法"简化为"长度 + role 合法"（维度齐全这件事在新契约下不存在了——通用形态没有
    "必须覆盖哪几类"的硬性要求，只要求诚实：有就写、没有不编）。
  - `app/portraits.py:9095-9109` 重试分支的触发条件从"card_complete 为 false"改为
    "source_evidence 存在未通过核验的条目"（新逻辑，见第 5 节改动清单），提示语从"缺少
    可视维度，请补全"改为"这条证据核验不通过（原因），请换一条真实可查的原文引句，或者
    去掉这个特征只写通用形态"——这不是把旧函数的空壳填回内容，是用第 3 节设计的新核验
    机制完全取代它原本的职责。

---

## 5. 改动清单（文件:行号 + 意图，不含 diff）

**P0**

1. `app/schemas.py:107-158` 区域新增 `class AppearanceEvidence(BaseModel)`，`Character`
   类（`:175-195`）新增 `source_evidence: list[AppearanceEvidence] = Field(default_
   factory=list)` 字段。
2. `app/stages.py:2227` 附近新增 `APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS` 常量与
   `_appearance_evidence_verified()` 函数（第 3.1 节给出的实现），复用 `_chapters_by_
   idx`(`:2190`)、`_quote_comparison_variants`(`:2212`)。
3. `app/stages.py` 新增 `_validate_appearance_evidence(bible, chapters_by_idx) ->
   list[str]`，只对非空 `source_evidence` 条目跑核验，失败才产生 error。
4. `app/stages.py:4238-4243` `validate_authoritative_bible` 闭包接入
   `_validate_appearance_evidence`（第 3.2 节给出的具体接入方式）。
5. `app/stages.py:4197` 第 2 条规则文案改写（第 2.3.1 节给出完整文案）。
6. `app/stages.py:2128` `_supplement_bible_characters` 第 2 条规则文案改写（第 2.3.2 节）。
7. `app/portraits.py:8994/9019` `assess_new_character` 身份合同与主规则文案改写
   （第 2.3.3 节）。
8. `app/portraits.py:9053-9093` `_build_verdict`：`card_complete` 判定去掉 `missing_
   production_appearance_dimensions` 依赖，改为长度+role 两项；新增对 `source_evidence`
   逐条跑 `_appearance_evidence_verified` 并把未通过的条目从 verdict 里剥离（appearance_
   canonical 对应文字是否需要联动裁剪，见下条限制说明）的逻辑。
9. `app/portraits.py:9095-9109` 重试分支重写：触发条件改为"存在未通过核验的
   `source_evidence` 条目"；重试提示语改为动态说明具体哪条证据、为什么不通过、以及"可以
   去掉该特征只写通用形态"这条合法出路（不是模板化静态文案，需要按第 3 节失败原因拼接）。
10. `app/portraits.py:528-542` `_forward_fragments` 改造：仿照同文件 `_future_chapter_
    context`(`:545-581`) 已用的 `【第 N 章】` 分块标记方式，让拼接后的 fragments 保留章节
    边界，并额外返回本次查询涉及的 `chapters_by_idx`（或原始 `rows`），供 `assess_new_
    character` 调用点在核验 `source_evidence` 时使用。
11. `app/refs.py:80-83` 删除 `missing_production_appearance_dimensions()` 函数本体（连同
    docstring）；`app/refs.py:75-77` `production_appearance_anchor()` 不改动。

**P1**

12. `app/stages.py:4303` 场景圣经 `scene_canonical` 规则文案改写；`app/schemas.py:204-230`
    `Scene` 新增同构的 `source_evidence` 字段；场景侧的 `_appearance_evidence_verified`
    等价函数（可直接复用同一个函数，只是 anchor_texts 换成场景规范名/别名）。
13. `app/stages.py:3032` 附近，参照 `_alias_evidence_resolution` 范式，为外观证据新增
    可选的模型二次裁决层（难点A 的 P1 缓解），需要新的裁决 prompt 与结果解析，工作量与
    `_alias_verdict_call` 相当。
14. `app/production/storyboard_pack.py:598-619` `_enrich_asset_manifest_canonical_
    visuals` 新增告警信号：角色存在于 `bible.characters` 但 `portrait_id` 未解析到时
    记一条日志/告警（难点C 遗留风险）。
15. `scripts/` 下新增只读审计脚本，扫描全库 `source_evidence` 缺失/全部未过核验的角色
    （难点E）。
16. `frontend/src/api.ts:2515-2527` `Character` 接口新增 `source_evidence` 字段声明；
    `frontend/src/pages/BiblePage.tsx:57-103`（`portraitAvailability`/`availabilityStamp`
    模式）新增一个独立的"外观来源"标记，`source_evidence` 为空时在角色卡（`:1268-1271`
    附近，与现有 `stamp` 同一位置）显示一个新的、独立于定妆质检 stamp 的提示（例如
    "外观：AI 生成，无原文依据"），复用现有 stamp 的视觉样式而不是新建一套组件。

**P0 依赖但本次不实现（记录依赖，不阻塞）**

- 项目王有材数据的实际清理（难点E），走既有 UI 完成，非代码改动，本文档不视为"改动清单"
  的一部分，需要在方案评审后单独执行。

---

## 6. 测试方案

### 6.1 会变红的现有测试

我检索了全仓库对 `missing_production_appearance_dimensions`/`production_appearance_
anchor`/`contains_non_production_appearance` 的引用：

- `tests/test_portrait_prompts.py:9/45/59/70` 只用到 `production_appearance_anchor`，
  本次不改这个函数，**不会变红**。
- `tests/test_bible_prompt_and_precheck.py:25-54` 两条测试只依赖 `validate_bible`/
  `Bible`/`Character`，本次不改 `validate_bible` 本身的长度校验逻辑，**不会变红**——但
  这两条测试的存在会继续约束"不能在 `validate_bible` 或它调用的任何函数里恢复关键词过滤"，
  第 5 节改动清单第 8 项（`card_complete` 简化）必须遵守这一点：新的 `card_complete`
  判定不能引入任何形式的关键词扫描。
- 没有找到任何测试直接调用 `missing_production_appearance_dimensions`（删除它不会让
  现有测试失败，只是需要同步删掉/改写它在 `app/portraits.py` 里的两处调用点，否则会
  `NameError`）。
- `app/multiview.py:23/204` 调用 `production_appearance_anchor`，未改动这个函数，**不
  受影响**；但如果 `Character.appearance_canonical` 后续因新契约变短（去掉标志性特征后
  更偏向通用形态），需要跑一遍 `tests/test_multiview_keyframe_qa.py`（`git show 59a31fa`
  diff 里出现过这个文件，说明它对 appearance 相关改动敏感）确认没有对特定长度/内容的
  隐式假设。

**结论：本次改动不会让任何现有测试直接变红**，前提是第 5 节改动清单第 8-9 项（`card_
complete`/重试逻辑）严格不引入关键词判据。这一点需要在实现阶段用 `py scripts/verify.py
--full` 跑全量确认（本文档未执行任何代码改动，这条是给实现阶段的验收要求，非本次已验证
结论）。

### 6.2 新增测试

参照 `tests/test_character_alias.py` 的既有结构（`_alias_declaration_verified` 的测试
组织方式：`test_alias_verified_when_quote_hits_and_cooccurs`、`test_alias_rejected_
when_quote_not_verbatim_in_chapter` 等），为 `_appearance_evidence_verified` 建一组
同构测试：

1. `test_appearance_evidence_verified_when_quote_hits_and_name_in_same_span`——正例。
2. `test_appearance_evidence_rejected_when_quote_exceeds_length_cap`——用本文档难点A
   实测的 44 字王有材/小胖子真实句子（或其可控子串）做**回归测试**，直接复现本次事故的
   触发条件，断言核验拒绝。这条测试的价值是"用真实项目数据验收"，符合任务要求。
3. `test_appearance_evidence_rejected_when_name_not_in_quote_itself`——引句真实存在
   但角色名不在引句内（例如引用第 10 章"小胖子、王有材……"这种纯名单句，断言核验拒绝，
   对应难点A 推演的第 3 种钻空子路径的可验证子集）。
4. `test_appearance_evidence_rejected_when_quote_not_verbatim_in_chapter`——引句非
   逐字子串。
5. `test_appearance_evidence_rejected_when_chapter_index_wrong`——章节号错。
6. `test_appearance_evidence_accepted_with_short_battle_damage_quote`——用第 130 章
   "他二人在出现后，都极为狼狈，身体满是伤痕"这条真实存在、确实指向王有材本人的短句做
   正例，明确这类"证据真实但断言强度存疑"的情况**按设计会通过**（对应难点A 第 2 种
   推演结论），测试名和注释要写清楚这是已知限制的具体化，不是遗漏。
7. `test_character_appearance_evidence_default_empty`——`Character` 反序列化旧
   `bible_json`（无 `source_evidence` 键）时不报错，默认空列表（同构于 `tests/test_
   character_alias.py:96 test_old_bible_json_without_aliases_field_loads_
   compatibly`）。
8. `test_validate_appearance_evidence_empty_list_never_errors`——`source_evidence`
   为空数组时 `_validate_appearance_evidence` 恒返回 `[]`，防止难点B 讨论过的"老实没有
   反而被拦"回归。
9. `test_bible_prompt_no_longer_requires_mandatory_signature_trait`——静态检查改写后的
   `app/stages.py:4197` prompt 文本里不再包含"必须包含……1 个标志性特征"这类强制表述
   （字符串层面的回归测试，防止未来有人无意中把配额加回去）。

### 6.3 验收判据（用本项目真实数据）

- 对项目 `proj_3ac0b627fa46` 的王有材条目，人工清理后（第 4 节难点E 流程），新的
  `appearance_canonical` 跑 `_validate_appearance_evidence`，`source_evidence` 应为
  空数组且不产生任何 error（因为原文对他确实没有可举证的标志性特征）。
- 用同一项目第 130 章原文重新构造一次"王有材战斗后狼狈"的候选 `source_evidence`，跑
  `_appearance_evidence_verified` 应返回 `True`（第 6.2 节测试 6 的真实数据版本，可
  作为集成测试）。
- 用第 1 章那句 44 字的真实原文构造一次"王有材+胖"的候选证据，跑
  `_appearance_evidence_verified` 应返回 `False`（第 6.2 节测试 2 的真实数据版本）。

---

## 7. P0/P1/P2 分级

**P0（本次必须）**

- Schema 新增 `AppearanceEvidence`/`source_evidence`。
- `_appearance_evidence_verified` 核验函数 + 接入 `generate_bible`/`_supplement_
  bible_characters`/`assess_new_character` 三处 P0 生成路径。
- `app/stages.py:4197`/`:2128`、`app/portraits.py:8994/9019/9107` 四处 prompt 改写。
- `app/portraits.py:9053-9109` `card_complete`/重试逻辑重写。
- `app/portraits.py:528-542` `_forward_fragments` 章节边界标记改造。
- 删除 `app/refs.py:80-83` `missing_production_appearance_dimensions` 及两处调用点。
- 第 6.2 节新增测试全部落地。
- 王有材数据人工清理（走既有 UI，非代码改动，但是本次事故的直接交付物）。

**P1（后续，不阻塞本次）**

- 场景圣经 `scene_canonical` 同构改造（难点C 第 3 项）。
- 外观证据的模型二次裁决层（难点A 缓解）。
- storyboard_pack 遗留风险的告警信号（难点C 遗留风险）。
- 只读审计脚本，扫描是否还有其他项目受同类问题影响（难点E）。
- 前端 `source_evidence` 可见信号（`api.ts`/`BiblePage.tsx`）。

**P2（本次明确不做，需要独立设计）**

- personality/speech_style 的出处约束（难点D）——需要先解决"性格形容 vs 设定事实断言"
  这个比 appearance 更难的开放分类问题，不能直接平移本方案的机制。
- 自动批量回填/修复历史受污染角色（难点E）——审计可以自动化，修复必须人工逐条确认。
- "证据真实但断言强度超出证据"这类质量问题的检测（难点A 已知限制）——需要额外的语义
  强度判断机制，不是本方案的结构判据能覆盖的范畴。

---

## 8. 风险与已知限制

1. **难点A 已证明的残余风险**：核验能拦住"跨人借用证据"（本次事故的实际模式，已用真实
   44 字测算证明），但拦不住"引用真实存在、真实指向该角色，但内容与断言的具体强度不匹配"
   这类更隐蔽的失真（例如把"战斗后暂时狼狈"写成"常年伤痕累累"）。这是结构判据的天花板，
   P1 的模型裁决层能部分缓解，但即使加上裁决层也无法做到 100% 语义正确性判断。
2. **40 字上限是本次用单一真实案例反推出的经验值，未经其他项目/其他语言风格的小说交叉
   验证**。如果某本小说的行文习惯是长句嵌套描写（人名与描写之间天然需要更长的连接从句），
   40 字上限可能会误伤一些本来合法的短证据引用，需要在接入更多项目的真实回归数据后复核
   这个常量，本文档不把它当作最终定论，只作为有实测依据的初始值。
3. **`assess_new_character` 路径（P0 改动第 10 项）依赖对 `_forward_fragments` 的改造，
   这部分改造本身没有在本次任务里做代码验证**（只读了代码，没有跑测试验证改造后的分块
   标记不会破坏现有依赖 `extract_character_fragments` 输出格式的其他调用方，例如
   `screen_appearance_changes`(`app/portraits.py:408`) 等也用同一个函数）。实现阶段需要
   核对所有 `extract_character_fragments`/`_forward_fragments` 调用方是否假设了"无章节
   标记的纯文本"格式。
4. **难点E 的 `artifacts` 版本化机制未独立验证**（第 4 节已标注），执行清理前必须先确认
   `saveCharacter` 后端接口的实际行为，否则可能违反 CLAUDE.md「不得在调用方连接上隐式
   提交」或「破坏性操作要有原子性」。
5. **难点D 的因果推断未独立验证**（"去掉配额就不会再触发 personality 级联编造"），这是
   基于对 prompt 结构的分析推断，没有跑一次真实生成调用做对照实验，如实标注。
6. **是否还有其他项目/其他角色受同类问题影响，本次没有条件全量扫描**（只核实了王有材
   一个案例），P1 的审计脚本是发现面的必要补充，本文档不能代表"污染面已确认仅限王有材"。
7. **`app/portraits.py:9053-9109` 的重写涉及"从 verdict 里剥离未通过核验的具体特征文字"
   这一步**——`appearance_canonical` 是自由文本，`source_evidence` 未通过核验时，代码
   如何从整段文本里精确剥离对应的那一小段特征描述，本文档没有给出可靠的自动化字符串处理
   方案（这本身是一个子句边界识别问题）。设计倾向是：核验失败时不做自动文本手术，而是把
   失败原因反馈给模型，通过 AgentLoop/`assess_new_character` 已有的重试机制让**模型自己**
   重写整段 appearance_canonical（模型知道自己写了什么，比代码去猜哪几个字对应哪条证据
   更可靠），第 5 节改动清单第 8-9 项按这个方向设计，但这里同样需要在实现阶段确认
   `assess_new_character` 现有的单轮重试（`require_identity_card` 分支，`max` 一次
   重试）是否够用，还是需要提升到 `generate_bible` 那种多轮 `AgentLoop` 机制——本文档
   未对这一点做最终判定，标注为实现阶段待定项。
