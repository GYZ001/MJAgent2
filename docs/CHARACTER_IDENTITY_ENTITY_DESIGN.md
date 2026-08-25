# 角色身份三层架构：命名权威与视觉实体解耦（设计定稿）

日期：2026-08-24。状态：**架构决策已拍板，本文档为落地设计**——本文档只记录决策与数据结构，
不包含任何代码/数据改动；第 35 轮回归仍在 EP10 重试中，本次不得干扰。所有代码引用均逐条
`grep`/`sed` 核对至当前工作树（commit 附近，`main` 分支），行号如与未来改动后的文件不一致，
以 `grep -n` 复核结果为准。

## 0. 一句话问题

同一个角色在不同集拥有不同的脸：许清在 EP1 是无图群演「银色长袍女子」、EP5 是「许姓女子」、
EP6 是「许师姐」，直到 EP13 才第一次绑定 `bible:许清` 拿到真正定妆照——四张不同的脸，而
`character_portraits` 表里她的定妆照 `portrait_e01eec6ef5ef` 早在 `ep_start=1` 就已
`pack_status=ready`（谱库从第一集就认识她，画面却不认得）。

## 1. 现象与实测证据

| 角色 | 集数 | 系统认出的身份 | 备注 |
|---|---|---|---|
| 许清 | EP1 | 无图群演「银色长袍女子」 | 未绑定 |
| 许清 | EP5 | 「许姓女子」 | 未绑定，与 EP1 不同措辞 |
| 许清 | EP6 | 「许师姐」 | 未绑定，第三种措辞；见 §2 生产事故记录 |
| 许清 | EP13 | `bible:许清` | 首次真正绑定，拿到定妆照 |
| 李富贵 | EP1 | 混在无图群演「其他被困少年」里 | 未绑定 |
| 李富贵 | EP2 起 | `bible:李富贵` | 绑定 |

数据库实测（只读查询 `data/manju.db`，未做任何写操作）：

```
character_portraits: id=portrait_e01eec6ef5ef character_name=许清   ep_start=1 ep_end=NULL pack_status=ready
character_portraits: id=portrait_9e2209df3692 character_name=李富贵 ep_start=1 ep_end=NULL pack_status=ready
```

两条定妆照记录都覆盖 `ep_start=1` 的开区间且已 `ready`——谱库（人物谱 + 定妆照）从第一集就
拥有这两个人的定稿视觉锚点，问题完全出在**分集侧的身份解析找不到这条已有记录**，不是素材缺失。

## 2. 根因链（逐条代码核对）

### 2.1 `Character` schema 没有持久别名位，`Scene` 有

`app/schemas.py:112-122`：

```python
class Character(BaseModel):
    name: str
    role: str
    appearance_canonical: str
    personality: str = ""
    speech_style: str = ""
    relationships: list[Relationship] = Field(default_factory=list)
    ref_image_path: str | None = None
    portrait_prompt_override: str | None = None
```

对照 `app/schemas.py:153`，`Scene` 有 `aliases: list[str] = Field(default_factory=list)`，
注释明确说明用途：「别名只用于把剧本地点稳定解析到同一规范场景，避免为了一个称谓差异重复建场景」。
`Character` 没有对应字段——场景侧"没病"，角色侧"有病"的直接体现。

### 2.2 `functional_extras[]` 冻结 schema 不带身份/视觉字段

`app/production/prep_pack.py` 模块 docstring（`prep_pack_version` 早期示例，第 15-42 行）
声明的 `asset_manifest.functional_extras` 形状：

```
"functional_extras": [{"label": str, "event_ids": [str]}],
```

核对当前运行时载荷（`PREP_PACK_VERSION = "1.6.1"`，定义于 `app/production/prep_pack.py:225`）：
1.6.0 修订（`app/production/prep_pack.py:300-320` 注释）为 `functional_extras[]` 新增了
`provenance: {method, anchor_segments, anchor_phrase}`（构造处见 `app/production/prep_pack.py:1503`
`_prep_pack_provenance(...)`，落盘拼装见 `app/production/prep_pack.py:2009-2016`）。也就是说
docstring 里 `{label, event_ids}` 这行字面已经比运行时载荷落后一个字段，但结论不变：截至当前，
`functional_extras[]` 每一项只有 `label` / `event_ids` / `provenance` 三个字段，**没有身份 ID、
没有视觉锚点、没有别名**——群演一旦被识别为"功能性"，就永久脱离了可寻址的身份体系。

### 2.3 跨集别名库只从已发布分集读写，形成死循环

`app/production/prep_pack.py:880-917` `_prep_pack_cross_episode_alias_conflict` 与
`app/production/prep_pack.py:925-948` `_prep_pack_lookup_character_alias_canonical_name`：
两个函数都用同一条 SQL 扫描 `episodes` 表里**其它已发布分集**的 `screenplay_json ->
asset_manifest.characters[].aliases`。第 888-890 行的注释是这条根因链里最关键的一句自证：

> 只查其它分集已发布的 asset_manifest（这是目前唯一持久化角色别名归属的地方——Character
> schema 没有 Scene 那样的项目级 aliases 字段，只能靠已发布分集的 manifest 做跨集比对）。

