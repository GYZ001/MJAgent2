"""人物身份/定妆照子系统的模块级常量：契约版本号、预算阈值、称谓形态枚举。
纯数据，零内部依赖，供本包其余模块与外部按需引用。
"""

from __future__ import annotations


from app.refs import PRODUCTION_APPEARANCE_MAX_CHARS, PRODUCTION_APPEARANCE_MIN_CHARS


FRAGMENT_WINDOW = 220   # 命中角色名前后各取多少字
FRAGMENT_BUDGET = 4000  # 单角色单段送审片段总字数预算
APPEARANCE_MIN = PRODUCTION_APPEARANCE_MIN_CHARS
APPEARANCE_MAX = PRODUCTION_APPEARANCE_MAX_CHARS
STAGED_INITIAL_EP_START = 2_147_483_647  # 候选包不得命中任何真实集号
CAST_DISCOVERY_SOURCE_BUDGET = 18000
CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET = 8000
CHARACTER_CARD_MAX_TOKENS = 4096
IDENTITY_DISCOVERY_CONTRACT_VERSION = "screenplay-identity-discovery.v16"
CURRENT_IDENTITY_DECISION_VERSION = "screenplay-current-identity.v18"  # v18:
# 真实 EP1 回归 ERR-20260826-d6fba4（proj_3ac0b627fa46/ep_3d523ff4d0a4，
# run_c313b5138699，provider_calls.id=11909，contract_version=screenplay-
# identity-discovery.v16）：K:E001:2690631a491d4e5ef3729ebf 把
# ['孟才子','孟兄']、K:E024:a3d42d9e45e09ef8776d0901 把 ['王伯的儿子']、
# K:E052:38f03a2cabff8be22d106f12 把 ['许师姐'] 填进了各自的
# absorbed_functional_keys，全部命中 v17 那道越界核验（安全默认，见
# _project_current_identity_response K 循环注释）被拒绝，整集 quality_gate
# 硬失败停跑。四个 token 均不在本批 F 声明、前批 P token 或既有 functional
# 组任一来源里——它们是有名有姓角色的其它称谓（孟才子/孟兄=孟浩、王伯的
# 儿子=王有材、许师姐=许清），模型想表达"这是同一个人"语义不算错，但用
# 错了通道。这是与 v17 同族但不同症状的变体：v17 只堵了"K 决议吸收自己的
# 锚定 source_label"这一个具体写法，从未正面陈述 absorbed_functional_
# keys 的完整合法取值域，模型换一种越界方式（吸收别人的称谓而非自己的）
# 照样命中同一道核验——补了实例、没补判据。真正根因还是 prompt 规则 9：
# 只改了下方规则 9 的措辞，把"禁止某个具体写法"改成"正面陈述可填值的完整
# 判据"（合法域=本批 F 声明过的 key/前批 P token/既有 functional 组，且
# 这三类来源的共同前提是背后实体仍处于"真名未定"的功能性占位状态；已有
# 确定真名之人的其它称谓从一开始就不满足这个前提，不得为了吸收而现造一条
# f 项）。不放宽越界核验本身，也不改判成可重采样的格式族——这两条结论 v17
# 已经写死，本次不推翻。换版本号只是为了让这条 prompt 变化生效——不换会让
# current_evidence_catalog_hash 相同、current_identity_version 仍是 v17
# 的旧输入，命中 discover_character_candidates 里 screenplay_identity_
# discovery 的已验证缓存工件（cached.get("current_identity_version") ==
# CURRENT_IDENTITY_DECISION_VERSION 那段），把旧 prompt 下的候选静默当成
# 新 prompt 下的结果复用，与 v14/v15/v16/v17 换版本号是同一个理由。
# v17:
# 真实 EP5 回归 ERR-20260825-0d8a29（proj_3ac0b627fa46/ep_0a7130b7b402，
# provider_calls.id=11141）：K 决议把自己的锚定 source_label（即同一
# decision_id 在本批 K 目录里自带的 source_label，如「许师姐」「孟浩」）也
# 填进了自己的 absorbed_functional_keys，被 _project_current_identity_
# response 的越界核验拒绝（安全默认，见该函数 K 循环注释），整集 quality_
# gate 硬失败停跑。核对真实 request/response：absorbed_functional_keys 在
# wire schema 里没有 enum（批内 f 项自造的 F1/F2 key 在 schema 构建时还不
# 存在，无法预先枚举，见 CurrentKnownIdentityDecision.absorbed_functional_
# keys 字段注释）——这条越界是纯 Python 侧跨字段核验，不是 wire-schema 已
# 声明约束，按既有先例（task #35，_CurrentIdentitySchemaViolation 只覆盖
# 真正的 enum/required/additionalProperties 违规）必须留在语义族硬失败，
# 不得改判格式族重采样，也不放宽这道核验本身。真正根因是 prompt 规则 9
# 没说清楚"决议自己的锚定 source_label 不需要、也不允许再吸收自己"：只改
# 了下方规则 9 的措辞。换版本号只是为了让这条 prompt 变化生效——不换会让
# current_evidence_catalog_hash 相同、current_identity_version 仍是 v16
# 的旧输入，命中 discover_character_candidates 里 screenplay_identity_
# discovery 的已验证缓存工件（cached.get("current_identity_version") ==
# CURRENT_IDENTITY_DECISION_VERSION 那段），把旧 prompt 下的候选静默当成
# 新 prompt 下的结果复用，与 v14/v15 换版本号是同一个理由。
# v16:
# 人物谱持久别名（Character.aliases）并入 identity_authority_registry 的
# source_labels，且 _project_current_identity_response 里 name_kind!=
# personal_name 的短路新增"命中 reserved_authority_labels 则放行"分支（见
# 该函数内注释）。两者都改变了这份契约的决议语义：前者让 K 决议目录/
# reserved_authority_labels 内容本身变化（已随 evidence_catalog_hash 的
# contract_version 输入自然失效缓存），后者改变了对同一份 raw provider
# 响应中 n 项的后端解读结果——即使某一集的人物谱还没人登记别名、catalog
# 内容不变，这条解读规则本身也变了。contract_version 直接进 evidence_
# catalog_hash 的 hash 输入（见 _project_current_identity_response 调用处），
# 不换版本号会让同一份缓存 raw response 被新逻辑静默复用/重新解读，
# 与 v14/v15 换版本号是同一个理由。
# v15:
# k 项新增 absorbed_functional_keys（RCA ERR-20260824-bc3d14，见
# docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §2.7/§4.2 "同批折叠通道"）：模型
# 借此声明某个 K 决议吸收了本批/前批的哪些 functional 称谓组，替代此前
# 唯一能表达"这是同一个人"的违规写法（在 n 里重复申报已由 k 覆盖的身份）。
# schema 与 prompt 都变了，必须换版本号——不换会让 operation_id 撞上旧版
# 缓存的 response，静默复用不含 absorbed_functional_keys 的旧结果，本次
# 修复形同虚设（与 v14 的 scope_qualifier 换版本号是同一个理由）。
# v14: f 项新增 scope_qualifier（真实第18轮 EP10 回归 ERR-20260824-b16bb4，
# 结构性方案 a：唯一性判定键改为 (source_label, scope_qualifier) 复合键，
# 见 prompt 规则8与 _project_current_identity_response 的 by_label 分组
# 注释）。
CURRENT_IDENTITY_EVIDENCE_RECEIPT_VERSION = (
    "screenplay-current-identity-evidence-receipt.v2"
)
CURRENT_IDENTITY_LITERAL_PROVENANCE = "owned_current_literal.v1"
CURRENT_IDENTITY_SYNTHETIC_PROVENANCE = "provider_synthetic_functional.v1"
IDENTITY_ADJUDICATION_SOURCE_PROVENANCE = "owned_ir_identity_adjudication.v2"
FUTURE_IDENTITY_DECISION_VERSION = "screenplay-future-identity.v14"  # v14:
# 事故 RCA（EP2「绿袍男子」误并入「李富贵」，proj_3ac0b627fa46）：当某个
# 待消歧组的标签在整段未来文本里从未逐字出现时，resolve_future_identity_
# candidates 原先仍会盲抓未来文本开头约 900 字符当作该组的证据窗口（纯
# 兜底，只为让 N: 分支仍有文本可看），但铸造可选决议目录时没有把"这段
# 证据是不是兜底取得"这件事考虑进去——窗口里偶然出现的任何已登记角色的
# 别名/真名，都会被当成"这就是该标签的身份证据"铸出 K: 选项，模型再据此
# 选中，就把两个不同的人错误地并成了一个。现在这种纯兜底证据不再铸造任何
# K: 选项，该组的可选项收窄到只剩 F:（证据不足）与 N:（若确实首次揭示了
# 新真名）。这改变的是发给模型的可选决议目录内容与后端对同一份未来文本的
# 解读结果——不换版本号，本次事故里已经生成并持久化的错误 K 决议
# （decision_contract_version 仍是 v13）会被 screenplay_identity_
# resolution_is_current_for_scope 判定为"仍然当前"而不会被重新解析，
# 修复形同虚设（与 CURRENT_IDENTITY_DECISION_VERSION 历次换版本号是
# 同一个理由）。
# 归一规则专用 resolution_kind（真实第26轮 EP5 回归 ERR-20260824-88ece5，见
# resolve_future_identity_candidates 内 normalize_identity_payload 的完整
# 说明）：跟 "known_named"/"new_named" 并列的第三种决议种类——模型把
# "引用已有身份"误说成 NEW（authority_ids 唯一命中，冗余而非幻觉），后端
# 确定性降格为对该已有身份的引用，不要求重新逐字锚定真名（锚点在该身份
# 初次签发时已经验过）。不出现在任何 provider 可选枚举里——纯后端内部
# 归一标记，从不作为 schema token 暴露给模型，不占用 FUTURE_IDENTITY_
# DECISION_VERSION 的契约版本号（wire schema/prompt 都未改变）。
REISSUE_KNOWN_RESOLUTION_KIND = "reissue_known"
STRUCTURAL_IDENTITY_COVERAGE_VERSION = (
    "screenplay-identity-structural-coverage.v6"
)
# 身份标签（source_label / 未来揭示的真名）的防御性长度上限。这不是业务约束
# 本身，只用来拦截模型输出中明显失控的超长值（如整段抄录原文）；真正的业务
# 约束是禁止携带 _IDENTITY_LIST_SEPARATOR_PATTERN 命中的分隔符标点或空白——
# 下游（plot_spine.who / dialogue speaker / information_ledger.speaker_id /
# voice_bible.speaker_id / scene.characters）按该 pattern 切分身份列表，源头
# 混入分隔符会让一个人被错误切成多段身份。生产事故：EP7 的
# source_label='一只约莫一人大小，样子如猴般的凶兽'（17 字）曾因超过旧
# max_length=16 被 pydantic 直接拒绝，而真正应该拒绝的原因是其中的全角逗号，
# 不是长度——10 字带逗号一样危险，30 字不带分隔符反而无害。与同文件
# functional_identity_key 的 max_length=64 对齐。
IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH = 64
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
# 身份合同的输出预算。推理模型的 reasoning token 计入 completion_tokens，
# 所以"够写下答案"并不等于"够跑完这次调用"：生产上换成推理模型后，
# 4096 的预算被推理吃光，returned finish_reason=length / completion_tokens=4097，
# 每一集都在人物预检确定性截断（EP4）。这里按输出上限的量级给足余量，
# 真正的成本仍由实际用量结算，预算只是不让推理把答案挤掉。
IDENTITY_REQUEST_MAX_TOKENS = 16384

