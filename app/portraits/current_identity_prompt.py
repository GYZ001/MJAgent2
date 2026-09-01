"""当前身份决议（current-source RF11 K/N/F wire）的 schema 与 prompt 构造。

从 ``discovery_legacy.py`` 拆出：原来内联在
``_discover_character_candidates_legacy`` 批次循环尾部的一段（把本批证据目录/
已登记身份目录/前批 functional 分组/本集既有功能身份决议投影成模型输入，拼出
带规则说明的 prompt 文案），本身不含判定逻辑，是纯粹的「组装模型输入」阶段，
与 ``future_identity_prompt.py`` 同一拆分理由——``discovery_legacy.py`` 当时
已经顶格贴着 ``line_count``/``function_lines`` 两条棘轮基线（零余量），新增
提示词内容必须先腾出地方，而不是把基线继续往上调。

2026-09-01 真实 EP1 回归 ERR-20260901-cfff07 / ERR-20260901-dded96：同一份
prompt 下，模型两次把 k/n 错误嵌进了 f 数组元素内部——
``{"f": [{"k": [], "n": [...]}]}``——而不是三个同级根数组。查证：
``_identity_strict_provider_schema``（见 identity_schemas.py）发给 provider 的
schema 白名单不含 description/title，所以 schema 本身不能携带任何字段级说明
文字；prompt 是唯一能讲清「这是三个同级数组、元素内部不得再出现 k/n/f」的地方，
而旧版 prompt 在规则 1 里只用一句话带过，从未给过一个实际的最小合法形状实例。
两次失败的原始响应（见 provider_calls.response_json）显示模型在写到某个 f 元素
的 "kind" 字段时直接接上了 "n": [...]，与本文件 docstring 引用的 RF10→RF11
迁移历史吻合：RF10 曾经是「每个 evidence span 配一个 K/N/F 分类子对象」，模型
不时会滑回那个更常见的「逐项挂三键」旧形状。下面新增的显式形状示例直接针对这个
已实测复现两次的失败形状；不改 schema 校验本身——畸形结构仍然该拒就拒，不兜底。
"""

from __future__ import annotations

import json

from .constants import IDENTITY_NAME_FORM_RULE
from .identity_schemas import (
    _current_identity_schema,
    _identity_strict_response_format,
)