写入侧：`app/production/prep_pack.py:1704`／`:1889` 把 `display_name` 写成消歧后的规范名，
`:1721` 把原始措辞 `name` 追加进 `entry["aliases"]`——但这份别名只活在**该集自己的**
`screenplay_json` 里。第 1 集永远是"其它已发布分集"列表里最空的一集：`_prep_pack_
lookup_character_alias_canonical_name` 在 EP1 查无所获 → EP1 无法复用任何跨集别名 → EP1
只能重新赌一次消歧 → 赌不中就落入未绑定/功能性群演 → 不写入任何持久别名位（因为没有持久别名位
可写）→ 死循环闭合。

### 2.4 `authority_id_for_resolution`：具名稳定、functional 按构造不稳定

`app/identity_authority.py:78-124`：

```python
def authority_id_for_resolution(value: dict[str, Any]) -> str:
    ...
    if canonical_name and resolution in {"future_identity", "reference_identity"}:
        return f"bible:{canonical_name}"          # 第 97 行：具名分支，身份键=名字字符串
    ...
    seed = {
        "canonical_name": canonical_name,
        "identity_group": identity_group or f"source:{source_label}",
        "identity_scope_fingerprint": identity_scope_fingerprint,   # 第 104 行
    }
    digest = hashlib.sha256(json.dumps(seed, ...).encode("utf-8")).hexdigest()[:16]
    return f"functional:{digest}"                  # 第 124 行：functional 分支
```

第 104-109 行自己的注释写明了这不稳定是有意为之的边界：`identity_scope_fingerprint`
（"current-1:F1 and similar model-local group tokens"）「only meaningful inside one
discovery input」——也就是说 functional 身份键**按设计**只在一次模型调用内稳定，换一次调用
（=换一集）指纹就变，`functional:{digest}` 必然跨集不同。这不是 bug，是这个函数从未被要求
承担"跨集视觉稳定"这个职责——它是命名权威（谁能签发身份）的键，不是视觉资产的键，问题出在
下游把它当成了后者来用。

### 2.5 `portraits.py` 查图路径按名字字符串索引，`functional_extras` 从不进入

`app/portraits.py:9588-9601` `portrait_for_episode`：

```python
def portrait_for_episode(project_id: str, name: str, episode_no: int | None) -> str | None:
    ...
    row = get_conn().execute(
        "SELECT image_path FROM character_portraits "
        "WHERE project_id=? AND character_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY ep_start DESC LIMIT 1",
        (project_id, name, episode_no, episode_no)).fetchone()
    ...
```

`app/portraits.py:9626-9638` `bible_for_episode` 对 `bible.characters` 逐个调用
`appearance_for_episode` / `portrait_for_episode`，只遍历**具名**角色列表；`functional_extras`
（§2.2）从头到尾不出现在这条路径的输入里。全仓核查（`grep -rln functional_extras app frontend`）
确认 `functional_extras` 只在 `app/production/prep_pack.py`（生产）和
`frontend/src/pages/ScriptPage.tsx:607`（纯展示）里出现，`app/portraits.py` 里唯一一处提到它的地方
（第 7758 行）只是注释，不是消费代码——功能性身份**没有任何下游消费入口**，查图路径天然看不到它。

### 2.6 同款生产事故的历史记录 + 现行"每集重赌"对策

`app/portraits.py:107-116`：

```python
# 称谓形态的优先级阶梯：真名 > 尊称 > 代称。
#
# 生产事故：第 1 集的后续窗口只写出「许师姐」，模型据此签发了一张全新人物卡
# ``bible:许师姐``，与人物谱里本来就有的「许清」构成同一个人的身份分裂，随后
# 第 5 集的场次身份注册表因为同一个称谓指向两个 canonical identity 而 fail-closed。
#
# 判据不能靠后缀词表（本项目明令禁止），只能由读得懂原文的模型给出形态判断；
# 后端拿到形态后确定性执行阶梯：只有真名可以签发新的人物权威，尊称与代称一律
# 先落为功能身份。这样「有真名就不能单独成角色」，而真名尚未出现时该人物仍然
# 是一个独立身份，等真名真正出现在证据里再由 K 决议认领同一个人。
```

现行对策（真名>尊称>代称优先级）在 `app/portraits.py:2281-2353` 的 K 决议提示词里落地
（规则 3："尊称或代称请照实写 honorific/referential，后端会自动把它落为功能身份"）。这条对策
本身是对的（防止二次身份分裂），但它只解决了"不要凭空造一个新权威"，没有解决"这个功能身份下次
还是不是同一张脸"——`scope_qualifier`（规则 8，`app/portraits.py:2340-2352`）目前只用于
**同一批模型调用内**区分"两个不同的师兄"这类歧义（见 §5.2 的复用方案），职责链条到此为止，
没有向下传给任何跨集持久化机制。于是每一集的功能身份判定都是独立事件——"每集重赌一次"。

### 2.7 关联故障 RCA：EP10 `ERR-20260824-bc3d14` 与三层设计是同一缺口的两个出口

