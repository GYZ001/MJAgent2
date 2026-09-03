"""分镜台 2.0.0：把映射台 (episode_prep_pack) 的产出直接生成可投喂视频模型的分段提示词。

背景（docs/STORYBOARD_PROMPT_IR_DESIGN.md，2026-08-26 用户拍板）：
- 剧本台已转型映射台（提交 48e01ff），episode_prep_pack 契约升到 2.0.0，
  不再产出 event_chain / hook / cliffhanger。旧的「事件链 -> 大纲 -> 逐镜」
  管线（app/stages.py 的 generate_storyboard_outline 及其调用的一整套
  narrative_plan 相关校验）是为 event_chain 驱动的叙事权威合同设计的，
  对 episode_prep_pack 这种「只出人物/场景/道具映射，不出事件」的输入
  结构性不适用。
- 这一模块是给 episode_prep_pack 输入专设的新生成路径，不复用旧的
  outline/逐镜/repair 状态机：分两阶段调用模型——
    阶段一：把本章原文（按 source_excerpt.index_source_segments 分段，
      与映射台使用的是同一套分段函数，segment_index 对齐）交给模型，
      产出节拍表（beat_sheet）与节拍到段的归组（一段 = 一个叙事单元，
      固定 15 秒 / 2-4 镜）——这一步决定「这一集有几段」，是整个改造的
      支点：取消事件链之后，段数不再由上游给，必须由本阶段从原文推导——
      2.1.0 起还决定「这一段有哪些必须说出口的原文台词」（见 storyboard_dialogue_ledger）。
    阶段二：为每一段各发一次独立调用（``_generate_all_segment_prompts``
      内部按 segment_no 顺序串行推进，不是 asyncio.gather 并行），每次
      调用只带这一段自己的原文切片 + 节拍 + 相关人物/场景/道具资源 +
      目标模型（Seedance 2.0 / MiniMax H3）的方言约束，模型产出一整块
      可复制的 prompt_text（代码不再拼装、不挂尾缀——对照
      app/video_prompt_ai.py 的 _render_seedance_prompt /
      _render_minimax_h3_prompt，那是本模块要替代的、按草稿字段拼接最终
      字符串的旧路径；这里模型的 prompt_text 只做 strip()，不做任何字段级
      重组）。这一步在 2.0.3～2.0.7 之间是「整集一次批量调用」，2.0.8 改回
      逐段独立调用、但带三层续接上下文（上一段定稿的 prompt_text 全文、
      最近几段的开场镜头语言清单、本段角色的世界书外观锚点）分别顶替批量
      调用曾经解决的三个缺陷（角色换装/转场生硬/镜头语言重复）——完整推导
      见下方 STORYBOARD_PACK_VERSION 的 2.0.3 与 2.0.8 两条 changelog。

持久化形状（用户已拍板，不是本模块自行决定）：一个 15 秒段 = shots 表一行，
段内的 2-4 个镜头切换写在 prompt_text 文本里，不拆成独立数据行。因此
shot_size / camera_move / camera_angle 这类描述单个连续镜头的字段在这里
粒度失效，本模块写入的新架构行一律留空，改用 Shot.storyboard_pack_segment
承载完整的冻结契约段记录（prompt_text / resources / dialogue /
degraded_capabilities / source_segment_indexes / shot_count）；
app/continuity.py、app/validators.py、app/domain/video_ops.py 对应位置的
校验器已按这个 marker 字段显式退役/改判旧的单镜构图假设，而不是让它们
对新架构的行悄悄判错或悄悄放行。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from app import config, hiagent, spoken_contract
from app.db import new_id
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.production.storyboard_capacity_normalize import normalize_and_assert_capacity
from app.production.storyboard_pack_identity import resolve_persisted_character_ids
from app.production.storyboard_pack_montage import fill_montage_beat_time_anchors
from app.production.storyboard_dialects import (
    MINIMAX_H3_DIALECT_INSTRUCTIONS,  # noqa: F401 -- 重新导出，测试按旧路径 import
    SEEDANCE_DIALECT_INSTRUCTIONS,  # noqa: F401 -- 重新导出，测试按旧路径 import
    _dialect_for_target_video_model,
)
from app.production.storyboard_beat_sheet import (
    _AiBeat as _AiBeat,
    _AiBeatSheetDraft as _AiBeatSheetDraft,
    _AiSegmentPlan as _AiSegmentPlan,
    _beat_sheet_rules as _beat_sheet_rules,
    _generate_beat_sheet,
    _paratext_exclusion_rule as _paratext_exclusion_rule,
    _paratext_segment_indexes as _paratext_segment_indexes,
    _source_block_for_prompt as _source_block_for_prompt,
    _validate_beat_sheet_draft as _validate_beat_sheet_draft,
)
from app.production.storyboard_continuity_memo import (
    _AiContinuityMemo,
    continuity_memo_character_advisories,
    continuity_memo_errors,
    continuity_memo_output_contract_text,
    continuity_memo_payload,
)
from app.production.storyboard_dialogue_extract import extract_dialogue_targets
from app.production.storyboard_dialogue_ledger import (
    dialogue_ledger_summary,
    required_dialogue_for_segments,
    required_dialogue_missing_errors,
    required_dialogue_rule,
)
from app.production.storyboard_dialogue_repeat import (
    already_delivered_dialogue_rule,
    already_delivered_payload,
    repeated_delivery_errors,
)
from app.production.storyboard_narrative_arc import (
    _segment_continuity_rules,
    _segment_shared_rules,
    phase2_segment_rules,
    segment_narrative_arc_payload_fields,
)
from app.production.storyboard_segment_ranges import (
    _PARATEXT_PLACEHOLDER_TEXT,
    segment_source_payload,
)
from app.schemas import Bible, Dialogue, MontageBeat
from app.validators import resource_advisories_for_segment
from app.source_chapters import _episode_source_text
from app.source_excerpt import SourceSegment, index_source_segments

#: 2.0.1（真实 EP1 回归，ep_3d523ff4d0a4/run_46660b74d025，三个逐段核对发现
#: 的产出缺陷）：送模型的 task_payload 形状变了——relevant_assets.
#: characters[]/functional_extras[]/scenes[] 新增世界书标准外观/场景锚点
#: （appearance/scene_canonical），phase 2 新增 rules[] 三条自洽要求；两个
#: 方言指令块也各补了一条硬要求。持久化契约（StoryboardPack/
#: StoryboardPackSegment 的字段名与形状）没有变，只是补丁级修正，不是 minor
#: ——但必须换版本号：不换的话 run_storyboard_pack_generation 的 resume 分支
#: 会看见 EP1 已持久化 shots 的 shot_contract_json 里仍带着旧版
#: STORYBOARD_PACK_CONTRACT_MARKER，判定"已经用同一套契约生成过"直接复用
#: 旧结果，不会真的用新 prompt 重新调模型（resume 短路机制本身不动，只是
#: marker 值变了才会让它对旧行判"不算数"）。
#:
#: 2.0.2（真实 EP1/EP6/EP7 十集回归横向核对发现的缺陷）：EP7 的 8 条
#: resources.characters[].identity_id 引用被模型自造成「character:」
#: 「char:」「ch:」三种前缀，一条真实 bible:/entity: 前缀都没写对，且全部
#: 8 条一个 [STORYBOARD_PACK_RESOURCE_CHARACTER_UNKNOWN] 降级标记都没触发
#: ——比 EP6 用旧映射包跑、至少还挂出同名标记的年代更静默。根因两处，都在
#: 本文件：① _segment_content_advisories 里 `known_character_ids and ...`
#: 这个真值判断，在 known_character_ids 恰好是空集（EP7 的 prep_pack 这次
#: 没能把「孟浩」解析进 asset_manifest.characters/functional_extras）时把
#: 整条判断短路跳过——空取值域被当成「不用查」，而不是「取值域里什么都不
#: 合法」，改成无条件成员检查（scene 侧同构一并改）；② phase 2 的
#: task_payload.rules 里 identity_id 来源那条规则只有"只能用 X 或 Y、不得
#: 自造"的禁令式收尾，没有正面说清"取值域从哪来、必须逐字整串复制"，也没
#: 覆盖"角色确实不在 relevant_assets 里该怎么写"这个边界情形——模型在没有
#: 任何一条可抄的合法样例时（EP7 的 relevant_assets.characters 恰好也是
#: 空的）只能按格式直觉现编，三种前缀正是这么来的；改成完整正面陈述 + 显式
#: 交代未收录角色的兜底写法（原文称谓本身，不加前缀）。持久化契约字段名/
#: 形状不变，纯属检测口径与提示词措辞的修正，换版本号是为了让 resume 分支
#: 不会拿旧 marker 的行当"已经用新判据/新提示词生成过"而误判复用。
#:
#: 2.0.3（用户从链路观测里看出分镜割裂感的根因并拍板，2026-08-26）：阶段二
#: 从「每段一次独立模型调用、asyncio.gather 并行发出」改成「整集一次模型
#: 调用产出全部段落的 prompt_text，再按 segment_no 分配回各段」——旧路径下
#: 生成第 N 段的那次调用既看不到第 N-1 段写了什么、也不知道第 N+1 段会写
#: 什么，跨段视野为零，是角色换装、场景转场生硬、镜头语言重复等割裂感的
#: 结构性根因（旁证：同一晚早些时候修的"同一角色换三套衣服"问题，当时的
#: 补丁是把世界书标准外观塞进每一段的载荷、强制逐字复用——那是在用"给每段
#: 发同一份标准答案"补偿跨段视野的缺失，不是解决根子）。
#:
#: 落地前先做了真实体量测量，不是先改后猜（现有各集 prompt_text 长度分布：
#: EP1/EP2/EP6/EP7 等十集实测均值约 700-1100 字符/段；EP3 12 段——本项目
#: 已出现的最大段数——合计约 22.9K 字符）。用实际部署模型反推真实
#: chars/token 比例：provider_calls 表按当前 storyboard_pack 使用的模型
#: （'d71l5c8nfdb167kligqg'）统计的 5125 次历史调用，chars/token 中位数
#: 2.29（不是 cl100k 通用估算的 ~1.1，该模型的中文 token 效率明显更高），
#: 换算下来 12 段合并输出约 1.0-1.3 万 token；即便按"段数再涨 33%、单段
#: 再长 30%"的双重压力场景估算也只到约 2.2 万 token，仍在该模型
#: max_output_tokens=32768 硬顶内、留有约三成余量。同一批 5125 次历史调用
#: 里 reasoning_tokens 全部为 0（这个模型配置不会偷输出预算去"思考"），
#: 真正因为触及 32768 硬顶而失败的历史调用只有 1 次（0.02%），其余 5 次
#: finish_reason=length 截断都是调用方自己把 max_tokens 定得比实际需要小
#: （4096/6144/7596/10752/16884），不是模型上限不够——因此本次批量调用的
#: max_tokens 按段数线性放大（见 SEGMENT_BATCH_TOKENS_PER_SEGMENT），不重
#: 蹈同一个坑；chat_structured 对 finish_reason=length 是硬失败、不重试、
#: 不做局部 JSON 恢复（app.hiagent._reject_truncated_chat_response），量不
#: 够就是整段失败，所以必须先量后动手，不能事后才发现。
#:
#: 残留风险，改造时就已知、写在这里不是事后补充：① 段数没有代码强制上限
#: （今天最大 12 段，不保证以后不会更多，真出现更长章节时上面的余量会被
#: 吃掉，需要重新量一次而不是假设结论还成立）；② 失败粒度的准确说法不是
#: "以前丢一段、现在丢整集"——persist_storyboard_pack 本来就是单事务、要么
#: 整份写完要么什么都不写，旧路径 asyncio.gather 不传 return_exceptions=True，
#: 任何一段的 chat_structured 耗尽自己的重试预算就会让整个 gather 连带
#: generate_storyboard_pack 一起失败，persist_storyboard_pack 同样一次都不会
#: 被调用——"落库要么整份要么全无"这条早就成立，不是这次改造引入的。真正
#: 变化的是独立模型调用的只数：从"12 次并行调用、任何一次耗尽重试预算都
#: 拖垮整体"（12 个独立失败点，聚合失败概率通常比单次调用更高）降到"1 次
#: 调用"（1 个失败点，但这次调用本身的输入更长、要求一次满足的约束更多，
#: 单次失败概率不为零）；重试成本方向也随之变化——旧路径重试要重新发起全部
#: 12 次调用，新路径重试只重新发起这 1 次。定位失败段落的能力没有跟着退化：
#: _validate_all_segments_draft 的报错逐条带 segment_no 前缀，哪一段不满足
#: 格式仍然可读。
#:
#: 2.0.4（用户从链路观测里看出「生成可执行分镜」节点输入含作者感言并拍板，
#: 2026-08-26）：喂给模型的两处原文文本（_generate_beat_sheet 的
#: source_block、_generate_all_segment_prompts 的 full_source_text）不再把
#: 映射台 coverage_ledger.paratext 账里的段落原文一起拼进去——复用映射台
#: 已经算好的账（章节标题的确定性识别 + 模型自报的作者话/求票/报字数尾记，
#: 见 app.production.prep_pack._prep_pack_build_coverage_ledger），不重新
#: 造一套识别逻辑（本文件不改 prep_pack.py 一行）。真实数据：EP8 段61=
#: 「又是大章，请登陆起点账号点击，这才算一个会员点击，拜求」——此前这段
#: 原文逐字进了 task_payload.source_text_by_segment，被当正文喂给了模型。
#:
#: 段号稳定性是这次改造的硬约束：source_segment_indexes 和
#: storyboard_source_bindings 的整条链路都靠"段号=index_source_segments 的
#: 1-based 位置"这个假设成立，paratext 段落被剔除后不能重新编号，否则下游
#: 全部指错地方。做法是"略去原文但保留段号"——paratext 段落的原文替换成
#: 固定占位说明（_PARATEXT_PLACEHOLDER_TEXT，不含任何原文字符），segment
#: 编号原样保留在 "[段N] ..." 里，不压缩、不重排；两处 rules[] 也各加一条
#: 显式禁止模型把这些段号编入任何 beat/segment 的引用。_load_indexed_
#: source_segments 返回的 segments 列表本身不做任何过滤——它的真实 offset/
#: text 仍然是 _resolve_segment_source_binding 用来核验 storyboard_source_
#: bindings 的唯一依据，这个函数读的是 chapters.content 的真实字节，不读
#: 送给模型的那份占位化文本，两者从一开始就是分离的两条数据路径，改其中
#: 一条不影响另一条对段号的解释。
#:
#: 防御性兜底（generate_storyboard_pack 里的 _strip_paratext_from_beat_draft）：
#: stage 1 的 rules[] 只是提示词层面的禁令，不是校验闸门——_validate_beat_
#: sheet_draft 只查段号越界，不查是否落在 paratext 账里，模型仍有可能无视
#: 提示把 paratext 段号写进某个 beat.segment_indexes 或某个 segment.source_
#: segment_indexes。这一步在 beat_draft 通过格式校验之后、进入 phase 2 之前，
#: 用同一份 paratext_indexes 数据把这类引用过滤掉——纯确定性列表运算，不
#: 是新增一道模型语义判断或黑名单，判据仍然全部来自映射台已经算好的账。
#: 唯一的例外是"过滤后这个 beat/segment 会变成空引用"：宁可保留一个不该
#: 在的 paratext 段号，也不留一个没有任何原文依据的空引用（后者会让
#: _resolve_segment_source_binding 直接抛错，整集生成失败）——这种情况极
#: 罕见（要求一个 beat/segment 的引用来源全部是 paratext），出现时不静默
#: 吞掉，_strip_paratext_from_beat_draft 会在 degraded_capabilities 之外
#: 另记一条 note 留痕（见该函数文档）。
#:
#: 旧契约分集兜底：coverage_ledger 缺失或没有 paratext 账（键不存在/不是
#: list）时 _paratext_segment_indexes 返回空集，两处 source_block 的行为
#: 退化为"每个段号都不是 paratext"——即改造前的全量路径，不崩、不会把不存在
#: 的账目误读成"全部段落都是 paratext"（那会让模型看到的原文变成清一色
#: 占位符）。
#:
#: 2.0.5（协调方从 EP4/EP5/EP6 横向统计发现批量产出的 resources.scenes 明显
#: 变稀：EP4（新架构整集批量）9 段里 5 段是 0 个场景，EP5/EP6（旧架构逐段
#: 独立调用）分别只有 0/9 段是 0——2026-08-26）：核对 EP4 真实数据后，根因
#: 不是"批量输出稀释了模型对 resources.scenes 的注意力"这个最初的怀疑，而是
#: 两处都有问题，程度不同：
#: ① 真正的主因是上游数据缺口，不是本文件的提示词——EP4 的
#: asset_manifest.scenes 只有 1 个场景条目，segment_indexes 覆盖范围是
#: [2..20]，但本集原文共 54 段；产出 0 场景的 5 段（shot 5-9）source_segment_
#: indexes 全部落在 [24..51]，完全在这个覆盖范围之外——_segment_relevant_
#: assets 按段号交集筛出的 relevant_assets.scenes 对这 5 段来说本来就是空
#: 列表，模型没有任何合法 scene_id 可用，留空是唯一诚实的选择，换成 EP8 用
#: 同一套（未改动）提示词重新生成一次可验证：EP8 的 asset_manifest.scenes
#: 覆盖了全部段号，8 段全部至少有 1 个场景。这一半根因在
#: app.production.prep_pack 的场景发现，不在本文件，本次不动 prep_pack.py
#: （范围外、真出问题需要单独一次映射台改造，不是分镜台能补的）。
#: ② 但确实还有本文件自己的、次要的一个提示词失衡：task_payload.rules[] 里
#: resources.characters 的取值来源、边界情形（未收录角色怎么写）都有完整
#: 正面陈述，resources.scenes 却完全没有对应的规则条目——唯一提到它的地方
#: 是 output_contract.segments[].resources 里一句话带过。批量调用要一次
#: 满足的约束本来就多，被动省略的字段更容易被跳过；即使 relevant_assets.
#: scenes 非空，也不能保证模型会主动把画面里的场景填进去。补一条与
#: characters 同结构的正面陈述规则（取值来源=该段 relevant_assets.scenes[]
#: 的 scene_id 字段本身，边界情形=relevant_assets.scenes 为空时留空是唯一
#: 诚实选择），同一族"正面陈述取值域，不是简单禁令"的教训（2.0.2
#: changelog 的 identity_id 那次已经验证过这个方向有效）。
#:
#: 可见信号，不做兜底填充（用户/协调方拍板：空着比编一个假场景更诚实）：
#: _segment_content_advisories 新增两条互斥的 degraded_capabilities 标记，
#: 只在 resources.scenes 为空时才判断——
#: [STORYBOARD_PACK_RESOURCE_SCENE_MANIFEST_GAP]：这段的 relevant_assets.
#: scenes 本身是空列表（① 的情形，根子在映射台，不是这次生成的遗漏）；
#: [STORYBOARD_PACK_RESOURCE_SCENE_MISSING]：relevant_assets.scenes 非空但
#: 模型仍未声明任何场景（② 的情形，可能是真的没有独立场景——例如纯特写/
#: 纯对话，也可能是遗漏，代码分不清这两种，只能标记出来交给人核对，不代为
#: 判断）。两条标记名字互斥、依据都是确定性的集合运算（relevant_assets.
#: scenes 是否为空），不是新的模型语义判断，也不是黑名单。
#:
#: 2.0.6（生产事故：生成可执行分镜 finish_reason=length、reasoning_present=
#: True、completion_tokens=32768，整集 escalate）：2.0.3 把阶段二收成「整集
#: 一次调用」，测量依据是当时模型的 5125 次历史调用 reasoning_tokens 全部
#: 为 0，「这个模型配置不会偷输出预算去思考」。当前文本路由是思考型模型，
#: 思考 token 与答案共用同一份 completion 预算（app.hiagent.text_request_
#: token_limits 叠加 TEXT_REASONING_TOKEN_RESERVE=16384，再夹到模型
#: max_output_tokens=32768）。段数 ≥7 时答案预算 2400×N 加上思考预留已经
#: 顶满 32768，截断抬升（truncation escalation）发现自己已经在上限上，无
#: 处可去，整集失败。2.0.3 自己写下的残留风险 ①（「段数没有代码强制上限
#: ……真出现更长章节时上面的余量会被吃掉」）在模型换成思考型的那一刻就
#: 提前兑现了，不是段数真的超过 12。
#: 修复：阶段二仍按整集视野写（全部节拍/原文/素材清单每次都给，后续批次
#: 带上 already_written_segments 承接已写提示词），但每次调用的答案预算
#: 必须严格小于 (max_output_tokens - reasoning_reserve)，装不下就顺序分
#: 批。这不是退回 2.0.3 之前的逐段并行——那条路径的跨段视野为零，是割裂
#: 感的结构性根因，不能为了预算再拆回去。持久化契约字段名/形状不变，
#: STORYBOARD_PACK_CONTRACT_MARKER 故意留在 2.0.5：resume 短路只看 marker，
#: 这次没有改产物形状，已成功的集不该被强迫重跑一次付费生成。
#:
#: 2.0.7（《黄英》EP1 镜6 实测）：「@点名即可、长相交给参考图」这条规则默认
#: 每个被 @ 的角色都有图，但本段还会出现只在原文里有称谓、relevant_assets
#: 查不到的人。吕氏就是这样进来的——她是映射台的 functional_extras，模型在
#: 镜头2 写了她的外观、镜头3 改成「固定 @吕氏 把一小袋粮食递到黄英手里」，
#: 而 @吕氏 绑不到任何参考图：图没有、文字又按规则不再描述长相，她这一镜的
#: 长相彻底没有来源。本段 CHARACTER_UNKNOWN 降级信号当时声称「已按纯文字
#: 描述处理」，实际什么都没做，只记了一条 advisory。
#: 修复：方言指令补上未收录角色的正面写法（不用 @ 前缀、每镜自带三项以上
#: 可视觉验证特征），判据是 relevant_assets.characters 里查不查得到；降级
#: 信号措辞改成如实陈述。只改提示词与措辞，落库形状不变，marker 仍在 2.0.5。
#:
#: 2.0.8（用户拍板，2026-08-29：阶段二从「整集一次批量调用」改回「逐段独立
#: 调用」，但带一镜参考）：2.0.3 把逐段独立调用改成整集打包，理由是逐段调用
#: 跨段视野为零，是角色换装/转场生硬/镜头语言重复三个真实缺陷的结构性根因
#: （见 2.0.3 changelog）。本次改回逐段调用不是简单回滚，三个缺陷各有新应对：
#: ① 角色换装——不再依赖模型在一次大调用里"记住"前面写过什么，relevant_
#: assets 的 appearance/scene_canonical 锚点每次独立调用都逐字重给，模型
#: 每次从同一份世界书原文抄、不凭记忆复述，比"整集一次看见全部段落"更不
#: 容易漂移。残余风险：functional_extras（无标准外观的群演）只能靠②的
#: "上一段"部分兜底，非相邻段仍可能漂移，是已知限制。
#: ② 转场生硬——每次调用把上一段（若存在）定稿的 prompt_text 全文给模型，
#: 由它判断承接空间/时间还是需要转场；第一段如实说明没有上一段可参考。
#: ③ 镜头语言重复——只给上一段发现不了"第 1、3、5 段用同一机位"这种隔段
#: 重复，模型每次自报 camera_digest（开场景别/运镜/转场类型）拼成"最近几段"
#: 清单，只服务于生成期参考，不落进 StoryboardPackSegment/shots 表。窗口取
#: 4 段：任务范围 3~5，本仓无历史观测能精确回答，4 是范围中点且与
#: MAX_SHOTS_PER_SEGMENT=4 同量级，是有理由但未经验证的判断。真需要重复
#: 机位（正反打/连续对切）时 camera_repetition_rationale 给合法出路。
#:
#: 代价：一集内调用变回严格串行，长尾命中机会从"一次整批 1~2 次"变成"每段
#: 各一次、共 N 次"；换来单次调用输入/输出体量大幅缩小，读超时从 960s 收紧
#: 到独立的 TIMEOUT_CHAT_STORYBOARD_PACK_SEGMENT_READ，卡住更快暴露/重试，
#: 不必再空等 16 分钟。批量算术（_segment_prompt_batch_capacity 等）随批量
#: 一并退场；答案预算安全网不退场（2.0.6 教训：装不下会撞 finish_reason=
#: length、整段失败且照常计费），改成 _ensure_segment_prompt_budget 每次
#: 单段请求前核验。持久化契约不变，MARKER 仍钉 2.0.5（同 2.0.6 先例）。
#:
#: 2.1.0（用户拍板，成片剧情跳跃/台词大量省略，实测 EP1 原文 488 字引号对白
#: 只存活 68 字）：根因是"模型看着办、丢了什么无人知道"——阶段一曾写"删掉
#: 内心独白里无法视觉化的部分"，阶段二方言指令曾写"挑一到两句、其余留给
#: 后面的段落"，但后面的段落从未接住。四项改造围绕"连贯性优先于时长紧凑"
#: （SEGMENT_DURATION_S=15 不动，段数不设上限）：
#: ① 对白台账（新模块 storyboard_dialogue_ledger）：原文引号台词确定性抽取
#: 为 dialogue_targets，阶段一必须逐句声明 kept_lines/dropped_lines 并写明
#: 理由，kept 台词按段分配、合计不超 15 秒容量，装不下是"拆更多段"不是
#: "丢台词"——段数从"节拍归组的副产品"升级为"台词预算的调节阀"。
#: ② 阶段二拿到 required_dialogue，_validate_segment_draft 新增 blocking
#: 检查（阈值复用 app.textmatch.KEY_LINE_PRESENT_RATIO，不新造口径）逼这些
#: 台词必须真的出现——这是对 2026-08-26"第一版不设内容门禁"的显式子集覆盖：
#: 用户以"质量最重要"重新拍板，只给"台词丢失"开一道有合法出路的门禁，不是
#: 恢复旧 F1-F6 全套闸门。
#: ③ 受控画外音：_AiDialogueLine 新增 delivery 字段（spoken_dialogue |
#: offscreen_voice，旧数据无此键按前者处理）；承载因果/动机/关键设定的内心
#: 独白改写成一句画外音（内容改编，不进引号台账），与①是两条数据路径。
#: ④ 镜头数下限 3→2：对话交锋段 2 镜正反打即可，不必凑镜头数挤占台词密度。
#: 落库形状变了（新增 required_dialogue 字段、dialogue[] 新增 delivery
#: 键），STORYBOARD_PACK_CONTRACT_MARKER 必须跟着改——目的正是让"已生成但
#: 台词大量丢失"的旧分集 resume 时判定为不算数、用新契约重新生成。方言约束
#: 两段指令文本同期拆到新模块 app.production.storyboard_dialects（纯搬移 +
#: 内容更新），为新增逻辑腾行数，不让本文件超过 2087 行 line_count 棘轮。
#:
#: 2.1.1（真实 EP1 回归 ERR-20260901-bcfa58/run_4e66c18f6713：容量闸门判得对，
#: 报错不可操作，三次语义重试耗尽仍未修出——完整归因与三处修复见
#: app.production.storyboard_dialogue_ledger 模块 docstring）。只改生成路径的
#: 报错文案与抽取期预拆逻辑，落库形状不变；marker 按 2.0.6 先例仍钉在
#: storyboard_pack/2.1.0，不随这次修复移动。
#:
#: 2.1.2（真实 EP1 二次回归 ERR-20260901-b1c349：2.1.1 报错仍未让模型修对，
#: 甚至出现两段互相指对方当挪动目标的"循环指路"——完整归因与实现见
#: app.production.storyboard_capacity_normalize 模块 docstring）。结论是容量
#: 维度（怎么重新分配/拆段）不该再打回模型语义重试，改成生成期确定性归一化；
#: 归一化只重排模型已确认保留的台词、不发明内容，synopsis 追加的可见标记是
#: 既有字段的内容变化（不是新字段/新类型），不构成落库形状变化，marker 仍钉
#: storyboard_pack/2.1.0。
#:
#: 2.2.0（专家审阅真实 EP1 十八段产出、用户拍板 2026-09-01：首尾段标记补丁 +
#: 色温弧线 + 独白压缩与闪回三项结构层改造，完整决策链与真实案例见
#: app.production.storyboard_narrative_arc 模块 docstring，不在这里重复）。
#: 落库新增 StoryboardPackSegment.palette 字段（阶段一模型自报的色温/色调
#: 方向），MARKER 必须跟着换版本号——旧行没有这个字段，resume 短路如果继续
#: 认旧 marker，会把这些缺字段的旧行误判为"已经用新契约生成过"而不重新生成。
#:
#: 2.3.0（用户拍板 2026-09-02，真实回归「人物白天说着说着就变成黑夜了」驱动）：
#: 新增跨段连贯性备忘 continuity_memo（本段结束时的时段与人物状态，下一段
#: 续接时默认逐字沿用，只有本段原文明确写出时间推移才允许改变并要求引用原文
#: 原话）——完整判据、正面提示词文案、阻断式校验见新模块
#: app.production.storyboard_continuity_memo 的 docstring，不在这里重复。
#: 落库新增 StoryboardPackSegment.continuity_memo 字段，MARKER 必须跟着换
#: 版本号：旧行没有这个字段，resume 短路如果继续认旧 marker，会把这些缺字段
#: 的旧行误判为"已经用新契约生成过"而不重新生成，跳过本次真正要修的时段漂移。
#:
#: 2.4.0（真实故障，2026-09-03「橘座在上」EP1，B 库实测）：节拍表把原文场
#: 1-3（761 字、无空行的一个原文段）拆成 6 段，但 6 段的 source_segment_
#: indexes 全是 [4]，每段拿到**整场**原文，只靠一句节拍摘要区分该拍哪一块——
#: 猫跳上桌在段 4/6 各拍一次，黄总抓猫在段 7/8/9 拍了三次，全集最后一句内心
#: 独白在段 4/6 各说一遍。旧校验只查段号越界，不查各段是否按顺序各占一块，
#: 因为节拍表压根没有"块"这个概念可查。三项改造对应三个根因：
#: ① 每段各占一块原文、按顺序不回退——节拍表新增 segments[].source_unit_
#: ranges（对每个非 paratext 原文段号声明一段句单元范围，句单元由新模块
#: app.production.storyboard_segment_ranges.split_source_units 确定性切出，
#: 不依赖模型），阻断式判据 segment_unit_range_errors 要求同一原文段被多段
#: 引用时范围首尾相接、不回退、并集覆盖全部单元；kept_line_unit_binding_
#: errors 进一步核对每条 kept 台词的单元号是否落在它被分到的那一段声明的
#: 范围内。阶段二只喂 segment_source_payload 算出的本段范围内单元 + 前后各
#: 两个单元的衔接上下文，不再是整段原文——这是同一原文段被拆给多段后，各段
#: 物理上看不到别的段负责的那几句、因此不可能拍重复的根本原因。
#: ② 时长维度：评估后用户拍板保留固定 15 秒/段（不引入 5/10/15 分档），改成
#: 节拍表 rules[] 里一条正面陈述——一个段必须承载足够撑满 15 秒的内容，内容
#: 单薄的相邻节拍要合并，不要为了凑段数把一场戏拆薄；只有台词容量装不下时
#: 才拆多段。判据来源仍是既有 config.MAX_SPOKEN_CHARS_PER_SHOT 与
#: dialogue_ledger_errors/容量归一化，不新增维度。
#: ③ 跨段台词去重——新模块 app.production.storyboard_dialogue_repeat：阶段二
#: 逐段调用互相看不到彼此写过什么台词，第 k 段生成时喂一份前 k-1 段已交付
#: 台词清单（already_delivered_dialogue），同一说话人重复交付同一句台词
#: （归一化后逐字相同）即阻断重试，判据 repeated_delivery_errors。
#: 搬移：storyboard_pack.py 已在 line_count 棘轮基线上零余量，阶段一（节拍表
#: schema/生成/校验/paratext 账处理）整体搬到新模块 app.production.
#: storyboard_beat_sheet（纯搬移，行为不变，见该模块 docstring）为上述三项
#: 新逻辑腾行数。落库新增 StoryboardPackSegment.source_unit_ranges 字段，
#: MARKER 必须跟着换版本号：旧行没有这个字段，resume 短路如果继续认旧
#: marker，会把这些缺字段的旧行误判为"已经用新契约生成过"而不重新生成，跳过
#: 本次真正要修的段落重复。
STORYBOARD_PACK_VERSION = "2.4.0"

#: Written to Shot.prompt_contract_version for every row this module writes;
#: downstream consumers key off it to know this row's legacy per-shot fields
#: are not authoritative. 2.1.0 起改到 storyboard_pack/2.1.0（见上方
#: changelog）：目的是让台词大量丢失的旧分集 resume 时判定为不算数、重新
#: 生成，marker 不动会让 resume 短路继续复用那些低保真产物。2.2.0 起改到
#: storyboard_pack/2.2.0（同上方 changelog）：新增 palette 落库字段，旧行
#: 没有这个字段，resume 短路必须判过期重生成。2.3.0 起改到
#: storyboard_pack/2.3.0：新增 continuity_memo 落库字段，同一理由。2.4.0 起
#: 改到 storyboard_pack/2.4.0：新增 source_unit_ranges 落库字段，同一理由。
STORYBOARD_PACK_CONTRACT_MARKER = "storyboard_pack/2.4.0"

SEGMENT_DURATION_S = 15
MIN_SHOTS_PER_SEGMENT = 2
MAX_SHOTS_PER_SEGMENT = 4

#: 阶段二每次调用只写一段（2.0.8 起固定如此，见 STORYBOARD_PACK_VERSION
#: changelog），"最近几段" 续接窗口给几段——用户方案原文只要求"参考上一镜"
#: （窗口=1），协调方评估后认为专治"镜头语言重复"这个原始缺陷（三个原始
#: 缺陷之一）需要比 1 更宽的窗口：只看上一段发现不了"第 1、3、5 段用同一
#: 机位"这种隔段重复。取 4：任务给的范围是 3~5 段，本仓库没有一份能回答
#: "隔几段观众才会感知到镜头语言重复"的历史观测，4 是范围中点，也与本模块
#: MIN_SHOTS_PER_SEGMENT/MAX_SHOTS_PER_SEGMENT 同一量级——是有理由但未经真实
#: 回归验证的判断，需要标注清楚，不能假装是精确推导（2.1.0：MIN 由 3 降到
#: 2，仍与窗口取值同一数量级，结论不受影响）。
CAMERA_DIGEST_WINDOW = 4

#: 单段调用的 answer 预算（业务只按「答案要多大」算，harness 自己叠加
#: reasoning 预留并夹到模型硬顶——见 app.hiagent.text_request_token_limits）。
#: 2400 取自真实历史单段调用 completion_tokens 观测（202 次
#: storyboard_pack_segment 调用，均值 1018、最大 1897，2.0.3 引入整集批量前
#: 记录的数据），乘约 1.3 的安全系数覆盖新设计里因续接/防重复陈述而略微
#: 变长的段落。2.0.8：批量退场后这个数字不再乘段数——固定一次只写一段，
#: 不再需要 SEGMENT_BATCH_TOKENS_FLOOR 那样为"批量太小也要留余量"设的下限
#: （批量机制本身已随 2.0.8 退场，见 changelog）。
SEGMENT_PROMPT_ANSWER_TOKENS = 2400


class StoryboardPackBudgetError(RuntimeError):
    """当前文本模型的输出预算装不下阶段二这一段，发请求前就拦下。

    拦是为了不白烧钱（实测那两次撞满的调用，供应商侧确实计了费），但拦住人
    就必须给出路，所以消息里要说清三件事：谁不够、差多少、去动哪个旋钮。
    最常见的成因不是模型真的弱，而是它的 ``max_output_tokens`` 还是系统在
    探测不到能力时写下的兜底默认值（``token_limits_source=default_128k_32k``，
    见 app/model_capabilities.py）——glm-5.3-flash 真实上限 131072，被兜底值
    按 32768 用了。这种情况下把能力填对就能跑，所以要专门点出来，不能让人
    以为只能换模型。
    """

    def __init__(
        self,
        *,
        model: str,
        model_cap: int,
        reserve: int,
        needed: int,
        provider: str | None,
    ) -> None:
        from app.db import get_setting
        from app.model_capabilities import active_model_token_limits

        source = ""
        try:
            source = str(active_model_token_limits(provider or "", model, get_setting).get("token_limits_source") or "")
        except Exception:  # noqa: BLE001 取不到来源不该盖掉真正的预算错误
            source = ""
        remedy = (
            "该模型的输出上限当前是系统探测不到能力时的兜底默认值，多半低于它的"
            "真实能力，请到「系统设置 - 模型」里把这个模型的最大输出 token 填成"
            "供应商文档给的真实值；"
            if source == "default_128k_32k"
            else "请为分镜台改配一个输出预算更大、或思考开销更小的文本模型；"
        )
        super().__init__(
            f"文本模型 {model} 的输出预算装不下分镜台阶段二这一段："
            f"输出上限 {model_cap} tokens，其中该模型实测思考要占 {reserve} tokens，"
            f"留给答案只剩 {model_cap - reserve} tokens，而写一段至少需要 {needed} tokens。"
            f"{remedy}"
            "继续发出去只会撞 finish_reason=length 截断，这一段失败且照常计费，因此在发请求前停住。"
        )
        self.model = model
        self.model_cap = model_cap
        self.reserve = reserve
        self.needed = needed
        self.token_limits_source = source


def _ensure_segment_prompt_budget() -> None:
    """发单段请求前核验：这一段的答案预算是否装得进「模型硬顶 - 思考预留」。

    2.0.8：批量退场后不再有「一次调用装几段」的算术问题（固定一次只写一段），
    但 2.0.6 记录的生产事故——答案预算装不下会撞 ``finish_reason=length``、
    该次调用失败且照常计费——与批不批无关，这道安全网本身不能跟着批量机制
    一起退场，只是从「算容量」简化成「算这一段够不够」。

    判据挂在这次调用自己的 token 算术上，不挂经验值：思考预留和模型上限都
    来自网关同一换算入口（``app.hiagent.text_request_token_limits`` /
    ``reasoning_token_reserve``），这里不另写一份常量。必须按**本环节实际
    要打给谁**来算——分镜台可以在项目上配专属文本模型
    （``projects.board_text_provider``，经 text_provider_scope 生效），不传
    provider 拿到的会是全局默认文本模型的上限，两边 ``max_output_tokens``
    可能不同（2.0.7 记录过的同一个坑）。
    """
    from app.harness.text_provider_scope import current_stage_text_provider

    provider = current_stage_text_provider()
    _, selected_model, model_cap = hiagent.text_request_token_limits(
        requested_max_tokens=10**9, provider=provider,
    )
    reserve = hiagent.reasoning_token_reserve(model=selected_model)
    room = int(model_cap) - reserve
    if room < SEGMENT_PROMPT_ANSWER_TOKENS:
        raise StoryboardPackBudgetError(
            model=selected_model,
            model_cap=int(model_cap),
            reserve=reserve,
            needed=SEGMENT_PROMPT_ANSWER_TOKENS,
            provider=provider,
        )


# ---------------------------------------------------------------------------
# 方言约束（阶段二模型使用）2.1.0 起拆到 app.production.storyboard_dialects
# （import 见文件顶部，原名字在那里重新导出，测试与其余调用方 import 路径
# 不变）：两段指令文本块 + 方言解析函数不依赖本文件任何私有状态，纯搬移 +
# 方言文案内容更新，为对白台账/必保台词/受控画外音等新逻辑腾行数。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 阶段一（节拍表 + 分段的 schema/生成/校验/paratext 账处理）2.4.0 起搬到
# app.production.storyboard_beat_sheet（纯搬移，行为不变，见该模块 docstring
# 与文件顶部 import：_AiBeat/_AiSegmentPlan/_AiBeatSheetDraft/
# _validate_beat_sheet_draft/_beat_sheet_rules/_generate_beat_sheet/
# _paratext_segment_indexes/_paratext_exclusion_rule/_source_block_for_prompt/
# _PARATEXT_PLACEHOLDER_TEXT 均从那边以 `import X as X` 再导出，测试与其余
# 调用方 import 路径不变）——为句单元范围声明与跨段台词去重两项新增闸门腾
# 出行数，storyboard_pack.py 已在 line_count 棘轮基线上零余量。
# ---------------------------------------------------------------------------


def _strip_paratext_from_beat_draft(
    draft: _AiBeatSheetDraft, paratext_indexes: set[int]
) -> list[str]:
    """防御性兜底：stage 1 的 rules[] 只是提示词层面的禁令，不是校验闸门，
    模型仍可能无视提示把 paratext 段号写进某个引用。这里在 beat_draft 通过
    格式校验之后、进入 phase 2 之前，用同一份确定性 paratext_indexes 把这类
    引用滤掉——纯列表运算，不是新增一道模型语义判断，也不是黑名单（判据仍
    然全部来自映射台账本身）。

    唯一的例外：过滤后如果会让某个 beat/segment 的引用列表变空，就保留原始
    列表不做过滤——宁可留一个不该在的 paratext 段号，也不留一个没有任何
    原文依据的空引用（后者会让 _resolve_segment_source_binding 直接抛错，
    整集生成失败）。返回值是发生了这种"保留未过滤"情况的说明列表，供调用方
    留痕（不阻断生成）。
    """
    if not paratext_indexes:
        return []
    notes: list[str] = []
    for beat in draft.beat_sheet:
        filtered = [i for i in beat.segment_indexes if i not in paratext_indexes]
        if filtered:
            beat.segment_indexes = filtered
        elif set(beat.segment_indexes) & paratext_indexes:
            notes.append(
                f"beat {beat.beat_id} 的 segment_indexes 全部落在 paratext 账内"
                f"（{beat.segment_indexes}），已保留未过滤，避免产出空引用"
            )
    for seg in draft.segments:
        filtered = [i for i in seg.source_segment_indexes if i not in paratext_indexes]
        if filtered:
            seg.source_segment_indexes = filtered
        elif set(seg.source_segment_indexes) & paratext_indexes:
            notes.append(
                f"段 {seg.segment_no} 的 source_segment_indexes 全部落在 paratext 账内"
                f"（{seg.source_segment_indexes}），已保留未过滤，避免产出空引用"
            )
        # 2.4.0：source_unit_ranges 同步清掉指向 paratext 段号的条目——paratext
        # 段落没有真实原文可切单元，range 声明本就不该存在；不受上面"保留未过滤"
        # 兜底的影响（那条只保护 source_segment_indexes 不变空引用，range 本身
        # 对 paratext 段号永远非法）。
        seg.source_unit_ranges = [
            r for r in seg.source_unit_ranges if r.source_segment_index not in paratext_indexes
        ]
    return notes


# ---------------------------------------------------------------------------
# 世界书标准外观/场景锚点接入（问题一修复，真实 EP1 回归：孟浩换了三套
# 衣服——asset_manifest 只写身份字段，模型只能现推外观；世界书的标准外观/
# 场景锚点一直都在，只是没被送给模型）。
# ---------------------------------------------------------------------------

_NO_CANONICAL_APPEARANCE_NOTE = (
    "素材库没有为这个角色建立标准外观定妆照（群演/一次性人物，没有定妆照）："
    "由你在本集第一次出现这个角色时自行确定其外观特征（年龄体型、发型头饰、"
    "服装颜色材质、随身物等可视信息），并在本集所有涉及这个角色的段落里原样"
    "沿用同一套自定特征，不得每段重新编写。"
)

_NO_CANONICAL_SCENE_NOTE = (
    "素材库没有为这个场景建立标准场景描述：由你在本集第一次出现这个场景时"
    "自行确定其可视特征（空间格局、主要陈设、光线氛围等），并在本集所有涉及"
    "这个场景的段落里原样沿用同一套自定特征，不得每段重新编写。"
)


def _character_canonical_appearance(
    conn, portrait_id: str | None, *, bible_appearance: str | None = None,
) -> str | None:
    """这个已解析 portrait_id 对应的世界书标准外观锚点串；查不到（含出图已
    解耦到后台、portrait_id 本就为空）时回退 ``bible_appearance``——世界书
    ``Character.appearance_canonical`` 本来就是外观权威，不是 character_
    portraits 行的附属产物。
    """
    if portrait_id:
        row = conn.execute(
            "SELECT appearance FROM character_portraits WHERE id=?", (portrait_id,),
        ).fetchone()
        if row is not None:
            appearance = str(row["appearance"] or "").strip()
            if appearance:
                return appearance
    return bible_appearance


def _scene_canonical_description(
    conn, scene_reference_id: str | None, *, bible_scene_canonical: str | None = None,
) -> str | None:
    """场景侧同构（见 ``_character_canonical_appearance``）：查不到时回退
    ``bible_scene_canonical``（世界书 ``Scene.scene_canonical``）。
    """
    if scene_reference_id:
        row = conn.execute(
            "SELECT scene_canonical FROM scene_references WHERE id=?", (scene_reference_id,),
        ).fetchone()
        if row is not None:
            canonical = str(row["scene_canonical"] or "").strip()
            if canonical:
                return canonical
    return bible_scene_canonical


def _enrich_asset_manifest_canonical_visuals(
    conn, payload: dict[str, Any], *, bible: Bible | None = None,
) -> None:
    """原地把世界书标准外观/场景锚点补进 ``payload["asset_manifest"]``。

    在 ``_generate_beat_sheet``/``_generate_all_segment_prompts`` 之前调用一次。
    ``bible`` 非空时兜底取世界书原始锚点——出图已解耦到后台，卡在人物谱/场景库
    但还没出图的资产查不到 character_portraits/scene_references 行，不该被读成
    "没有任何外观信息"。``functional_extras``（群演）没有 portrait_id，天生没有
    标准外观，这里显式写一条说明而不是留空，避免被模型读成"无信息"而各段各编。
    """
    bible_appearance = {c.name: c.appearance_canonical for c in (bible.characters if bible else [])}
    bible_scenes = {s.name: s.scene_canonical for s in (bible.scenes if bible else [])}
    manifest = payload.get("asset_manifest") or {}
    for character in manifest.get("characters") or []:
        character["appearance"] = _character_canonical_appearance(
            conn, character.get("portrait_id"),
            bible_appearance=bible_appearance.get(str(character.get("display_name") or "")),
        ) or _NO_CANONICAL_APPEARANCE_NOTE
    for extra in manifest.get("functional_extras") or []:
        extra["appearance"] = _NO_CANONICAL_APPEARANCE_NOTE
    for scene in manifest.get("scenes") or []:
        scene["scene_canonical"] = _scene_canonical_description(
            conn, scene.get("scene_reference_id"),
            bible_scene_canonical=bible_scenes.get(str(scene.get("display_name") or "")),
        ) or _NO_CANONICAL_SCENE_NOTE


# ---------------------------------------------------------------------------
# 阶段二：逐段提示词
# ---------------------------------------------------------------------------

class _AiDialogueLine(BaseModel):
    speaker_identity_id: str
    line: str
    source_segment_index: int
    #: 2.1.0 受控画外音：默认画内说话；因果/动机/关键设定的旁白性交代填
    #: offscreen_voice。旧数据行没有这个键时按 spoken_dialogue 处理。
    delivery: Literal["spoken_dialogue", "offscreen_voice"] = "spoken_dialogue"


class _AiResourceCharacter(BaseModel):
    identity_id: str
    portrait_id: str | None = None
    description: str = ""


class _AiResourceScene(BaseModel):
    scene_id: str
    scene_reference_id: str | None = None
    description: str = ""


class _AiResourceProp(BaseModel):
    label: str
    description: str = ""


class _AiSegmentResources(BaseModel):
    characters: list[_AiResourceCharacter] = Field(default_factory=list)
    scenes: list[_AiResourceScene] = Field(default_factory=list)
    props: list[_AiResourceProp] = Field(default_factory=list)


class _AiCameraDigest(BaseModel):
    """本段的开场镜头语言，供之后几段的「最近镜头语言清单」使用。

    2.0.8 新增，只在生成期跨调用传递（见 ``_generate_all_segment_prompts``
    的 ``camera_digest_by_segment_no``），不写入 ``StoryboardPackSegment``/
    ``shots`` 表——分镜产出的持久化形状不变，这个字段只服务于「怎么生成」。
    三个字段都允许空串（``str = ""`` 而不是 ``Field(min_length=1)``）：本集
    第一段没有「与上一段的转场」可言，留空是唯一诚实的写法，不强行编一个。
    """

    opening_shot_size: str = ""
    opening_camera_move: str = ""
    transition_from_previous: str = ""


class _AiStoryboardSegmentDraft(BaseModel):
    prompt_text: str = Field(min_length=1)
    shot_count: int = Field(ge=MIN_SHOTS_PER_SEGMENT, le=MAX_SHOTS_PER_SEGMENT)
    dialogue: list[_AiDialogueLine] = Field(default_factory=list)
    resources: _AiSegmentResources = Field(default_factory=_AiSegmentResources)
    degraded_capabilities: list[str] = Field(default_factory=list)
    #: 2.0.8：只在本段开场确实沿用了 recent_camera_language 清单里出现过的
    #: 机位时才需要非空——专治「镜头语言重复」缺陷时给的合法出路：允许重复，
    #: 但要说明为什么这次重复是必要的（例如同一场戏的正反打）。没有重复就
    #: 留空，不强行找理由（对应「不得兜底填充」）。
    camera_repetition_rationale: str = ""
    camera_digest: _AiCameraDigest = Field(default_factory=_AiCameraDigest)
    #: 2.3.0 跨段连贯性备忘（真实回归「白天说着说着变成黑夜」驱动），只在生成期
    #: 跨调用传递 + 落库审计，与 camera_digest 同一先例——见
    #: app.production.storyboard_continuity_memo 模块 docstring。
    continuity_memo: _AiContinuityMemo = Field(default_factory=_AiContinuityMemo)
    #: WS7 蒙太奇形态（app.schemas.shot_montage）：判据文本已在 dialect_instructions
    #: 排比/列举段落一节给过，这里不重复写第二份。
    form: str = "scene"
    beats: list[MontageBeat] = Field(default_factory=list)


def _segment_relevant_assets(
    payload: dict[str, Any], source_segment_indexes: list[int]
) -> dict[str, Any]:
    wanted = set(source_segment_indexes)
    manifest = payload.get("asset_manifest") or {}

    def _hits(entry: dict[str, Any]) -> bool:
        return bool(wanted & set(entry.get("segment_indexes") or []))

    characters = [c for c in (manifest.get("characters") or []) if _hits(c)]
    scenes = [s for s in (manifest.get("scenes") or []) if _hits(s)]
    props = [p for p in (manifest.get("props") or []) if _hits(p)]
    functional_extras = [f for f in (manifest.get("functional_extras") or []) if _hits(f)]
    appellations = [
        a for a in (payload.get("appellation_map") or [])
        if int(a.get("segment_index") or -1) in wanted
    ]
    return {
        "characters": characters,
        "scenes": scenes,
        "props": props,
        "functional_extras": functional_extras,
        "appellation_map": appellations,
    }


def _validate_segment_draft(
    draft: _AiStoryboardSegmentDraft,
    *,
    dialect_render_format: str,
    required_dialogue: list[dict[str, Any]],
    delivered_lines: list[tuple[int, str, str]],
    current_segment_no: int,
    previous_memo: _AiContinuityMemo | None = None,
    segment_source_text: str = "",
) -> list[str]:
    """Blocking checks -- the only things that can make
    ``model_gateway.chat_structured`` retry or fail this segment.

    2026-08-26（用户拍板，第一版不设内容门禁）：说话人在场/资源可溯源这类
    判断不许出现在这里，移到 ``_segment_content_advisories`` 只算不拦截。
    2.1.0（用户以「质量最重要」重新拍板）显式覆盖这条决策的一个具体子集——
    required_dialogue 是否真的被说出口（容量已在上一阶段分配好，没有"装不下"
    的借口），不是恢复旧 F1-F6 全套内容闸门。其余仍是"下一环节用不了"的形状
    问题：prompt_text 空/超限、H3 固定字段名是否存在。

    2.3.0：新增 ``continuity_memo_errors`` 阻断检查（真实回归「白天说着说着
    变成黑夜」驱动，完整判据见 app.production.storyboard_continuity_memo 模块
    docstring）。``previous_memo``/``segment_source_text`` 两个参数带默认值
    只是为了不必逐一改写本文件里既有的、与连贯性备忘无关的直接调用点
    （它们测的是 prompt_text/H3 字段/required_dialogue 这几件事，默认值下等
    价于「本集第一段、原文为空」，_draft() 测试 fixture 已配一份合法备忘让
    这些用例继续通过）；``_generate_all_segment_prompts`` 唯一的生产调用点
    两个参数都显式传递，不依赖默认值。

    2.4.0：新增 ``delivered_lines``/``current_segment_no`` 两个必传参数（无
    默认值——CLAUDE.md「可选参数是缺陷的温床」：跨段去重必须每次显式传入
    截至当前的已交付台词，漏传会让重复检查悄悄失效而不是报错），接
    ``app.production.storyboard_dialogue_repeat.repeated_delivery_errors``。
    真实故障：猫跳上桌在段 4/6 各拍一次，黄总抓猫在段 7/8/9 拍了三次——阶段二
    逐段调用互相看不到彼此写过的台词，没有任何信号能让模型知道"这句已经说过
    了"。
    """
    errors: list[str] = []
    if not draft.prompt_text.strip():
        errors.append("prompt_text 为空")
    elif len(draft.prompt_text) > config.PROMPT_CHAR_LIMIT:
        errors.append(
            f"prompt_text 长度 {len(draft.prompt_text)} 超过上限 {config.PROMPT_CHAR_LIMIT}"
        )
    if dialect_render_format == "minimax_h3_native_fields":
        for field in ("integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:"):
            if field not in draft.prompt_text:
                errors.append(f"prompt_text 缺少 H3 固定字段「{field}」")
    errors.extend(required_dialogue_missing_errors(
        required_dialogue, [line.line for line in draft.dialogue],
    ))
    errors.extend(continuity_memo_errors(draft.continuity_memo, previous_memo, segment_source_text))
    errors.extend(repeated_delivery_errors(
        delivered_lines,
        [(line.speaker_identity_id, line.line) for line in draft.dialogue],
        current_segment_no=current_segment_no,
    ))
    return errors


def _speaker_absent_advisory(
    index: int, line: _AiDialogueLine, segment_character_ids: set[str],
) -> str | None:
    """delivery 感知的说话人在场提示文案：画外音不要求在场，改说「未列入」。"""
    if line.speaker_identity_id in segment_character_ids:
        return None
    if line.delivery == "offscreen_voice":
        return (
            f"[STORYBOARD_PACK_DIALOGUE_SPEAKER_ABSENT][未拦截] dialogue[{index}] "
            f"是画外音，说话人「{line.speaker_identity_id}」未列入本段 "
            "resources.characters（画外发声也需要注明归属角色）"
        )
    return (
        f"[STORYBOARD_PACK_DIALOGUE_SPEAKER_ABSENT][未拦截] dialogue[{index}] "
        f"的说话人「{line.speaker_identity_id}」不在本段 resources.characters 内，"
        "没有在场证据"
    )


def _segment_content_advisories(
    draft: _AiStoryboardSegmentDraft,
    *,
    source_segment_indexes: list[int],
    manifest: dict[str, Any] | None,
    segment_relevant_scene_ids: set[str] = frozenset(),
) -> list[str]:
    """Non-blocking content checks: computed every time, never gate generation.

    2026-08-26（用户拍板）：「我认为第一版的分镜提示词先不需要任何门禁……
    只要格式没问题就直接作用到下一环节」。这些判断此前是
    ``_validate_segment_draft`` 的一部分，会触发 chat_structured 的语义重试
    直到耗尽预算后整段失败；现在原样保留计算，只是结论从「拦截」改成
    「附在产物 degraded_capabilities[] 上的信息」——校验照算，不是删掉，
    删掉以后就永远看不到这条不一致了。台词说话人是否在场的第三条（rule 1）
    对照的是这一段自己的 resources.characters（映射台对"这个人物在这些
    段落里在场"的结论），不是全集已知人物表，与
    app.validators.storyboard_pack_dialogue_errors 用的是同一套判据，只是
    这里在生成时就先算一遍、写进产物，那边在确认时再算一遍、当作可见但
    不拦截的 warning——两处判据不重复发明，只是消费方式不同。
    """
    # Tag names deliberately match app.validators.storyboard_pack_dialogue_errors'
    # [STORYBOARD_PACK_DIALOGUE_*] codes -- same underlying judgment computed
    # at two points in the pipeline (here at generation time, there again at
    # confirmation time against the persisted row), so a search for one code
    # finds both occurrences instead of two unrelated-looking strings.
    advisories: list[str] = []
    # 15 秒能说完多少字是物理量，不是偏好。config.MAX_SPOKEN_CHARS_PER_SHOT
    # 是全仓库唯一口径（大纲分组 app/narrative_outline.py、话轮切分
    # app/renderability.py 都读它），这里用 spoken_contract.content_char_count
    # 数字数，与 app/spoken_contract.py 的口播统计同口径，不另起一套算法。
    # 实测（EP1，改提示词之前）：段 6 写了 172 字、段 11 写了 175 字，都是上限
    # 的三倍多。超出的部分模型只能抢读或整句吞掉，而吞哪句不可预测。
    spoken_chars = sum(spoken_contract.content_char_count(line.line) for line in draft.dialogue)
    if spoken_chars > config.MAX_SPOKEN_CHARS_PER_SHOT:
        advisories.append(
            f"[STORYBOARD_PACK_DIALOGUE_OVER_CAPACITY][未拦截] 本段台词共 "
            f"{spoken_chars} 字，超过 15 秒的口播容量 "
            f"{config.MAX_SPOKEN_CHARS_PER_SHOT} 字"
            f"（约 {config.SPOKEN_CHARS_PER_5_SECONDS / 5:.1f} 字/秒），"
            "视频模型会抢读或漏读其中一部分"
        )
    allowed_segments = set(source_segment_indexes)
    segment_character_ids = {c.identity_id for c in draft.resources.characters}
    for index, line in enumerate(draft.dialogue):
        absent_advisory = _speaker_absent_advisory(index, line, segment_character_ids)
        if absent_advisory is not None:
            advisories.append(absent_advisory)
        if line.source_segment_index not in allowed_segments:
            advisories.append(
                f"[STORYBOARD_PACK_DIALOGUE_NO_SOURCE][未拦截] dialogue[{index}]"
                f".source_segment_index={line.source_segment_index} "
                f"不在本段引用的原文段号 {sorted(allowed_segments)} 内"
            )
    # WS8 接线：人物/场景资源判据统一用 app.validators.resource_advisories_
    # for_segment（与生成台同判据，[拦截]/[未拦截] 一致）。manifest 是调用方
    # 必传的显式决定——manifest=None（bible 缺失/解析不到）时该判据本身按
    # 最严处理（见 app.multiview.manifest_production_blockers 对非 dict
    # manifest 的"依赖 manifest 缺失"分支），本函数不再另开一条回退文案。
    advisories.extend(resource_advisories_for_segment(
        resources=draft.resources.model_dump(mode="json"), manifest=manifest,
    ))
    # 场景资源漏填 vs 本来就没有可引用场景（2.0.5，真实 EP4 回归）：两种情况
    # 必须分开报告——relevant_assets.scenes 非空却仍留空，是"可能漏填"，需要
    # 人核对；本身为空由上面 resource_advisories_for_segment 的 MANIFEST_GAP
    # 兜底覆盖（其内部 `not resources.get("scenes")` 判据同源，不重复实现）。
    if not draft.resources.scenes and segment_relevant_scene_ids:
        advisories.append(
            "[STORYBOARD_PACK_RESOURCE_SCENE_MISSING][未拦截] 本段 "
            f"relevant_assets.scenes 提供了可引用场景"
            f"（{sorted(segment_relevant_scene_ids)}），但 resources.scenes 为空："
            "可能是本段确实没有独立场景（例如纯特写/纯对话），也可能是遗漏，"
            "需要人工核对"
        )
    # 2.3.0：continuity_memo.characters 只做 advisory，不阻断——见
    # app.production.storyboard_continuity_memo.continuity_memo_character_advisories。
    advisories.extend(
        continuity_memo_character_advisories(draft.continuity_memo, segment_character_ids)
    )
    return advisories


def _segment_source_block(
    segments: list[SourceSegment],
    source_segment_indexes: list[int],
    paratext_indexes: set[int],
) -> str:
    """本段自己的原文切片（[段N] 原文/占位说明），不是全集原文。

    2.0.8：逐段调用只需要这一段自己的原文——这一段的节拍/素材范围已经由
    ``source_segment_indexes`` 锁定，全集原文对本段来说是白白抬高单次调用
    输入体量的多余上下文（承袭 2.0.3 之前逐段调用的原始设计）。段号占位
    规则与 ``_source_block_for_prompt`` 完全一致（同一份
    ``_PARATEXT_PLACEHOLDER_TEXT``），只是只输出这一段自己引用的段号。
    """
    lines = []
    for index in source_segment_indexes:
        if not (1 <= index <= len(segments)):
            continue
        text = (
            _PARATEXT_PLACEHOLDER_TEXT
            if index in paratext_indexes
            else segments[index - 1].text
        )
        lines.append(f"[段{index}] {text}")
    return "\n".join(lines)


def _camera_digest_window_payload(
    camera_digest_by_segment_no: dict[int, _AiCameraDigest],
    *,
    segment_no: int,
    window: int,
) -> list[dict[str, Any]]:
    """最近 ``window`` 段（不含本段）的开场镜头语言，按 segment_no 升序。

    专治「镜头语言重复」——只看上一段（窗口=1）发现不了「第 1、3、5 段用
    同一机位」这种隔段重复，窗口大小的推导见 CAMERA_DIGEST_WINDOW 的注释。
    本集前几段不足 window 段时，只给实际存在的那些，不补空占位——空列表
    本身就是诚实的「还没有可参考的历史」，不是需要兜底填充的缺口。
    """
    start = max(1, segment_no - window)
    return [
        {
            "segment_no": no,
            "opening_shot_size": camera_digest_by_segment_no[no].opening_shot_size,
            "opening_camera_move": camera_digest_by_segment_no[no].opening_camera_move,
        }
        for no in range(start, segment_no)
        if no in camera_digest_by_segment_no
    ]


# ``_segment_continuity_rules``/``_segment_shared_rules``/``phase2_segment_rules``
# 2.2.0/2.3.0 起搬到 app.production.storyboard_narrative_arc（纯函数、不依赖
# 本文件任何私有对象，为新增的结构层改造腾行数——见该模块 docstring）；本文件
# 通过上方 import 继续用同一个名字调用它们，行为不变。



async def _generate_all_segment_prompts(
    *,
    episode_id: str,
    episode_no: int,
    beat_draft: _AiBeatSheetDraft,
    segments: list[SourceSegment],
    payload: dict[str, Any],
    target_video_model: str,
    bible: Bible | None,
    required_dialogue_by_segment_no: dict[int, list[dict[str, Any]]],
    conn: Any,
    project_id: str,
) -> dict[int, _AiStoryboardSegmentDraft]:
    """逐段独立调用产出全部段落的 prompt_text（2.0.8 起，替代整集批量调用）。

    2.0.3 曾把这里改成整集一次调用，理由是逐段调用跨段视野为零，是角色
    换装、转场生硬、镜头语言重复三个真实缺陷的结构性根因；2.0.8 改回逐段
    调用不是简单回滚原样——三个缺陷各自换了新的应对方式，完整推导见
    STORYBOARD_PACK_VERSION 的 2.0.8 changelog：
    ① 角色换装——relevant_assets 的世界书 appearance/scene_canonical 锚点
       每次独立调用都逐字给一遍，不依赖模型的跨段记忆，比"整集一次看见
       全部段落、指望模型自己保持一致"更不容易漂移。
    ② 转场生硬——上一段（若存在）定稿的 prompt_text 全文原样给到本段，
       用户方案明确要求的"一镜参考"。
    ③ 镜头语言重复——最近 CAMERA_DIGEST_WINDOW 段的开场景别/运镜清单，
       只给上一段发现不了隔段重复。

    代价：一集内严格串行，第 N 段必须等第 N-1 段成功返回才能发出——见
    changelog 里的超时/串行代价评估。返回值按 segment_no 建索引，供调用方
    按 beat_draft.segments 的顺序取回，签名与返回形状均与批量版本一致。
    """
    profile, target_model_literal, dialect_instructions = _dialect_for_target_video_model(target_video_model)
    beats_by_id = {beat.beat_id: beat for beat in beat_draft.beat_sheet}
    paratext_indexes = _paratext_segment_indexes(payload)
    visual_style = bible.world.visual_style_canonical if bible is not None and bible.world is not None else ""
    shared_rules = _segment_shared_rules()

    by_segment_no: dict[int, _AiStoryboardSegmentDraft] = {}
    camera_digest_by_segment_no: dict[int, _AiCameraDigest] = {}
    relevant_assets_by_segment_no: dict[int, dict[str, Any]] = {}
    # 2.4.0：截至当前段已交付的 (segment_no, speaker_identity_id, line)，用于
    # 跨段台词去重（真实故障见 STORYBOARD_PACK_VERSION 2.4.0 changelog）。
    delivered_lines: list[tuple[int, str, str]] = []
    for plan in beat_draft.segments:
        _ensure_segment_prompt_budget()
        relevant_assets = _segment_relevant_assets(payload, plan.source_segment_indexes)
        relevant_assets_by_segment_no[plan.segment_no] = relevant_assets
        previous_draft = by_segment_no.get(plan.segment_no - 1)
        previous_segment_no = plan.segment_no - 1 if previous_draft is not None else None
        previous_memo = previous_draft.continuity_memo if previous_draft is not None else None
        camera_history = _camera_digest_window_payload(
            camera_digest_by_segment_no, segment_no=plan.segment_no, window=CAMERA_DIGEST_WINDOW,
        )
        continuity_rules = _segment_continuity_rules(
            previous_segment_no=previous_segment_no, camera_history=camera_history,
        )
        # 2.4.0：source_text_by_segment 只喂本段 source_unit_ranges 范围内的
        # 单元 + 前后各两个单元的衔接上下文，不再是整段原文（见
        # storyboard_segment_ranges 模块 docstring 的真实故障）。
        source_payload = segment_source_payload(plan, segments, paratext_indexes)
        segment_paratext_hit = set(plan.source_segment_indexes) & paratext_indexes
        required_dialogue = required_dialogue_by_segment_no.get(plan.segment_no, [])
        palette_previous = beat_draft.segments[plan.segment_no - 2].palette if plan.segment_no > 1 else ""
        task_payload: dict[str, Any] = {
            "task": (
                "为下面这一段原文和节拍写一整段可直接投喂视频生成模型的提示词（prompt_text）。"
                "prompt_text 必须是完整、可直接复制使用的一整块文本，不要拆成多个片段或只写关键词"
                "——你产出的字符串会被原样保存并原样提交给视频生成接口，不会再被代码拼接、改写或补充任何后缀。"
            ),
            "rules": [
                *phase2_segment_rules(
                    continuity_rules=continuity_rules, shared_rules=shared_rules,
                    required_dialogue_rule_text=required_dialogue_rule(required_dialogue),
                    paratext_exclusion_rule=(
                        _paratext_exclusion_rule(segment_paratext_hit) if segment_paratext_hit else None
                    ),
                    palette_current=plan.palette, palette_previous=palette_previous,
                    previous_memo=previous_memo,
                ),
                already_delivered_dialogue_rule(delivered_lines),
            ],
            **segment_narrative_arc_payload_fields(
                segment_no=plan.segment_no,
                total_segments=len(beat_draft.segments),
                palette_current=plan.palette,
                palette_previous=palette_previous,
            ),
            "segment_no": plan.segment_no,
            "synopsis": plan.synopsis,
            "beats": [
                {"beat_id": beat_id, "summary": beats_by_id[beat_id].summary}
                for beat_id in plan.beat_ids
                if beat_id in beats_by_id
            ],
            "duration_s": SEGMENT_DURATION_S,
            "shot_count_range": [MIN_SHOTS_PER_SEGMENT, MAX_SHOTS_PER_SEGMENT],
            "source_segment_indexes": plan.source_segment_indexes,
            **source_payload,
            "relevant_assets": relevant_assets,
            "required_dialogue": required_dialogue,
            "already_delivered_dialogue": already_delivered_payload(delivered_lines),
            "previous_segment_prompt": previous_draft.prompt_text if previous_draft is not None else None,
            "previous_continuity_memo": continuity_memo_payload(previous_memo),
            "recent_camera_language": camera_history,
            "visual_style": visual_style,
            "target_video_model": target_model_literal,
            "dialect_instructions": dialect_instructions,
            # app.video_prompt_profiles 的 SEEDANCE_2_PROFILE/MINIMAX_H3_PROFILE 是
            # 既有的正确接缝（docs/STORYBOARD_PROMPT_IR_DESIGN.md「与既有代码的衔接」），
            # 职责收窄为"交给模型的方言约束"；dialect_instructions 是本模块新写的
            # 详细版本（含 H3 字段名等接口语法），这里把 profile 自带的精简规则也一并
            # 带上作为强化重申，避免两处描述同一模型方言、其中一处不再被任何调用方
            # 读取而悄悄漂移。
            "profile_generation_rules": list(profile.generation_rules),
            "output_contract": {
                "prompt_text": "完整可复制的提示词整块文本，按上面的方言约束写",
                "shot_count": f"{MIN_SHOTS_PER_SEGMENT}-{MAX_SHOTS_PER_SEGMENT} 之间的整数，须与 prompt_text 里实际写的镜头数一致",
                "dialogue": (
                    "本段实际出现的台词；required_dialogue 里的每一条必须逐句出现（主干"
                    "逐字保留，允许衔接性微调），除此之外可以是原文其它对话的压缩/改写，"
                    "不要求逐字，但不得偏离本段剧情；每条必须给 speaker_identity_id（引用"
                    "relevant_assets.characters 的 identity_id）与 source_segment_index"
                    f"（这句话对应原文的哪一段，必须在 {plan.source_segment_indexes} 范围内）"
                ),
                "resources": "本段实际用到的人物/场景/道具，identity_id/scene_id 必须来自 relevant_assets；素材库没有对应图的（scene_reference_id 或 portrait_id 为空）如实留空，不得编造",
                "degraded_capabilities": "本段因模型能力缺失而做的降级处理清单（例如 Seedance 侧的屏上文字改「无字」+ 后期合成说明）；没有降级则留空数组，不得留空字符串占位",
                "camera_digest": "本段实际选用的开场景别（opening_shot_size）、开场运镜（opening_camera_move），以及本段与上一段之间的转场类型（transition_from_previous，本集第一段留空）；只用于给接下来几段做参考，不进入分镜产出契约",
                "camera_repetition_rationale": "只在本段开场确实沿用了 recent_camera_language 里出现过的机位时才写理由；没有重复就留空，不得编造理由",
                "continuity_memo": continuity_memo_output_contract_text(),
            },
            "output_schema": _AiStoryboardSegmentDraft.model_json_schema(),
        }
        fingerprint = hashlib.sha256(
            json.dumps(task_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        draft = await model_gateway.chat_structured(
            [
                {
                    "role": "system",
                    "content": (
                        "你是短剧分镜师和视频生成提示词撰写者。"
                        f"当前目标模型是 {profile.model_family}。"
                        "只输出符合 Schema 的一个 JSON 对象，不输出 Markdown 或解释；"
                        "prompt_text 字段内部可以换行，但整体是一个字符串。"
                    ),
                },
                {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False)},
            ],
            model_type=_AiStoryboardSegmentDraft,
            validate=lambda value, _req=required_dialogue, _pm=previous_memo,
            _st=source_payload["source_text_by_segment"], _dl=list(delivered_lines),
            _no=plan.segment_no: _validate_segment_draft(
                value, dialect_render_format=profile.render_format, required_dialogue=_req,
                previous_memo=_pm, segment_source_text=_st, delivered_lines=_dl, current_segment_no=_no,
            ),
            operation_id=f"storyboard_pack_segment_{episode_id}_{plan.segment_no}_{fingerprint}",
            max_tokens=SEGMENT_PROMPT_ANSWER_TOKENS,
            format_retry_limit=1,
            semantic_retry_limit=2,
            temperature=0.6,
            call_meta={
                "stage_key": "storyboard_pack_segment",
                "call_role": "storyboard_pack_segment_single",
                "initiator_label": "分镜台逐段提示词",
                "episode_id": episode_id,
                "segment_no": plan.segment_no,
                "segment_count": len(beat_draft.segments),
                "target_video_model": target_video_model,
                "contract_version": STORYBOARD_PACK_VERSION,
            },
            repair_context=f"第 {plan.segment_no} 段（本集共 {len(beat_draft.segments)} 段）",
        )
        camera_digest_by_segment_no[plan.segment_no] = draft.camera_digest
        by_segment_no[plan.segment_no] = draft
        delivered_lines.extend(
            (plan.segment_no, line.speaker_identity_id, line.line) for line in draft.dialogue
        )

    # 延迟导入：app.multiview<->app.validators 互相依赖，模块级导入会触发循环导入。
    from app.multiview import _storyboard_pack_asset_dependencies

    result: dict[int, _AiStoryboardSegmentDraft] = {}
    for plan in beat_draft.segments:
        draft = by_segment_no[plan.segment_no]
        relevant_scene_ids = {
            str(s.get("scene_id") or "")
            for s in relevant_assets_by_segment_no[plan.segment_no]["scenes"]
        }
        manifest = _storyboard_pack_asset_dependencies(
            project_id=project_id, episode_no=episode_no, shot_id=f"draft:{plan.segment_no}",
            segment={"resources": draft.resources.model_dump(mode="json")}, conn=conn, bible=bible,
        ) if conn is not None and bible is not None else None
        advisories = _segment_content_advisories(
            draft, source_segment_indexes=plan.source_segment_indexes,
            segment_relevant_scene_ids=relevant_scene_ids, manifest=manifest,
        )
        if advisories:
            draft.degraded_capabilities = [*draft.degraded_capabilities, *advisories]
        result[plan.segment_no] = draft
    return result


# ---------------------------------------------------------------------------
# 契约装配与持久化
# ---------------------------------------------------------------------------

class StoryboardPackBeat(BaseModel):
    beat_id: str
    summary: str
    segment_indexes: list[int]


class StoryboardPackSegment(BaseModel):
    segment_no: int
    duration_s: int = SEGMENT_DURATION_S
    synopsis: str
    source_segment_indexes: list[int]
    # Carries forward the model's own self-declared association from the
    # beat-sheet stage (_AiSegmentPlan.beat_ids) -- the same list already used
    # to build ``segment_units[].beats`` for this segment's own prompt (see
    # _generate_all_segment_prompts). This is the authoritative source for "which
    # beats does this segment cover"; persist_storyboard_pack must key off it
    # directly instead of re-deriving via segment_indexes/source_segment_indexes
    # set intersection, which is only a proxy and can disagree with what the
    # prompt was actually built from at edges (e.g. a beat whose
    # segment_indexes happens to overlap this segment's source range without
    # the model having assigned it here, or vice versa).
    beat_ids: list[str] = Field(default_factory=list)
    prompt_text: str
    shot_count: int
    dialogue: list[dict[str, Any]]
    resources: dict[str, Any]
    degraded_capabilities: list[str]
    #: 2.4.0：本段对它引用的每个非 paratext 原文段号声明的句单元范围
    #: （_AiSourceUnitRange.model_dump 逐条落库），落库供审计核对；默认空列表
    #: 兼容旧行（旧行没有这个字段）。
    source_unit_ranges: list[dict[str, Any]] = Field(default_factory=list)
    #: 2.1.0 对白台账：本段 kept 的原文台词，供事后核对丢没丢；默认空列表兼容旧行。
    required_dialogue: list[dict[str, Any]] = Field(default_factory=list)
    #: 2.2.0 色温弧线，落库供审计核对；默认空串兼容旧行（旧行没有这个字段）。
    palette: str = ""
    #: 2.3.0 跨段连贯性备忘（_AiContinuityMemo.model_dump），落库供审计核对；
    #: 默认空 dict 兼容旧行（旧行没有这个字段）。
    continuity_memo: dict[str, Any] = Field(default_factory=dict)
    form: str = "scene"  # WS7：同 _AiStoryboardSegmentDraft.form/beats 契约
    beats: list[dict[str, Any]] = Field(default_factory=list)
    #: WS11：蒙太奇拍点独立键——上面的 beats 在 persist 时会被改写成叙事节拍
    #: 摘要（beat_id/summary/segment_indexes），蒙太奇拍点写这里才不会被覆盖。
    montage_beats: list[dict[str, Any]] = Field(default_factory=list)


class StoryboardPack(BaseModel):
    storyboard_version: str = STORYBOARD_PACK_VERSION
    episode_no: int
    target_model: str
    beat_sheet: list[StoryboardPackBeat]
    segments: list[StoryboardPackSegment]
    #: 2.1.0 对白台账统计，只是 generate->persist 传递载体，落库形态是另开的 EvidenceArtifact。
    dialogue_ledger: dict[str, Any] = Field(default_factory=dict)


def _load_indexed_source_segments(conn, ep) -> list[SourceSegment]:
    """Segment the chapter text exactly the way app.production.prep_pack does.

    Both call sites in prep_pack.py call ``index_source_segments(source_text)``
    with no override (default max_chars=900); this reuses the identical
    function so ``segment_index`` here means the same 1-based position that
    asset_manifest/appellation_map already anchor on.
    """
    source_text = _episode_source_text(conn, ep)
    return index_source_segments(source_text)


def _manifest_speaker_names(payload: dict[str, Any]) -> list[str]:
    """本集人物谱正名 + 别名，供 storyboard_dialogue_extract 判定"说话人行"用。

    取自 ``payload["asset_manifest"]["characters"][].display_name``/``aliases``
    ——与 ``_manifest_brief_for_prompt``（storyboard_beat_sheet.py）读的是同一
    份数据，取值域来自映射台已经解析好的人物谱，不新造一套判定逻辑。
    """
    names: list[str] = []
    for character in (payload.get("asset_manifest") or {}).get("characters") or []:
        display_name = character.get("display_name")
        if display_name:
            names.append(str(display_name))
        names.extend(str(alias) for alias in (character.get("aliases") or []))
    return names


async def generate_storyboard_pack(
    episode_id: str,
    *,
    ep: Any,
    conn: Any,
    payload: dict[str, Any],
) -> StoryboardPack:
    """Generate the frozen 2.0.0 storyboard contract for a prep_pack episode.

    Answers "which function decides how many segments this episode has, and
    on what basis" (docs/STORYBOARD_PROMPT_IR_DESIGN.md 交付前必须回答 #1):
    ``_generate_beat_sheet`` -- it reads the full chapter text (not the
    now-empty event_chain) and asks the model to list narrative beats and
    group them into segments; segment count is exactly ``len(draft.segments)``
    from that one call, never computed arithmetically from duration.
    """
    episode_no = int(ep["episode_no"])
    segments = _load_indexed_source_segments(conn, ep)
    if not segments:
        raise ValueError(f"episode {episode_id} 没有可用原文，无法生成分镜")
    target_video_model = str(ep["target_video_model"] or "hiagent").strip() or "hiagent"

    bible: Bible | None = None
    project = conn.execute(
        "SELECT * FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    if project and project["bible_json"]:
        from app.portraits import bible_for_episode

        bible = bible_for_episode(
            ep["project_id"], Bible.model_validate(json.loads(project["bible_json"])), episode_no,
        )
    _enrich_asset_manifest_canonical_visuals(conn, payload, bible=bible)

    # 2.1.0：paratext 账提前算一次，抽取台词同时喂给阶段一（取值域）与阶段二（分配结果）。
    # 2.4.0：接 storyboard_dialogue_extract 的剧本格式抽取路径（真实 EP1《橘座
    # 在上》回归：剧本格式台词全写成「说话人（备注）：台词」，纯引号正则一句
    # 都抽不到，见该模块 docstring）；speaker_names 取本集人物谱正名+别名。
    paratext_indexes = _paratext_segment_indexes(payload)
    dialogue_quotes = extract_dialogue_targets(
        segments, paratext_indexes, speaker_names=_manifest_speaker_names(payload),
    )

    beat_draft = await _generate_beat_sheet(
        episode_id=episode_id, episode_no=episode_no, segments=segments, payload=payload,
        dialogue_quotes=dialogue_quotes, contract_version=STORYBOARD_PACK_VERSION,
    )
    # 防御性兜底（见 STORYBOARD_PACK_VERSION 2.0.4 changelog）：stage 1 的
    # rules[] 只是提示词层面的禁令，不是校验闸门，这里在通过格式校验之后、
    # 进入 phase 2 之前用同一份 paratext 账把漏网的引用滤掉。
    paratext_strip_notes = _strip_paratext_from_beat_draft(beat_draft, paratext_indexes)
    # 2.1.2：容量维度改成确定性归一化，不再打回模型重试（见 storyboard_capacity_normalize）。
    # 2.4.0：归一化拆段时必须同步拆 source_unit_ranges，因此需要真实原文
    # （source_segments）与 paratext_indexes——见该模块 docstring。
    capacity_normalization = normalize_and_assert_capacity(
        beat_draft, dialogue_quotes, source_segments=segments, paratext_indexes=paratext_indexes,
    )
    required_dialogue_by_segment_no = required_dialogue_for_segments(
        beat_draft.kept_lines, dialogue_quotes,
    )

    # 2.0.3/2.0.6：全部段落的 prompt_text 按整集视野产出（见
    # _generate_all_segment_prompts 文档与 STORYBOARD_PACK_VERSION 的 2.0.3/
    # 2.0.6 changelog），不再是每段一次独立调用、asyncio.gather 并行发出——
    # 旧路径下并行意味着生成第 N 段的那次调用互相看不到彼此，是跨段割裂感
    # 的结构性根因。答案装不进一次 completion 时顺序分批，后续批次带着已写
    # 段落，不退回互不可见的并行。
    segment_drafts = await _generate_all_segment_prompts(
        episode_id=episode_id,
        episode_no=episode_no,
        beat_draft=beat_draft,
        segments=segments,
        payload=payload,
        target_video_model=target_video_model,
        bible=bible,
        required_dialogue_by_segment_no=required_dialogue_by_segment_no,
        conn=conn, project_id=ep["project_id"],
    )
    pack_segments = [
        StoryboardPackSegment(
            segment_no=plan.segment_no,
            duration_s=SEGMENT_DURATION_S,
            synopsis=plan.synopsis,
            source_segment_indexes=list(plan.source_segment_indexes),
            beat_ids=list(plan.beat_ids),
            prompt_text=segment_drafts[plan.segment_no].prompt_text.strip(),
            shot_count=segment_drafts[plan.segment_no].shot_count,
            dialogue=[
                line.model_dump(mode="json")
                for line in segment_drafts[plan.segment_no].dialogue
            ],
            resources=segment_drafts[plan.segment_no].resources.model_dump(mode="json"),
            source_unit_ranges=[r.model_dump(mode="json") for r in plan.source_unit_ranges],
            # paratext_strip_notes 极罕见地非空时（见 _strip_paratext_from_beat_
            # draft 文档），留痕给每一段而不是挑一段单独记——这个信号描述的是
            # 整份 beat_draft 里出现的引用异常，不是某一段独有的问题。
            degraded_capabilities=[
                *segment_drafts[plan.segment_no].degraded_capabilities,
                *paratext_strip_notes,
            ],
            required_dialogue=required_dialogue_by_segment_no.get(plan.segment_no, []),
            palette=plan.palette,
            continuity_memo=segment_drafts[plan.segment_no].continuity_memo.model_dump(mode="json"),
            form=segment_drafts[plan.segment_no].form,
            montage_beats=[b.model_dump(mode="json") for b in segment_drafts[plan.segment_no].beats],
        )
        for plan in beat_draft.segments
    ]
    _, target_model_literal, _ = _dialect_for_target_video_model(target_video_model)
    return StoryboardPack(
        episode_no=episode_no,
        target_model=target_model_literal,
        beat_sheet=[
            StoryboardPackBeat(
                beat_id=beat.beat_id, summary=beat.summary, segment_indexes=list(beat.segment_indexes),
            )
            for beat in beat_draft.beat_sheet
        ],
        segments=list(pack_segments),
        dialogue_ledger=dialogue_ledger_summary(
            dialogue_quotes, beat_draft.kept_lines, beat_draft.dropped_lines, capacity_normalization,
        ),
    )


def _resource_identity_display_names(payload: dict[str, Any], identity_ids: list[str]) -> list[str]:
    by_id = {
        str(c.get("identity_id") or ""): str(c.get("display_name") or c.get("identity_id") or "")
        for c in (payload.get("asset_manifest") or {}).get("characters") or []
    }
    return [by_id.get(identity_id, identity_id) for identity_id in identity_ids]


def _resource_scene_display_name(payload: dict[str, Any], scene_id: str | None) -> str:
    """段级 ``resources.scenes[]``（``_AiResourceScene``）没有 ``display_name``
    字段——只有 ``scene_id``/``scene_reference_id``/``description``。展示名只在
    episode 级 ``asset_manifest.scenes[]`` 里，按 ``scene_id`` 反查。之前直接读
    ``scene_entries[0].get("display_name")`` 在这个模型上永远是 None，导致
    ``shots.scene_name`` 对分镜台 2.0.0 的行全部写成空——参考图解析
    （app.multiview.resolve_shot_asset_dependencies）按名字找场景，名字为空就
    找不到，场景参考图因此从未被附加过。"""
    if not scene_id:
        return ""
    by_id = {
        str(s.get("scene_id") or ""): str(s.get("display_name") or "")
        for s in (payload.get("asset_manifest") or {}).get("scenes") or []
    }
    return by_id.get(str(scene_id), "")


def _largest_contiguous_source_run(indexes: list[int]) -> list[int]:
    """Pick the one run this shot's source binding can honestly prove.

    ``storyboard_source_bindings`` is schema-fixed to one (chapter, start,
    end) span per shot (app/db.py: ``shot_id TEXT PRIMARY KEY``), and
    ``assert_storyboard_source_bindings_complete`` requires the sliced
    ``content[start:end]`` to equal ``shots.source_excerpt`` byte-for-byte.
    Real ``source_segment_indexes`` are routinely non-contiguous (EP1 data:
    shot 2 = [12,14,15,16,17], skipping 13; shot 6 = [44,46,47,48], skipping
    45) because the model omits a connective paragraph it judged irrelevant
    to this narrative beat. Taking the naive ``min(indexes)..max(indexes)``
    envelope would silently claim the skipped paragraph as part of this
    shot's verified excerpt -- pretending a non-contiguous reference is
    contiguous. Instead this keeps only the longest actually-contiguous run
    (ties broken toward the earliest/smallest-starting run, for
    determinism); indexes outside that run are not represented in
    ``shots.source_excerpt`` or the binding.
    """
    unique_sorted = sorted(set(indexes))
    if not unique_sorted:
        return []
    runs: list[list[int]] = []
    current = [unique_sorted[0]]
    for value in unique_sorted[1:]:
        if value == current[-1] + 1:
            current.append(value)
        else:
            runs.append(current)
            current = [value]
    runs.append(current)
    return max(runs, key=lambda run: (len(run), -run[0]))


def _segment_matched_timeline_anchors(
    timeline_by_segment: dict[int, list[dict[str, Any]]], source_segment_indexes: list[int],
) -> list[dict[str, Any]]:
    """WS9：本段命中的 ``episode_prep_pack.timeline.segments`` 锚点，按段号去重合并。"""
    seen: set[str] = set()
    matched: list[dict[str, Any]] = []
    for index in source_segment_indexes:
        for anchor in timeline_by_segment.get(index, []):
            key = f"{anchor.get('kind')}:{anchor.get('value')}:{anchor.get('chapter_index')}"
            if key in seen:
                continue
            seen.add(key)
            matched.append(anchor)
    return matched


def _timeline_anchor_scene_time(anchors: list[dict[str, Any]]) -> str:
    """段落锚点 -> ``shots.scene_time``：没有锚点保持空串（不兜底）；有锚点取
    原文逐字 ``value``，多个候选按 age > year > era > relative 取最具体的一条。"""
    if not anchors:
        return ""
    priority = {"age": 0, "year": 1, "era": 2, "relative": 3}
    best = min(anchors, key=lambda a: priority.get(a.get("kind"), 9))
    return str(best.get("value") or "")


def _resolve_segment_source_binding(
    *,
    segment_no: int,
    source_segment_indexes: list[int],
    segments: list[SourceSegment],
    full_source_text: str,
    authorized_sources: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Derive (source_excerpt, normalized binding) for one persisted shot row.

    ``normalized`` matches exactly the shape ``app.storyboard_workspace.
    persist_source_binding`` expects and the legacy pipeline already produces
    (see ``verify_or_bind_existing_excerpt``/``align_generated_source_evidence``
    in app/storyboard_workspace.py): binding_kind/chapter_id/chapter_idx/
    source_version_hash/start_offset/end_offset/excerpt_hash, offsets always
    chapter-local (matched against ``chapters.content``, not the multi-chapter
    joined text ``index_source_segments`` was run against).

    The excerpt text itself is the literal joined-text slice for the chosen
    contiguous run (``full_source_text[start:end]``), not a
    ``"\\n".join(segment.text ...)`` reconstruction -- ``index_source_segments``
    splits on blank-line boundaries (``\\n\\s*\\n``), so a single ``\\n`` join
    does not reproduce the real inter-paragraph whitespace and would already
    fail the binding's byte-for-byte check even for a fully contiguous run.
    Locating which authorized chapter contains that literal slice (and its
    chapter-local offset) is done the same way the legacy pipeline does it --
    ``content.find(excerpt)`` against each authorized chapter -- rather than
    re-deriving offsets from the join format, which would be fragile across
    multi-chapter episodes.

    One join artifact needs an explicit unwrap: ``_episode_source_text``
    (app/domain/common.py) prefixes each chapter's block with
    ``【{title}】\\n`` before joining, so ``index_source_segments`` frequently
    folds that bracketed heading into segment 1 of a chapter (real EP1 data:
    segment 1's text is literally ``"【第一章书生孟浩】\\n第一章书生孟浩"`` --
    chapters.content itself repeats its own title as its first line, see
    ``app.source_excerpt.chapter_title_segment_ids``'s docstring for the same
    join artifact). ``chapters.content`` never contains the bracket wrapper,
    only chapters.content's own text, so a run starting at a chapter's first
    segment must have that wrapper stripped before ``content.find`` can ever
    match -- this is not a fuzzy/best-effort fallback, the wrapper is a fixed,
    known literal (``_episode_source_text``'s own format string), so this
    reproduces exactly the bytes ``chapters.content`` actually holds rather
    than approximating them.
    """
    valid_indexes = sorted({i for i in source_segment_indexes if 1 <= i <= len(segments)})
    if not valid_indexes:
        raise ValueError(
            f"第 {segment_no} 段没有落在原文分段范围内的段号（{source_segment_indexes}），无法生成原文绑定"
        )
    run = _largest_contiguous_source_run(valid_indexes)
    start = segments[run[0] - 1].start_offset
    end = segments[run[-1] - 1].end_offset
    excerpt = full_source_text[start:end]
    for source in authorized_sources:
        content = str(source.get("content") or "")
        title = str(source.get("title") or "")
        candidate = excerpt
        wrapper = f"【{title}】\n"
        if title and excerpt.startswith(wrapper):
            candidate = excerpt[len(wrapper):]
        local_start = content.find(candidate)
        if local_start >= 0:
            return candidate, {
                "binding_kind": "source_excerpt",
                "chapter_id": int(source["id"]),
                "chapter_idx": int(source["idx"]),
                "source_version_hash": source["source_version_hash"],
                "start_offset": local_start,
                "end_offset": local_start + len(candidate),
                "excerpt_hash": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
            }
    raise ValueError(
        f"第 {segment_no} 段原文摘录（原文段号 {valid_indexes}）无法在本集授权章节中定位"
    )