def _current_identity_prompt(
    *,
    episode_no: int,
    known: str,
    prior_functional_projection: list[dict],
    evidence_catalog: list[dict],
    known_decision_projection: list[dict],
    existing_resolution_projection: list[dict],
    evidence_refs: list[str],
    known_decision_ids: list[str],
) -> tuple[dict, dict, str]:
    current_schema = _current_identity_schema(
        evidence_refs,
        known_decision_ids=known_decision_ids,
    )
    current_response_format = _identity_strict_response_format(
        current_schema,
        name="screenplay_current_identity_discovery_v11",
    )
    prompt = f"""任务：为第 {episode_no} 集做人物身份增量预检。请用语义和上下文判断，
不要依赖服饰、性别、年龄或称谓后缀的固定词表。

当前人物谱已有角色：
{known}

前批已确定的 functional 分组 P 决议（后批判断为同一人时，functional_identity_key 必须精确复用 decision_id）：
{json.dumps(prior_functional_projection, ensure_ascii=False, separators=(',', ':'))}

本批 backend-owned 当前身份证据目录。E ref 已绑定完整证据 receipt，禁止跨 E 搬运人物：
{json.dumps(evidence_catalog, ensure_ascii=False, separators=(',', ':'))}

本批已登记身份 K 决议目录（只有这些 decision_id 可进入 k；目录为空则所有 k=[]）：
{json.dumps(known_decision_projection, ensure_ascii=False, separators=(',', ':'))}

本集已有功能身份决议（可为空；canonical_name 是已分配的本集稳定 ID）：
{json.dumps(existing_resolution_projection, ensure_ascii=False, separators=(',', ':'))}

响应根对象的形状固定为三个同级数组 k/n/f，互不嵌套：k、n、f 只在根对象出现一次，
某一类没有内容就写空数组 []，不得省略该键或用其它结构代替；n、f 数组里的每一个
元素内部都不得再出现 "k"、"n" 或 "f" 这三个键——它们是根对象专属的键名，不是可以
逐项挂在数组元素上的分类字段。最小合法形状示例（仅示范结构，取值不代表本次真实
判断）：
{{"k": [], "n": [{{"evidence_ref": "E001", "identity_label": "孟浩", "name_kind": "personal_name", "kind": "onscreen"}}], "f": []}}

规则：
1. root 只输出一次 k/n/f 三个全局数组，不得输出 decisions，也无需覆盖没有人物的 E。
   每个身份/称谓只输出一次，从它的 owned 证据中选最清晰的 E；同一 E 可支持多人。
2. 已登记身份只可选 k：decision_id 精确复制 K 目录，该 token 已绑定 E；
   kind 必须属于该 K 的 allowed_kinds；
   不得把 K 目录中的 source_label 写进 n/f。只允许 mentioned 的 K 没有可安全物化的最终人物卡
   authority；若人物实际出镜则必须停止而不能谎报 mentioned 或另造身份。
   本批 K 目录没有为「当前人物谱已有角色」中的某人签发 decision_id，说明本批证据没有
   逐字锚定他：此时既不得把他的真名写进 n，也不得据上下文推断，只能把你实际读到的
   逐字称谓按第 4 条放入 f，交给后续带 authority_id 的权威绑定去认领。人物谱名单只用于
   识别，不是可以直接书写的名字。
3. 当前阶段的新 named 只用于逐字自称谓：n 每项写 evidence_ref、identity_label、name_kind 与 kind，
   identity_label 必须是所选 E text 的连续逐字子串；后端会令 canonical_name=source_label。
   {IDENTITY_NAME_FORM_RULE}
   name_kind 只描述 identity_label 这个字符串本身的形态，与你是否认得这个人无关；
   尊称或代称请照实写 honorific/referential，后端会自动把它落为功能身份。
   任何“称谓 A 其实是名字 B”的别名判断，即使 A、B 同时出现在当前输入，也必须先判为
   functional，交由后续带 authority_id 的权威绑定；不得用同场共现代替同一性证据。
4. 若是一次性角色，别名待后续确认，或无法确认稳定真名，放入 f；每项填写
   evidence_ref、source_label、functional_identity_key、kind，不得携带 canonical/authority/evidence。
   source_label 尽量逐字复用所选 E text；若为区分同段多个无名实体而
   必须使用非逐字的稳定描述，只能保留为 functional，后端会隔离为 synthetic identity，
   不得将它当作别名或真名。
   若同一实体在证据里有多个逐字可用的称呼（如「凶兽」与「一只约莫一人大小，
   样子如猴般的凶兽」），必须选其中最短的那个稳定称谓：source_label 只是一个
   可复用的身份标签，不是用来证明你读到了完整描述。
   source_label 不得包含 、，,／/；;｜|＆&＋+ 等分隔符标点或空白：后端会按这些
   字符切分身份列表（如台词发言人、场次角色表），混入分隔符会让一个人被错误
   切成多段身份。
5. 若身份投影中的 source_label 混入动作或表演提示，必须结合对应 line_context 判断真正说话人；
   source_label 保留原始完整字符串，canonical_name/functional_identity_key 绑定到真正说话人。
   禁止按“说、喊、点头”等固定词表或后缀规则猜测。
6. 每个 f 项必须填写 functional_identity_key：
   - 若它与“前批已确定的 functional 分组”是同一人，必须精确复制该 P decision_id；
     不得重新使用前批原始分组字符串。
   - 若它与“本集已有功能身份决议”中的某人是同一人，精确填写该人的 canonical_name。
   - 否则填写本次响应内的不透明分组 ID（如 F1、F2）；不同 source_label 若明确是同一人必须共用同一 ID。
   - 无法确认是否同一人时必须使用不同 ID，禁止根据称谓字面相似猜测。
7. 每个人只输出一次；不得因多次出现重复输出，不得因共用证据合并人物，
   也不得把同一 source_label 放入多个分支。后端只会聚合语义签名完全相同的合法重复，
   任何跨分支、跨分组或非逐字 synthetic 重复都会硬失败。
8. f 每项还需要填写 scope_qualifier（默认可留空字符串）：如果同一个 source_label
   在本批不止一次出现、且这几次实际指的不是同一个人——比如「师弟」「师兄」「道友」
   「前辈」这类相对说话人或语境而定的关系称谓/身份指代，本就可能在同一批里对应
   不同人——必须给每一次单独填一句简短、能从对应证据里直接读出依据的限定语，说明
   这次具体是哪一个人（如取自证据的动作、对话对象或所在场景），确保同一 source_label
   下不同的人各自的 scope_qualifier 互不相同。如果这几次确实是同一个人反复出现，
   或这个称谓本就唯一指向一个人，scope_qualifier 留空即可。判断依据是"这次读到的
   是不是同一个人"，不是称谓字面是什么词；拿不准时倾向于填写限定语而不是留空，
   避免把两个不同的人误合并成一个人。同一 source_label 下用不同
   functional_identity_key 申报了多个人时，必须各自填写能互相区分的
   scope_qualifier——不要依赖后端的确定性降级补足（后端会用甲/乙/丙...
   兜底填一个可用但没有语义信息量的限定语，只是防止拒绝重来，不是让你
   可以不填）。
9. absorbed_functional_keys 的合法取值域只有三类，逐项必须精确复制其中
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
   共存，就是这条通道设计的正常形态。
只输出 response_format 约束的 JSON，不要复述证据、Schema 或规则。"""
    return current_schema, current_response_format, prompt