日志实测（`logs/serial10.log:1991-1993`）：

```
[08-24 22:19:30] EP10 :: failed ... err=... 业务校验失败：current 已登记身份必须选择 K decision：李富贵
（QA · ERR-20260824-bc3d14）
```

专项 RCA（另一 agent 完成，事实已核实）结论：

- 模型在**同一次**身份预检响应里，既正确签发了 K 决议（`K:E034:c321b6b3c385f96601b1959c`，
  `source_label=李富贵`，证据 E034 = 原文「孟浩，你是我李富贵这一辈子的好朋友。」），又在 `n`
  （新具名）数组里重复申报了李富贵，触发 `app/portraits.py:1592` 的硬校验：
  `"current 已登记身份必须选择 K decision：" f"{source_label}"`（该分支的完整判据见
  `app/portraits.py:1581-1596`）。
- 提示词信息完整（已有角色清单 + 36 条 K 目录含该条 + 规则 2 原文要求），K 帽（`decision_cap`，
  `app/portraits.py:2885` `_current_identity_decision_cap`）远未触顶，排除信息缺口。
- 302.9s ReadTimeout 是另一条流水线（paratext discovery）的延迟，无因果，属噪声。
- 相同输入换一次采样即通过，属非确定性输出。
- 该校验分支历史触发 3 次：EP5×2（许清）、EP10×1（李富贵）——全部是"绰号→真名揭晓"型角色，
  与 §1 现象同源。

**已有的窄口径缓解与其边界**：`app/portraits.py:1736-1778` 已经落地一个第 35 轮的即时补丁
（注释明确标注 `第35轮真实回归 ERR-20260824-bc3d14`）：为本响应 `k` 数组里每条合规决议记录它
实际锚定的 `(source_label, evidence_ref)` 复合键（`redundant_n_echo_k_pairs`，第 1745 行初始化，
第 1778 行写入），`n` 循环里（`app/portraits.py:1810-1826`）如果某条 `n` 声明命中的
`(identity_label, evidence_ref)` 复合键恰好等于某条 `k` 决议锚定的复合键，判定为"模型对同一个人
签发了两份声明，k 是权威、n 是冗余回显"，静默丢弃这条 `n`、不再硬失败。但这个补丁的判据要求
**label 与 evidence_ref 都一致**——第 1736-1742 行的注释自己写明了留白：「第35轮用例 C：同
label 不同 ref 不算——那种情况下 k 决议并未覆盖 n 这条具体声明所引用的证据，仍然维持硬失败」。

日志里的真实失败（`ERR-20260824-bc3d14`，§2.7 开头引用）正是这个"用例 C"：K 决议锚定在 E034
（真名揭晓的那句台词），而模型试图在 `n` 里补的是**另一条证据**（大概率是 E007，「小胖子」
一路追踪下来的功能性称谓组自己的证据）——两者 `evidence_ref` 不同，窄口径补丁的复合键对不上，
落回硬失败。这不是补丁没生效，是这个补丁从一开始就只解决"模型手滑重复回显同一份证据"，没有
（也不该在那个时点）解决"功能性称谓组的历史证据应该并入新签 K 决议"这个语义折叠问题——后者需要
一个模型主动申报、代码核验申报合法性的新通道，而不是靠复合键巧合命中。

**结构性缺口**：当前身份预检契约（`CurrentKnownIdentityDecision`，`app/portraits.py:2687-2693`，
字段只有 `decision_id` / `kind` 两个）没有任何字段能表达"本批一直跟踪的功能性称谓组（小胖子，
自证据 E007 起）就是本批刚刚签发的 K 决议（李富贵，E034 揭晓）"这件事。现有的合并机制覆盖三种
情形：跨批 `functional_identity_key` 延续（`prior_functional_projection`，
`app/portraits.py:2250-2258`）、集内 `existing_resolution_projection`
（`app/portraits.py:2023-2035`）复用、以及刚落地的"同证据冗余回显"窄口径去重（上一段），
**唯独不覆盖"同批内 功能性称谓（不同证据）↔ 本批新签 K 决议 折叠"**。模型察觉到了指代同一人，
但没有合法表达通道，于是发明了违规写法（重复走 `n`，且证据凑不上窄口径补丁的复合键）。

**校验器不对称**（次要观察，记录但不作为本次结构修复的直接目标）：对"非逐字、臆测型"的
reserved-label 申报，`app/portraits.py:1800-1809` 静默丢弃（`continue`，不报错、不记账）；
而对"有逐字证据、且自己已经在 `k` 里正确签发"的重复申报，`app/portraits.py:1592` 直接硬毙整集
重试。容忍了更糟的（凭空猜测），毙掉了更好的（有证据、且已经正确签发）。

**对本文档三层设计的影响**：见 §4.2 末尾"同批折叠通道"——层二的"实体合并"必须同时覆盖**跨集**
（真名在后续集揭晓）与**同批**（同一次调用内绰号与真名并存）两种时机，且要给出明确申报字段 +
可机械核验的成员关系检查 + 记账，而不是依赖模型自觉遵守没有落地的约定。