def persist_storyboard_pack(
    conn,
    episode_id: str,
    ep: Any,
    payload: dict[str, Any],
    pack: StoryboardPack,
    *,
    segments: list[SourceSegment] | None = None,
) -> list[str]:
    """Write one ``shots`` row per segment. No ``shot_versions`` row and no
    adoption -- this shot has not generated anything yet (see the no-placeholder
    note above the loop body for why).

    Answers "does anything post-process the model's prompt_text"
    (docs/STORYBOARD_PROMPT_IR_DESIGN.md 交付前必须回答 #2): no. ``draft.prompt_text``
    is ``.strip()``-ed in ``generate_storyboard_pack`` and then written verbatim
    into ``shots.shot_contract_json.storyboard_pack_segment.prompt_text`` below
    (via ``segment_record = segment.model_dump(mode="json")``) -- there is no
    render/compile step between the model call and persistence, unlike the
    legacy ``_render_seedance_prompt``/``_render_minimax_h3_prompt`` code path in
    app/video_prompt_ai.py that this module replaces for prep_pack episodes.

    One 15s segment = one shots row (user-frozen decision): the 2-4 internal
    shot cuts live inside prompt_text as free text, never split into separate
    rows. shot_size/camera_move/camera_angle/first_frame_desc/last_frame_desc
    are left empty -- they describe a single continuous camera setup, a
    granularity this row no longer has; the marker
    ``prompt_contract_version=STORYBOARD_PACK_CONTRACT_MARKER`` is how every
    consumer (app/continuity.py, app/validators.py, app/domain/video_ops.py) knows to
    stop treating those columns as authoritative for this row instead of
    silently failing or silently passing on empty values. ``scene_time`` is the
    one exception (WS9): still means "when, not where", populated verbatim
    from this segment's matched timeline anchor when the source text had one.
    """
    from app.domain.storyboard_ops.mutation_primitives import (
        _assert_storyboard_write_authorized,
    )
    from app.storyboard_workspace import chapter_sources, persist_source_binding

    _assert_storyboard_write_authorized(conn, episode_id, None)
    if segments is None:
        segments = _load_indexed_source_segments(conn, ep)
    full_source_text = _episode_source_text(conn, ep)
    authorized_sources = chapter_sources(episode_id, conn=conn)
    conn.execute("DELETE FROM shots WHERE episode_id=?", (episode_id,))
    beats_by_id = {beat.beat_id: beat for beat in pack.beat_sheet}
    # WS9：payload["timeline"]["segments"]（见 .prep_pack.timeline_segments）
    # 按段号匹配回本段；旧 payload 无此字段时下面每段拿到空列表，行为不变。
    timeline_by_segment = {
        int(entry.get("index") or 0): entry.get("anchors") or []
        for entry in ((payload.get("timeline") or {}).get("segments") or [])
    }
    shot_ids: list[str] = []
    for segment in pack.segments:
        character_ids, extra_reconcile_notes = resolve_persisted_character_ids(
            payload, [str(c.get("identity_id") or "") for c in (segment.resources.get("characters") or [])],
            segment_source_indexes=segment.source_segment_indexes)
        scene_entries = segment.resources.get("scenes") or []
        scene_display_name = (
            _resource_scene_display_name(payload, scene_entries[0].get("scene_id"))
            if scene_entries else ""
        )
        source_excerpt, source_binding = _resolve_segment_source_binding(
            segment_no=segment.segment_no,
            source_segment_indexes=segment.source_segment_indexes,
            segments=segments,
            full_source_text=full_source_text,
            authorized_sources=authorized_sources,
        )
        segment_timeline_anchors = _segment_matched_timeline_anchors(
            timeline_by_segment, segment.source_segment_indexes,
        )
        scene_time = _timeline_anchor_scene_time(segment_timeline_anchors)
        shot_id = new_id("shot")
        shot_uid = new_id("shotuid")
        segment_record = segment.model_dump(mode="json")
        # WS12：归并歧义提示并入 degraded_capabilities（见 app.production.
        # storyboard_extras_reconcile 模块 docstring）。
        if extra_reconcile_notes:
            segment_record["degraded_capabilities"] += extra_reconcile_notes
        # WS9：附加键，不覆盖既有 "beats"；供 resource_forecast 等读锚点详情。
        segment_record["timeline_anchors"] = segment_timeline_anchors
        # WS11：montage_beats 真源在这——不得再被下面 beat_ids 摘要重写覆盖
        # （见 app.production.storyboard_pack_montage 模块文档的陈年 bug）；
        # 没填 time_anchor 的拍用本段确定性锚点回填。
        segment_record["montage_beats"] = fill_montage_beat_time_anchors(
            segment_record.get("montage_beats") or [], segment_timeline_anchors,
        )
        # 段落自报的 beat_ids（_AiSegmentPlan.beat_ids）是唯一真源，与本段
        # prompt 用的同一份关联；_validate_beat_sheet_draft 已保证每个
        # beat_id 都在 pack.beat_sheet 里。不再靠 segment_indexes 与
        # beat.segment_indexes 的交集代理判定——交集在边界处可能与模型自报
        # 的结果不一致（例如原文范围调整但 beat_ids 未变，或反之）。
        matched_beats = [
            beats_by_id[beat_id] for beat_id in segment.beat_ids if beat_id in beats_by_id
        ]
        # beat_ids（裸 id 列表）是前端既有读取键，原样保留；beats 是自包含
        # 字段——拿到这一个 shot 就能知道它承载的节拍在讲什么，字段名沿用
        # 冻结契约（beat_id/summary/segment_indexes），不发明新名。
        segment_record["beat_ids"] = [beat.beat_id for beat in matched_beats]
        segment_record["beats"] = [
            {
                "beat_id": beat.beat_id,
                "summary": beat.summary,
                "segment_indexes": list(beat.segment_indexes),
            }
            for beat in matched_beats
        ]
        segment_record["target_model"] = pack.target_model
        segment_record["storyboard_version"] = pack.storyboard_version
        is_final = segment.segment_no == len(pack.segments)
        dialogues = [
            Dialogue(
                speaker=str(line.get("speaker_identity_id") or ""),
                line=str(line.get("line") or ""),
                emotion="平静",
                delivery=str(line.get("delivery") or "spoken_dialogue"),  # 2.1.0：旧行无此键按前者处理
            ).model_dump()
            for line in segment.dialogue
        ]
        # continuity_mode/transition/first_frame_desc/last_frame_desc describe a
        # single continuous camera setup and are not meaningful once one row
        # covers 2-4 internal cuts; left at their non-committal defaults rather
        # than a fabricated enum value (this row's prompt_contract_version marker
        # is what tells every downstream consumer to stop reading these as
        # authoritative -- see the module docstring and app/continuity.py).
        conn.execute(
            "INSERT INTO shots(id, shot_uid, episode_id, script_id, shot_no, duration_s, "
            "shot_size, camera_move, scene_time, scene_setting, scene_name, characters, "
            "action_desc, first_frame_desc, last_frame_desc, source_excerpt, narration, "
            "dialogues, transition, continuity_from_prev, shot_contract_json, "
            "continuity_mode, observed_state_out, storyboard_artifact_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                shot_id, shot_uid, episode_id, None, segment.segment_no, segment.duration_s,
                "", "", scene_time, "",
                scene_display_name or None,
                json.dumps(
                    _resource_identity_display_names(payload, character_ids),
                    ensure_ascii=False,
                ),
                segment.synopsis, "", "",
                source_excerpt,
                segment.synopsis,
                json.dumps(dialogues, ensure_ascii=False),
                "硬切", 0,
                json.dumps(
                    {
                        "storyboard_pack_segment": segment_record,
                        "prompt_contract_version": STORYBOARD_PACK_CONTRACT_MARKER,
                        "is_final": is_final,
                    },
                    ensure_ascii=False,
                ),
                "", "", None,
            ),
        )
        # No placeholder shot_versions row and no adoption here. This shot has
        # not generated anything yet -- adopted_version_id must stay NULL
        # until a real job produces a succeeded version with a video file
        # (app.media_exec.enqueue / app.evidence.media.select_best_video_candidate
        # own that transition). prompt_text is not orphaned by skipping the
        # placeholder: it already lives verbatim in this row's own
        # shot_contract_json.storyboard_pack_segment.prompt_text (segment_record
        # above is segment.model_dump(mode="json"), which includes prompt_text),
        # and app.media_exec.enqueue reads it from there for the first real
        # generation. Previously this function inserted a version_no=1 row with
        # status='queued'/video_path=NULL and immediately pointed
        # shots.adopted_version_id at it -- every shot looked "adopted" the
        # instant the storyboard was persisted, before any video ever existed,
        # and it burned version slot 1 so the first real generation became v2.
        # storyboard_source_bindings is the provable "this excerpt really is
        # this chapter, this offset range, source unchanged since" pointer
        # app.storyboard_workspace.assert_storyboard_source_bindings_complete
        # gates on -- without this the row's source_excerpt above is an
        # unverifiable free-text field and every shot fails that gate.
        persist_source_binding(shot_id, source_binding, conn=conn, commit=False)
        shot_ids.append(shot_id)

    # Full beat_sheet (with summaries), stored once per generation independent
    # of any single segment row. Per-segment ``beats`` above only carries the
    # beats each segment overlaps -- it cannot answer "how was the segment
    # count decided" on its own if a beat somehow ends up unclaimed by every
    # segment, and it duplicates the same beat's summary across every segment
    # it touches instead of having one canonical copy. This artifact is that
    # canonical copy: an auditable record of exactly what
    # ``_generate_beat_sheet`` produced (segment_count here is
    # ``len(pack.segments)``, i.e. the number this whole module exists to
    # decide -- see ``generate_storyboard_pack``'s docstring).
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="storyboard_pack_beat_sheet",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T2",
            content={
                "storyboard_version": pack.storyboard_version,
                "episode_no": pack.episode_no,
                "target_model": pack.target_model,
                "segment_count": len(pack.segments),
                "beat_sheet": [beat.model_dump(mode="json") for beat in pack.beat_sheet],
            },
            parent_artifact_ids=(
                [str(ep["screenplay_artifact_id"])] if ep["screenplay_artifact_id"] else []
            ),
            contract_version=pack.storyboard_version,
        ),
        conn=conn,
        commit=False,
    )
    # 2.1.0：episode 级对白台账，与上面 beat_sheet 产物同一模式、同一事务。
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="storyboard_pack_dialogue_ledger",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T2",
            content=pack.dialogue_ledger,
            parent_artifact_ids=(
                [str(ep["screenplay_artifact_id"])] if ep["screenplay_artifact_id"] else []
            ),
            contract_version=pack.storyboard_version,
        ),
        conn=conn,
        commit=False,
    )
    conn.commit()
    return shot_ids