IDENTITY_NAME_FORM_PERSONAL = "personal_name"
IDENTITY_NAME_FORM_HONORIFIC = "honorific"
IDENTITY_NAME_FORM_REFERENTIAL = "referential"
IDENTITY_NAME_FORMS = (
    IDENTITY_NAME_FORM_PERSONAL,
    IDENTITY_NAME_FORM_HONORIFIC,
    IDENTITY_NAME_FORM_REFERENTIAL,
)
IDENTITY_NAME_FORM_RULE = (
    "称谓形态优先级：真名 > 尊称 > 代称。"
    "personal_name=人物的真实姓名（姓+名或单名）；"
    "honorific=姓氏或关系加称呼（如「某师姐」「某爷」），不是真名；"
    "referential=只描述外形、衣着、身份或方位的代称。"
    "只有 personal_name 才能签发新的人物身份；"
    "尊称与代称必须留作功能身份，等真名在证据中出现后再由 K 决议认领同一个人。"
)


# 「逐字」到底怎么判，以及"我认得这部作品"不构成例外。
#
# 真实事故 ERR-20260901-0fbce1 / ERR-20260901-ca388b（《金瓶梅词话》第一回，同一
# 提示词下连撞两次）：本批 16 条证据里「武大」出现 52 次、「武植」出现 0 次，模型
# 仍然按民间说法把这个人写成本名「武植」并绑到一条讲汉高祖四皓的证据上，整集预检
# 硬失败。旧版规则 3 只写了「identity_label 必须是所选 E text 的连续逐字子串」——
# 这句话是对的，但没有覆盖模型真正越界的那一种形态：它读到了「武大」，却用记忆里
# 的原著知识把名字换掉了。CLAUDE.md「写完整的正面陈述，不写禁令」：这里要说清判据
# 是字符串包含、说清"认得这部作品"与写什么名字无关、并说清读到的称谓形态不合适时
# 该往哪写，而不是再堆一条"不许编名字"。
IDENTITY_LITERAL_LABEL_RULE = (
    "判据是字符串包含：把 identity_label 拿到所选 E 的 text 里做一次精确查找必须能找到，"
    "字形、读音、称呼习惯相近都不算命中。"
    "你是否认得这部作品、这个人物，与该写什么名字无关——"
    "即使你知道他的本名、全名、字号或后世通称，只要证据文本里没有逐字出现，就不得写出来；"
    "只写你在这批证据里实际读到的那个称谓（原文写「武大」就写「武大」，"
    "哪怕你知道民间说法里他本名叫「武植」）。"
    "如果实际读到的称谓形态不是 personal_name，按第 4 条放进 f，"
    "不要为了凑一个像真名的名字去调用原著知识。"
    "同一个人只写你读到的那一个称谓：写出证据里的称谓之后，"
    "不得再为他补一条你记得的本名/全名/字号——那既是证据里没有的名字，"
    "也会被当成另一个人，整集会因此判失败。"
)