## 3. 架构判断

现行架构把"这个人叫什么"（命名，需要硬证据，判错=事实错误）与"这个人长什么样"（视觉实体，
只需要知道与上次是同一人）绑成了一件事：`bible:{name}` / `functional:{digest}` 既是命名权威
的键，也被当成了视觉资产的挂载键。于是命名层刻意保守的安全默认（不确定不绑）被当成了画面的判决
——而画面的"不绑"不是安全默认，它等于"换一张新脸"。

原著读者先认识"小胖子"、第 10 章才知道他叫李富贵；观众理应获得同样的体验：**同一张脸，换了
称呼**。现在恰好反了：名字没有提前泄露（这点做对了），脸却一直在变（这点错了）。

修复方向：把"命名权威"（authority_id，§2.4 的 `bible:{name}` / `functional:{digest}`）与
"视觉实体"（本文档新增的 `visual_entity_id`）拆成两个独立的键空间，各自解决自己的问题：

- 命名权威继续保守："不确定不绑"，判错是事实错误，代价高，必须谨慎。
- 视觉实体必须激进："只要知道跟上次是同一人就复用同一张脸"，代价是最多复用错一张不影响剧情的
  图，远低于"每次都换脸"的观感损失。

## 4. 三层设计

### 4.1 层一 · 人物谱别名位（数据字典）

**新增结构**（`app/schemas.py`，紧邻 `Character` 类，`Relationship` 之后、`Character` 之前
或类内均可，建议独立类以复用 Pydantic 校验）：

```python
class CharacterAlias(BaseModel):
    """一条别名证据：模型申报 + 代码核验后才允许落库（不确定不登记）。"""
    text: str                    # 逐字称谓/别名字符串（如"许师姐""银色长袍女子""小胖子"）
    name_kind: str                # personal_name/honorific/referential，
                                   # 复用 app.portraits.IDENTITY_NAME_FORM_*（app/portraits.py:124-126），
                                   # 不新造平行词表
    evidence_chapter_index: int   # 证据锚点：原著章节序号
    evidence_quote: str           # 证据锚点：逐字引句；必须能在该章节原文中作为子串命中，
                                   # 且与该角色在同段/同场景共现——不满足则不登记

class Character(BaseModel):
    name: str
    role: str
    appearance_canonical: str
    personality: str = ""
    speech_style: str = ""
    relationships: list[Relationship] = Field(default_factory=list)
    ref_image_path: str | None = None
    portrait_prompt_override: str | None = None
    aliases: list[CharacterAlias] = Field(default_factory=list)   # 新增
```

`name_kind` 分级（真名/尊称/代称）**不决定谁能签发身份权威**——那仍由 §2.6 的现行阶梯（只有
`personal_name` 能签发新权威）把关，本字段只决定层三的显示称谓选择（见 §4.3）。

**证据来源**：全书分析阶段（`app/stages.py:3462` `generate_bible`）模型申报，代码核验（逐字子串
命中 + 同段/同场景共现）后落库；该阶段已经具备全书取材能力
（`_render_bible_source` + `BIBLE_HEAD_CHAPTERS`/`BIBLE_LOOKAHEAD_CHAPTERS`，
`app/stages.py:3192-3193`，专门为"后期才登场的重要角色也要进圣经"设计），别名证据的全书可见性
与 `Character.name`/`appearance_canonical` 本来就有的全书可见性同源，不构成新的"系统提前知道
真名"的信息泄露——真正的剧透纪律在层三（本集只显示本集措辞，见 §4.3），不在这里。

**跨集别名注册表读写源切换**：`app/production/prep_pack.py:880-917`
`_prep_pack_cross_episode_alias_conflict` 与 `:925-948`
`_prep_pack_lookup_character_alias_canonical_name` 改为直接读 `Bible.characters[].aliases`
（内存中一次拿到，不再逐条扫描 `episodes.screenplay_json`），直接切断 §2.3 的死循环：EP1 也能
查到全书分析阶段已经申报好的别名，不必等到"其它分集先发布过"。

### 4.2 层二 · 视觉实体与名字解耦

**新增函数**（`app/identity_authority.py`，与 `authority_id_for_resolution` 并列，
**不修改**该函数现有语义——它继续只负责命名权威，10+ 处调用方的既有行为不受影响）：

```python
def visual_entity_id_for_resolution(value: dict[str, Any]) -> str:
    """稳定视觉实体 ID：与命名权威（authority_id）解耦。
    - 已具名分支（resolution in {future_identity, reference_identity} 且 canonical_name 非空）：
      复用 f"bible:{canonical_name}"——与 authority_id_for_resolution 第 97 行同格式，
      对现有已正确工作的具名绑定零迁移成本。
    - 功能分支：sha256({"source_label": 归一化(source_label), "scope_qualifier": scope_qualifier})
      取前 16 位，f"entity:{digest}"——不掺入 identity_scope_fingerprint
      （authority_id_for_resolution 第 101-109 行注释自证：该指纹"only meaningful inside one
      discovery input"，是 functional 分支跨集不稳定的构造性根因，见 §2.4）。
    """
```