async def run_storyboard_pack_generation(
    episode_id: str,
    *,
    ep: Any,
    conn: Any,
    payload: dict[str, Any],
    resume: bool = True,
):
    """Entry point called from app.storyboard_supervisor.run_storyboard_supervisor
    for every episode whose screenplay_json is an episode_prep_pack payload.

    This intentionally does not touch the legacy checkpoint-driven repair
    state machine (PLANNING_OUTLINE / GENERATING_SHOTS / REPAIRING / ...) that
    the rest of run_storyboard_supervisor implements: that machinery exists to
    incrementally repair a 50-field per-shot narrative contract one shot at a
    time, keyed off screenplay.narrative_plan / screenplay.events, which
    episode_prep_pack (2.0.0) structurally does not have. Phase two (all
    segments' prompt_text) is a whole-episode-context generation as of 2.0.3,
    split into sequential answer-budget batches as of 2.0.6 when thinking
    tokens would otherwise saturate max_output_tokens; each batch is retried
    internally by model_gateway.chat_structured's own format/semantic retry
    budget. There is no equivalent multi-shot repair loop to run -- a failure
    there fails the whole episode's generation, not one segment (see
    STORYBOARD_PACK_VERSION's 2.0.3/2.0.6 changelog). On success this
    reuses the exact same completion contract the legacy path uses for its
    non-narrative-authority branch (app.storyboard_supervisor.py's own
    ``else: _finalize_storyboard_evidence(episode_id, evaluation.board)`` /
    ``cp.phase = "SUCCEEDED"`` tail) so publish/certificate/evidence and the
    confirmation gate see the same shape of "done" they already know how to
    handle.
    """
    from app.storyboard_supervisor import SupervisorCheckpoint, save_checkpoint
    from app.domain.storyboard_ops.evidence import _finalize_storyboard_evidence
    from app.domain.storyboard_ops.mutation_primitives import _board_from_shot_rows

    if resume:
        existing = conn.execute(
            "SELECT id, shot_contract_json FROM shots WHERE episode_id=? ORDER BY shot_no",
            (episode_id,),
        ).fetchall()
        tail_contract: dict[str, Any] = {}
        if existing:
            try:
                tail_contract = json.loads(existing[-1]["shot_contract_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                tail_contract = {}
        if (
            existing
            and all(
                STORYBOARD_PACK_CONTRACT_MARKER
                in (row["shot_contract_json"] or "") for row in existing
            )
            and bool(tail_contract.get("is_final"))
        ):
            # 判据完全落在产物本身，不看 episodes.status：
            # 1) 每一行都带当前 STORYBOARD_PACK_CONTRACT_MARKER —— 同一套契约生成的。
            # 2) 尾镜自带 is_final=True —— persist_storyboard_pack 只在
            #    generate_storyboard_pack 一次整跑成功、写完 pack.segments 的最后一段
            #    时才会落这个标记（本模块的持久化是单事务、一次性全写，不存在
            #    "写了一半"的中间态），所以 is_final=True 就等价于"这是一整套完整
            #    产物，不是半途残留"。
            # 之前这里判的是 ``ep["status"] in ("scripted","confirmed","generating",
            # "done")``——事故根因就在这儿：resume_storyboard()（app/domain/
            # storyboard_ops.py）这个 HTTP 路由，在派发生成任务之前，自己会先把
            # episodes.status 改成 'scripting' 并提交（给 _storyboard_generation_is_live
            # 之类的去重用），然后才 spawn 任务；run_storyboard_supervisor 随后重新
            # SELECT 出来的 ep 快照因此必然是 'scripting'——不在允许列表里，短路
            # 必然判不过，100% 落到下面的全量重灌分支，不是偶发。真实事故
            # （ep_3d523ff4d0a4，run_84f1d96f9963 把已通过的 10 段吃成 7 段）正是
            # 这个必然失败的短路触发的。episodes.status 是会被同一次请求自己的写
            # 操作耦合改动的外部可变字段，不该是"这批产物完不完整"的判据——判据必须
            # 只看产物自己（marker + is_final）。resume 语义下不重新调模型、不 DELETE
            # 重灌——那会连同已经采纳/生成的视频版本一起级联删掉（shots ->
            # shot_versions -> jobs 都是 ON DELETE CASCADE）。直接把已持久化的结果
            # 重建成 checkpoint 返回。
            rows = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
            ).fetchall()
            cp = SupervisorCheckpoint(
                episode_id=episode_id,
                phase="SUCCEEDED",
                outcome="SUCCEEDED_READY_FOR_CONFIRM",
                expected_total=len(rows),
                validated_prefix_end=len(rows),
                next_shot_no=len(rows) + 1,
                input_versions={"screenplay_artifact_id": ep["screenplay_artifact_id"]},
            )
            # 上面那段事故记录只修好了"要不要重灌"，没有修"短路成功后
            # episodes.status 仍停留在调用方写入的 'scripting'"这一半——
            # resume_storyboard() 派发前把 status 改成 'scripting' 并提交，
            # 这条快路径命中后从不写回，episode 永远卡在 'scripting'。
            # app.domain.video_ops._is_storyboard_terminal_for_confirmation
            # 与 app.media_exec.enqueue._enqueue_shot_impl 都硬性要求 status
            # 落在 scripted/confirmed/generating/done 才放行确认与付费生成，
            # 卡在 'scripting' 等于确认与生成入口永久打不开（实测复现：
            # ep_3d523ff4d0a4 8 段全部通过、完成凭证齐全，仍无法确认）。
            # 判据同上一段注释：只看产物本身（已经过 marker+is_final 判定），
            # 不看调用方写入的中间态；只在还没到达更高状态时补齐这条终态标记，
            # 已经推进到 confirmed/generating/done 的不回退。
            if ep["status"] not in ("scripted", "confirmed", "generating", "done"):
                conn.execute(
                    "UPDATE episodes SET status='scripted', script_error=NULL WHERE id=?",
                    (episode_id,),
                )
                conn.commit()
            save_checkpoint(cp)
            return cp

    segments = _load_indexed_source_segments(conn, ep)
    pack = await generate_storyboard_pack(episode_id, ep=ep, conn=conn, payload=payload)
    persist_storyboard_pack(conn, episode_id, ep, payload, pack, segments=segments)

    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    board = _board_from_shot_rows(rows, int(ep["episode_no"]))
    _finalize_storyboard_evidence(episode_id, board)
    conn.execute(
        "UPDATE episodes SET status='scripted', script_error=NULL WHERE id=?",
        (episode_id,),
    )
    conn.commit()

    cp = SupervisorCheckpoint(
        episode_id=episode_id,
        phase="SUCCEEDED",
        outcome="SUCCEEDED_READY_FOR_CONFIRM",
        expected_total=len(pack.segments),
        validated_prefix_end=len(pack.segments),
        next_shot_no=len(pack.segments) + 1,
        input_versions={
            "screenplay_artifact_id": ep["screenplay_artifact_id"],
        },
    )
    save_checkpoint(cp)
    return cp