# 同一事故的另外两处落点：一条紧贴证据目录（模型读完 text 立刻看到），一条放在
# 提示词最后一行（提交前自检）。只把这句话写在规则 3 中段是不够的——实测改完规则 3
# 之后重跑，模型照样写了「武植」，还多编了一个「武大郎」（原文同样 0 次）。
IDENTITY_EVIDENCE_ONLY_NAME_SOURCE = (
    "以上 text 是本次唯一的名字来源。响应里出现的每一个 identity_label 与 source_label，"
    "都必须能在它所选的那条 E 的 text 里原样查到；这批 text 之外的任何名字都不得写出——"
    "包括你从这部作品的其它版本、影视改编或民间说法里知道的本名、全名、字号与后世通称。"
    "同一个人在原文里叫什么，就只能写什么。"
)

IDENTITY_LITERAL_SELF_CHECK = (
    "提交前逐条自检 n 与 f：把每一个 identity_label / source_label 拿到它所选 E 的 text 里"
    "做一次精确查找；查不到的那一项，改成你在该 text 里真正读到的那个称谓，"
    "改不出来就删掉这一项，不得凭记忆保留。"
)


# 规则 9（absorbed_functional_keys 的合法取值域与"功能性占位"前提）整块从
# current_identity_prompt.py 的 f-string 里搬出来，一字未改。搬移理由与
# 该模块 docstring 里写的一样：_current_identity_prompt 顶着 function_lines
# 棘轮（122 行，零余量），而这次要在证据目录后面和提示词末尾各加一行正面
# 陈述——"装不下时先想怎么拆，不要先想加基线"。文本内容不属于本次改动。
IDENTITY_ABSORBED_KEYS_RULE = """9. absorbed_functional_keys 的合法取值域只有三类，逐项必须精确复制其中
   之一——本批 f 项自己声明过的 functional_identity_key、前批 P token
   （prior functional 分组的 decision_id）、或本集已有功能身份决议的
   canonical_name；不是任意你认为"指代同一人"的称谓原文。后端只核验每个
   token 是否确实来自这三类来源，不做文本语义判断，越界或臆造的 token
   （包括任何未按上述三类之一先行声明过的称谓原文）都会导致本次响应被
   拒绝重试。

   这三类来源有一个共同前提：token 背后的实体在被吸收前必须处于"稳定真名
   尚未确认"的功能性占位状态——这正是规则4"若…无法确认稳定真名，放入 f"
   的适用范围。一个人只要已经有确定真名（不论是这条 k 决议刚揭晓的，还是
   人物谱/更早证据里早已确认的），TA 的其它称谓从一开始就不满足"功能性
   占位"这个前提，永远不构成合法的 f 项，也就永远不会出现在上述三类合法
   来源里——不得为了让某个称谓能被吸收，倒着现造一条 f 项把它包装成功能性
   占位；f 项存在的理由是"真名未定"，不是"我想吸收它"。这类已有确定真名
   之人的其它称谓，走称谓解析的正常渠道（n 的逐字自称谓声明、或人物谱别名
   登记），不进 absorbed_functional_keys。（真实事故：「孟才子」「孟兄」是
   孟浩的称谓、「王伯的儿子」是王有材的称谓、「许师姐」是许清的称谓——这
   四人都已有确定真名，从一开始就不是合法的 f 项，任何 k 决议都不得把这类
   称谓原文填入 absorbed_functional_keys。）

   合法用例：如果某个 k 决议揭晓的真名，其实就是一个仍处于 functional 状态
   的称谓组一路指代的同一个人——例如某绰号从更早的证据起就被追踪为
   functional，直到这条 k 决议对应的证据才第一次读到该人物的真名——不要
   把真名重复写进 n（那是这条 k 决议已经覆盖的重复声明，会被拒绝）：改为
   在这条 k 决议里填写 absorbed_functional_keys，逐项精确复制被吸收的
   functional_identity_key/P token/canonical_name。只有在你确实判断这些
   token 指代的是同一个人时才填写；拿不准是否为同一人时留空，不要吸收。

   absorbed_functional_keys 里禁止填入这条 k 决议自己的 source_label（即
   本批 K 决议目录里这个 decision_id 条目自带的 source_label 原文）：选中
   decision_id 本身已经表达了这个称谓属于该决议，重复列出会被判定为越界
   token 而拒绝，不是多填了一道保险。absorbed_functional_keys 只能用来
   吸收这个自身称谓之外的、真正处于功能性占位状态的其它称谓组（不是任何
   已有确定真名之人的称谓，见本条前半段）——如果某个这样的称谓只是你在
   证据里零散认出、还没有单独作为一条 f 项列出（source_label 与 functional_
   identity_key 均已确定），它就还不是合法的可吸收 token：必须先在本响应
   的 f 数组里为它单独声明一条 f 项（source_label 填该称谓本身，
   functional_identity_key 可以直接使用你打算吸收的同一个 key），再在
   absorbed_functional_keys 里精确复制那个 key。第7条"每个人只输出一次"
   约束的是同一个人不得被同时判给两个互相冲突的最终身份归属，不禁止你为
   将被吸收的称谓单独声明它自己的 f 项——被吸收的 f 项与吸收它的 k 决议
   共存，就是这条通道设计的正常形态。"""


AUTOMATIC_IDENTITY_DECISION_PROVENANCE = "automatic_identity_discovery.v1"
DURABLE_IDENTITY_DECISION_PROVENANCE = frozenset({"manual", "bible"})