`scope_qualifier` 不是新造概念：它是 K 决议提示词规则 8（`app/portraits.py:2340-2352`）已经
要求模型申报的、区分"同一称谓指不同人"的限定语（如"师兄"在同一批出现两次但分指两人时各自的
限定语），现有代码已经用 `(source_label, scope_qualifier)` 复合键做**批内**唯一性判定
（分组与消歧逻辑见 `app/portraits.py:1854-1873`，函数 `_project_current_identity_response`
定义于 `app/portraits.py:1476`；真实第 18 轮 EP10 回归 ERR-20260824-b16bb4 引入）。层二只是把
这把已经存在、已经被模型正确使用的尺子，从"批内去重"的适用范围**扩展**为"跨集稳定键"的输入，
不引入新的模型职责。

**`character_portraits` 迁移**（`app/db.py:348-362` 现有建表语句）：

现状：

```sql
CREATE TABLE IF NOT EXISTS character_portraits (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    character_name TEXT NOT NULL,
    ep_start INTEGER NOT NULL,
    ep_end INTEGER,
    ...
    UNIQUE(project_id, character_name, ep_start),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
```

`id` 是 SQL 主键（代理键，不变）；真正承担"这是谁"这个业务身份的是 `character_name` 列（配合
`UNIQUE(project_id, character_name, ep_start)`）。迁移方式：追加新列，走 `app/db.py:1376`
`MIGRATIONS` 元组的既有增量模式（参照第 1541 行 `pack_status` 列的先例）：

```sql
ALTER TABLE character_portraits ADD COLUMN visual_entity_id TEXT;
CREATE INDEX IF NOT EXISTS idx_character_portraits_visual_entity
  ON character_portraits(project_id, visual_entity_id, ep_start);
```

回填（机械、无需模型调用）：既有 `character_portraits` 行只可能来自"已经跑完真名核验"的具名
分支（§2.5 已证：functional 身份从未进入查图路径，不可能有既存的功能性 portrait 行），所以：

```sql
UPDATE character_portraits SET visual_entity_id = 'bible:' || character_name
WHERE visual_entity_id IS NULL;
```

`character_name` 列与其 `UNIQUE` 约束**保留不动**（过渡期双轨）；读路径
（`app/portraits.py:9588` `portrait_for_episode`、`app/portraits.py:9605`
`appearance_for_episode`、`app/portraits.py:9580` `_open_portrait`、`app/multiview.py:250`
`portrait_row_for_episode`）新增按 `visual_entity_id` 查询的分支，未命中或调用方未传时回退
`character_name`——不删字段、不改现有字段语义，符合"优先修改现有代码"的项目纪律。

**实体合并**（真名揭晓时）：新增审计表：

```sql
CREATE TABLE IF NOT EXISTS visual_entity_merges (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    from_visual_entity_id TEXT NOT NULL,   -- 合并前的 functional 实体 ID
    to_visual_entity_id TEXT NOT NULL,     -- 合并后的规范实体 ID（通常是 bible:{name}）
    canonical_name TEXT NOT NULL,          -- 揭晓的真名
    merge_rule TEXT NOT NULL,              -- 选图规则版本标签，确定性、可复算
    selected_portrait_id TEXT,             -- 合并后选中的规范定妆照
    evidence_episode_no INTEGER NOT NULL,  -- 触发合并的集号
    created_at REAL NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
```

选图规则（确定性、非本次臆测新词表）：合并时若 `from`/`to` 双方都已有 `ready` 定妆照，保留
`to`（规范权威）一侧的图，`from` 一侧的历史图保留在 `character_portraits` 表中但通过
`base_portrait_id`/lineage 字段挂到 `to` 之下，不删除、可回溯；若只有一侧有图，直接复用该侧。

**同批折叠通道**（§2.7 RCA 直接指向的缺口，层二的合并机制必须覆盖"跨集"与"同批"两种时机，
用同一套 `visual_entity_merges` 记账，不是两套平行机制）：

与 `app/portraits.py:1736-1778` 已有的"同证据冗余回显"窄口径去重（复合键
`(source_label, evidence_ref)` 精确相等才触发）不是同一件事、不重叠、不冲突：那个补丁处理的是
"模型对同一条证据签发了两份声明"这种纯粹的复读，本通道处理的是"功能性称谓组的历史证据（不同
`evidence_ref`）应该并入新签 K 决议"这种真实的身份折叠——前者是去重，后者是合并，二者的判据
（复合键相等 vs. token 成员关系）也不同，都需要保留，互不替代。

`CurrentKnownIdentityDecision`（`app/portraits.py:2687-2693`，当前字段只有 `decision_id: str`
/ `kind: Literal["onscreen", "mentioned"]`）新增：

```python
class CurrentKnownIdentityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str = Field(min_length=1, max_length=96)
    kind: Literal["onscreen", "mentioned"]
    absorbed_functional_keys: list[str] = Field(default_factory=list)   # 新增
```

语义：模型借此声明"这个 K 决议（本批新签或已登记）就是这些 `functional_identity_key` 指代的
同一人"。**代码核验方式与 `k.decision_id` 的既有校验同构**——不重新做文本语义判断，只做集合
成员关系检查：
`absorbed_functional_keys` 里每一项必须已经真实存在于以下任一来源（伪造的 token 直接拒绝）：
- 本批 `f` 数组自己声明过的 `functional_identity_key`（批内声明，见
  `CurrentFunctionalIdentityDecision.functional_identity_key`，`app/portraits.py:2721`）；
- `prior_functional_projection` 的 `decision_id`（跨批延续，`app/portraits.py:2250-2258`）；
- `existing_resolution_projection` 的 `canonical_name`（跨集/本集已有功能身份决议，
  `app/portraits.py:2023-2035`）。

命中即执行：naming 侧（`authority_id`）不变，K 决议已经正确签发；visual 侧新增一条
`visual_entity_merges` 记录，`from_visual_entity_id` = 被吸收的 functional 组当时已经分配到的
`visual_entity_id`（若尚未分配，说明该组从未出镜，无需合并，跳过），`to_visual_entity_id` =
`bible:{K 决议的 canonical_name}`，`merge_rule="same_batch_k_absorption"`。有了这条合法通道，
模型不再需要靠重复写 `n` 来表达"这是同一个人"（§2.7 的违规写法失去存在必要），
`app/portraits.py:1592` 的硬校验分支不会再被这种"同批内绰号与真名共存"的合法形状触发。

`app/portraits.py:1800-1809` 的静默丢弃分支保持现状不动（本次不修复，见 §7 不实现清单）——
折叠通道消除了触发 1592 硬失败的**主要**场景，1800-1809 的不对称作为已知设计张力记录在案。

### 4.3 层三 · 本集显示称谓

`asset_manifest.characters[]`（`app/production/prep_pack.py:37-38` 冻结形状 + `:1700-1721`/
`:1889` 组装逻辑）新增两个可选字段，**沿用 1.6.0 先例的纯增量纪律**
（`app/production/prep_pack.py:314-320` 注释："两个字段都是新增可选结构……不破坏任何既有消费者，
payload 冻结纪律没有被打破——冻结的是既有字段的语义不变，不是禁止新增字段"）：

```
"characters": [{
  "identity_id": str,              # 不变：命名权威引用（bible:{name} / functional:{digest}）
  "display_name": str,             # 不变：现行行为保留，历史消费者兼容
  "portrait_id": str,              # 不变
  "event_ids": [str],              # 不变
  "aliases": [str],                # 不变：本集原始措辞列表
  "visual_entity_id": str,         # 新增：全局稳定（层二产出），决定取图
  "display_appellation": str,      # 新增：本集原文措辞，决定字幕/台词显示
}],
```

`functional_extras[]`（`app/production/prep_pack.py:41`，现有 `{label, event_ids,
provenance}`）同步新增 `visual_entity_id`。`PREP_PACK_VERSION`
（`app/production/prep_pack.py:225`，当前 `"1.6.1"`）随之推进为 `"1.7.0"`，版本注释追加一条
说明本次是 schema 新增字段、非破坏性变更，与既有 1.4.2/1.5.x/1.6.0 版本注释同一记账风格
（`app/production/prep_pack.py:226-320`）。

**动机**：现行 `display_name` 在绑定成功后被重写为全局规范名（`app/production/prep_pack.py:1704`
`"display_name": resolved_name`），前端展示层（`frontend/src/pages/ScriptPage.tsx:713`
`character.display_name || character.identity_id`）直接呈现这个规范名——这会造成"第一集字幕就
叫她许清"的剧透风险，同时视觉侧却在"换脸"，两个方向的问题同时存在、原因相反。分离后：
`visual_entity_id` 保证画面稳定（哪怕早于真名揭晓），`display_appellation` 保证本集只说本集
措辞（哪怕视觉早已认出是同一人）。

## 5. 模块边界

| 层 | 文件 | 改动性质 |
|---|---|---|
| 层一 | `app/schemas.py` | `CharacterAlias` 新类 + `Character.aliases` 新字段 |
| 层一 | `app/stages.py:3462-3521`（`generate_bible`） | 提示词新增别名+证据申报要求，规则 5（第 3515 行）从"不得拆分角色"改为"须申报别名及证据锚点" |
| 层一 | `app/stages.py`（新函数，与 `_supplement_bible_characters`@3348 同级） | 新增窄口径别名回填函数，用于当前项目一次性回填历史人物谱 |
| 层一 | `app/production/prep_pack.py:880-948` | 两个跨集别名函数读写源从"扫描已发布分集"切换为读 `Bible.characters[].aliases` |
| 层二 | `app/identity_authority.py` | 新增 `visual_entity_id_for_resolution()`，不改动 `authority_id_for_resolution` |
| 层二 | `app/db.py` | `character_portraits` 新增列（`MIGRATIONS` 元组追加）+ 新表 `visual_entity_merges` |
| 层二 | `app/portraits.py:9580-9638, 2687-2693` | 查图路径新增 `visual_entity_id` 分支；`CurrentKnownIdentityDecision` 新增 `absorbed_functional_keys` + 折叠执行逻辑 |
| 层二 | `app/multiview.py:250-350` | `portrait_row_for_episode` 等同步 `visual_entity_id` 查询路径 |
| 层三 | `app/production/prep_pack.py:37-42, 1700-1900` | `asset_manifest.characters[]`/`functional_extras[]` 组装逻辑新增两字段；`PREP_PACK_VERSION`（第 225 行）推进 |
| 层三 | `frontend/src/pages/ScriptPage.tsx`, `frontend/src/api.ts` | 展示层读取 `display_appellation`（P1/P2，见 §7） |

## 6. P0/P1/P2 拆分

### P0（本次架构改造必须完成——直接对应问题现象的根因修复）

1. `app/schemas.py`：`CharacterAlias` 结构定义 + `Character.aliases` 字段。
2. `app/stages.py`：新增窄口径别名回填函数（全书上下文，只产出 `aliases`，冻结其它一切既有字段
   不改写），用于当前项目一次性回填；`generate_bible` 提示词规则 5 同步更新（面向未来新项目）。
3. `app/production/prep_pack.py:880-948`：跨集别名读写源切换为 `Bible.characters[].aliases`，
   直接消除死循环。
4. `app/identity_authority.py`：新增 `visual_entity_id_for_resolution()`。
5. `app/db.py`：`character_portraits` 新增 `visual_entity_id` 列 + 索引 + 机械回填。
6. `app/portraits.py`：`portrait_for_episode`/`appearance_for_episode`/`bible_for_episode`/
   `_open_portrait` 新增按 `visual_entity_id` 查询路径（向后兼容 `character_name` 回退）。
7. `app/production/prep_pack.py`：`asset_manifest.characters[]`/`functional_extras[]` 新增
   `visual_entity_id` + `display_appellation`；`PREP_PACK_VERSION` → `1.7.0`。
8. `app/portraits.py:2687-2693`：`CurrentKnownIdentityDecision.absorbed_functional_keys` +
   同批折叠执行逻辑（§4.2 末尾，直接解决 EP10/EP5 的 K/F 折叠缺口）。

### P1（架构完整性所需，不阻断"同一张脸"这条核心判据本身）

9. `app/multiview.py`：`portrait_row_for_episode` 等函数同步 `visual_entity_id` 查询路径。
10. `visual_entity_merges` 审计表的完整读路径（当前 P0 只建表+写入，P1 补查询/展示）。
11. 未具名角色首次出场即触发参考图生成的具体触发点（当前架构里功能身份从未进入生成流水线，
    §2.5；本文档只定义数据结构，触发逻辑设计留待专项，范围与风险需单独评估）。
12. 历史项目（非当前回归项目）的别名回填批处理脚本。
13. `_supplement_bible_characters`（`app/stages.py:3348-3381`）提示词同步别名申报（P0 只改
    `generate_bible` 主路径）。
14. 前端 `ScriptPage.tsx`/`api.ts` 展示 `display_appellation`。
15. `app/portraits.py:1800-1809` 静默丢弃分支的不对称问题（§2.7 次要观察）：补一条观测日志
    （丢弃事件计数），不做行为改变。

### P2（性能/收尾，不阻断功能正确性）

16. `_prep_pack_cross_episode_alias_conflict` 等函数彻底退役"扫描已发布分集 JSON"这条旧路径
    （P0 只是切换主读源，旧扫描逻辑可先保留一段时间做双重校验再删除）。
17. `character_portraits` 唯一约束从 `(project_id, character_name, ep_start)` 切换为
    `(project_id, visual_entity_id, ep_start)`（P0/P1 期间两套并存，`character_name` 列不删除）。
18. `identity_scope_fingerprint` 相关命名/注释的清理。

## 7. 本次明确不实现的功能

- 不实现未具名角色首次出场即自动触发参考图生成的具体代码（P1 占位，需要专项设计生成触发点、
  成本评估）。
- 不实现 `character_portraits` 主键/唯一约束的结构性切换（本次只新增列，`character_name` 列
  与其 `UNIQUE` 约束保持不动）。
- 不实现跨项目共享别名库——人物谱、别名、视觉实体严格按 `project_id` 隔离，不做跨项目复用。
- 不实现针对"许师姐""小胖子"等具体词的特判或名单：CLAUDE.md 明令禁止黑白名单式修复；判据
  只能是模型申报 + 代码核验的证据锚点机制（逐字引句 + 章节序号），不允许出现任何具体称谓的
  硬编码分支。
- 不实现 `_supplement_bible_characters`（`app/stages.py:3348`）与前端展示层的同步改造（P1/P2）。
- 不代入 EP10 `ERR-20260824-bc3d14` 的最终验收结论——本文档已把 RCA 的结构性缺口（§2.7）纳入
  层二设计，但具体修复的验收/上线时机由持有该 RCA 的一方决定，不在本文档内下判断。
- 不对已发布 EP1-10 数据做任何清库/重跑操作——本任务只写文档，不碰数据、不重启服务、不发起
  生成 run。

## 8. 回归验证判据（可机械判定）

1. **视觉实体跨集一致性**：许清在 EP1/EP5/EP6/EP13 的 `asset_manifest.characters[].
   visual_entity_id` 完全一致（无论 `display_name`/`display_appellation` 当集写的是什么措辞）。
2. **群演转正一致性**：李富贵在 EP1（群演）与 EP2 起（具名）的 `visual_entity_id` 完全一致。
3. **别名可追溯**：`Character.aliases` 每条记录的 `evidence_quote` 都能在
   `evidence_chapter_index` 对应的原著章节原文中逐字命中（机械字符串包含检查）。
4. **别名注册表读写自洽**：EP1（项目内最早一集）就能查到 `Bible.characters[].aliases` 里已
   登记的别名并成功绑定，不再要求"必须有其它已发布分集先命中过"这个前置条件。
5. **同批折叠首次通过**：EP10（或任何同构场景：绰号与真名在同批/跨批共存）身份预检**首次
   采样即通过**，不依赖 60s 自动重试或换一次采样才能通过——即 `app/portraits.py:1592` 的
   "current 已登记身份必须选择 K decision" 分支不再被"模型已经正确签发 K 决议、但试图重复
   走 n 表达同一人"这种形状触发。
6. **合并可审计**：任一次真名揭晓触发的实体合并（跨集或同批）都在 `visual_entity_merges`
   表中留有一条可查询、可回溯的记录，`selected_portrait_id` 指向的图片文件确实存在。
7. **命名权威不受影响**：`authority_id_for_resolution` 的既有行为（`bible:{name}` /
   `functional:{digest}`）逐字不变，所有依赖它的现有测试/调用方无需改动即可通过。

## 9. 数据失效范围（哪些必须清库重跑）

- 层一改动（`Character.aliases` 新增+回填、`prep_pack.py` 别名读写源切换）影响"角色身份解析"
  模块——按项目既有纪律（模块改动后清该模块前十集数据、从第 1 集严格串行重跑），EP1-10 的
  `episodes.screenplay_json` / `asset_manifest` / `episodes.screenplay_character_resolutions`
  需要清库从 EP1 重跑。
- 层二改动（`visual_entity_id` 新增、`character_portraits` 新增列+回填、查图路径切换）同样
  影响该模块——但**已生成的定妆照图片文件本身不需要重新生成**：`portrait_e01eec6ef5ef`
  （许清）、`portrait_9e2209df3692`（李富贵）机械回填 `visual_entity_id='bible:许清'`/
  `'bible:李富贵'` 后可直接复用，需要重建的只是"哪一集在哪个时间点绑定到了哪个身份"这件事本身。
- 层三改动（`asset_manifest` 新字段）附着于层一/层二的重跑，无独立清库需求。
- **结论**：三层改造合计会使 EP1-10 已发布的 `screenplay_json`/`asset_manifest`/
  `screenplay_character_resolutions` 全部失效，必须清"剧本台"模块数据、从 EP1 严格串行重跑；
  `character_portraits` 中已 `ready` 的图片资产可回填复用，不必重新生成。

## 10. 风险与回滚

| 风险 | 说明 | 缓解 |
|---|---|---|
| 迁移脚本执行范围 | `character_portraits` 新增列 + `UPDATE` 回填是全表操作 | SQLite `ALTER TABLE ADD COLUMN` 是轻量操作；`UPDATE` 先在 dev 副本验证影响行数与耗时，再上生产库 |
| `generate_bible` 提示词改动 | 新增别名申报要求可能改变模型输出的 token 消耗与失败率 | 小流量验证后再全量启用；`_supplement_bible_characters` 保持 P1，不与主路径同批改动 |
| 双 ID 语义混淆 | `authority_id`（命名权威）与 `visual_entity_id`（视觉实体）并存，后续开发者可能混用 | 代码注释与本文档明确标注"authority_id 决定命名权威，visual_entity_id 决定取图，二者不得混用"；两者共享 `bible:{name}` 前缀格式是刻意的（具名分支零迁移成本），不是巧合，需要在两处函数的 docstring 里互相引用对方 |
| 改动面广 | `character_portraits` 被 14 个文件引用，episode-lookup 系列函数被 7 个文件调用 | P0 只改读路径的入口函数（新增分支，旧分支保留），不要求一次性改完全部调用方；全量迁移是 P1/P2 |
| 同批折叠通道被滥用 | `absorbed_functional_keys` 若校验不严会变成新的"凭空认领"后门 | 校验只做集合成员关系检查（token 必须已经真实存在于 `f`/`prior_functional_projection`/`existing_resolution_projection` 三个来源之一），不接受任意字符串，与 `k.decision_id` 的既有校验方式同构 |

**回滚**：P0 全部改动均为新增字段/新增列/新增函数，不删除、不改写既有字段语义；唯一的语义级
改动是 `prep_pack.py` 两个别名注册表函数的读写源切换——如需回滚，恢复原扫描逻辑、保留新增字段
不读取即可，向后兼容。因此回滚成本低，可按文件逐个 revert，新增的数据库列允许残留（旧代码从不
读取新列，不影响运行）。
