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
      固定 15 秒 / 3-4 镜）——这一步决定「这一集有几段」，是整个改造的
      支点：取消事件链之后，段数不再由上游给，必须由本阶段从原文推导。
    阶段二：把全部段落的原文切片 + 节拍表 + 全集人物/场景/道具资源 +
      目标模型（Seedance 2.0 / MiniMax H3）的方言约束一起打包，交给模型
      一次调用，模型对每一段各产出一整块可复制的 prompt_text（代码不再
      拼装、不挂尾缀——对照 app/video_prompt_ai.py 的 _render_seedance_
      prompt / _render_minimax_h3_prompt，那是本模块要替代的、按草稿
      字段拼接最终字符串的旧路径；这里模型的 prompt_text 只做
      strip()，不做任何字段级重组），再按 segment_no 分配回各段。这一步
      直到 2.0.3 之前是「每段一次独立调用、asyncio.gather 并行发出」，
      见下方 2.0.3 changelog——用户从链路观测里看出这是分段视频割裂感的
      结构性根因并拍板改造。

持久化形状（用户已拍板，不是本模块自行决定）：一个 15 秒段 = shots 表一行，
段内的 3-4 个镜头切换写在 prompt_text 文本里，不拆成独立数据行。因此
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
from typing import Any

from pydantic import BaseModel, Field

from app import config
from app.db import new_id
from app.domain.common import _episode_source_text
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.schemas import Bible, Dialogue
from app.source_excerpt import SourceSegment, index_source_segments
from app.video_prompt_profiles import VideoPromptProfile

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
STORYBOARD_PACK_VERSION = "2.0.5"

#: Written to Shot.prompt_contract_version for every row this module writes.
#: This is the single, principled marker every downstream consumer keys off
#: of to know "this row's shot_size/camera_move/first_frame_desc/... are not
#: authoritative -- read storyboard_pack_segment.prompt_text instead". It is
#: a data-derived version tag, not a per-episode/per-shot allowlist.
STORYBOARD_PACK_CONTRACT_MARKER = "storyboard_pack/2.0.5"

SEGMENT_DURATION_S = 15
MIN_SHOTS_PER_SEGMENT = 3
MAX_SHOTS_PER_SEGMENT = 4

#: 阶段二整集批量调用的 answer 预算（业务只按「答案要多大」算，harness 自己
#: 叠加 reasoning 预留并夹到模型硬顶——见 app.hiagent.text_request_token_
#: limits，2.0.3 changelog 有完整测量数据）。每段系数 2400 取自真实历史
#: 单段调用 completion_tokens 观测（202 次 storyboard_pack_segment 调用，
#: 均值 1018、最大 1897），乘约 1.3 的安全系数覆盖新设计里因跨段连续性
#: 陈述而变长的段落；下限 8000 保证段数很少的集也有余量。
SEGMENT_BATCH_TOKENS_PER_SEGMENT = 2400
SEGMENT_BATCH_TOKENS_FLOOR = 8000


# ---------------------------------------------------------------------------
# 方言约束（供第二阶段模型使用；对照 docs/STORYBOARD_PROMPT_IR_DESIGN.md 的
# 对照表与 docs/prompt-skills/{novel-to-storyboard,minimax-h3-prompts}/。
# H3 的字段名与固定语法是接口约定，逐字符照抄；Seedance 是自由散文，按同一
# 精神收窄成可执行规则，不是逐字抄 skill 原文。
# ---------------------------------------------------------------------------

SEEDANCE_DIALECT_INSTRUCTIONS = """\
目标模型：Seedance 2.0（中文自由散文，一整块可直接复制的提示词，不要拆成
JSON 字段或分点罗列）。

- 第一句必须是「电影级预告片质感，多镜头叙事，镜头之间硬切。」——这是触发
  15 秒档多镜头模式的固定锚句，照写，不得省略或改写。
- 用「镜头1（约0-X秒）」「镜头2（约X-Y秒）」……序号排列，本段固定 3-4 镜；
  括号里的秒数只是软提示，不是精确切点，不要为了卡秒数牺牲镜头数。
- 每个镜头描述顺序：一个运镜（推近/拉远/横摇/固定/跟随/环绕，只选一个，不
  要复合运镜）→ 主体（用 @角色名 引用）→ 一个具体动作 → 场景 → 光影。
- 角色第一次出现时用至少三个可视觉验证特征定义（年龄区间、体型脸型、发型
  头饰、服装颜色材质、随身物）；此后每一个「镜头N」都要重新 @点名，并把
  头巾/伤疤/随身物等连续性元素重复写一遍——不能只在开头写一次就假设模型
  记得，这是段内漂移最常见的根因。relevant_assets 里每个角色/场景都带一个
  外观/场景字段（角色是 appearance，场景是 scene_canonical）：内容是一段
  具体描述时，那就是这个角色/场景在本集的标准锚点，第一次出现时的特征定义
  必须逐字沿用这段描述本身，不得改写、精简、替换或按本段情境调整；内容是
  「没有标准外观/场景……」这类说明文字时，才由你自行确定特征，并让同一
  角色/场景在本集所有出现它的镜头里沿用同一套自定特征。
- 情绪一律写成面部肌肉动作和肢体动作（例如「眉毛拧起、嘴大张、眼睛瞪圆」），
  不写抽象情绪词（「惊恐」「释然」这类词模型没有稳定映射）。
- 承担叙事功能的关键道具要写成构图约束——「XX始终清晰可见，位于画面XX」，
  不要只写道具被作用的动作（「玉佩砸入水面」会让道具直接消失在水花里）。
- 群像要正向锁人数并加负向排除，例如「画面中只有两名绿袍修士，不出现其他
  人物」，两句缺一都会导致模型自己加人。
- 神通/异能等超自然效果用物理描述代替文化词（「化作长虹」→「一道细长银白
  光带以极高速度横穿画面并留下拖影」）。
- 若这是全片收尾段，最后一镜必须是大远景或缓慢升起拉远的格局镜，不能停在
  人物中近景上。
- 结尾必须有一段「全片贯穿：音频……；风格……；约束……」，音频（环境音/对白/
  配乐）不能留空，约束里必须包含「面部一致、手指正确、人数锁定、无字幕
  水印」。dialogue[] 里的每一句台词都必须在这段音频描述里用引号带出原话、
  逐句出现，不能只写「XX说话声」这类概括；反过来，音频描述里用引号写出的
  台词原话也必须逐句同时登记进 dialogue[]——两处台词是同一份清单的两种
  呈现，不是各自独立的两份内容。
- 画面中任何需要出现的文字（牌匾、书信、标题）一律写「无字」/「空白」，交给
  后期合成——Seedance 对汉字字形的还原极不稳定，这是能力缺失，不是可选项。
  凡是写了「无字」的地方，必须在 degraded_capabilities 里对应记一条后期文字
  合成清单条目（写清载体是什么、原文应该是什么字）。
"""

MINIMAX_H3_DIALECT_INSTRUCTIONS = """\
Target model: MiniMax H3. The prompt is exactly three fields, field names
copied character-for-character, each separated by a blank line (T2VA mode --
no image-alignment instruction line, since this project uses reference-image
mode instead of first/last-frame chaining):

integrated_multimodal_description: [Shot 1] <style>, <description>... [Shot 2]
At 00:0X.000, the camera cuts to ...

overall_soundscape: <1-4 English sentences>

non_diegetic_music: <1-3 English sentences, or N/A>

Rules:
- integrated_multimodal_description opens with "[Shot 1]" (no timestamp),
  first declaring the overall style (e.g. "Live-action, cinematic" or
  "2D-animated"), then subsequent shots use "[Shot N] At 00:SS.sss, the
  camera cuts to ..." with strictly increasing timestamps. This segment is a
  fixed 15 seconds; write 3-4 Shots total.
- Write all descriptive prose in English. Keep dialogue and any on-screen
  text verbatim in their original language -- do not translate them.
- Camera moves are one natural English sentence combining type + amplitude +
  speed (e.g. "The camera pushes in with small amplitude at slow speed
  toward her hands"), not a stack of tags at the end of the sentence. One
  dominant camera move per shot.
- Speaking characters get a stable ID: (S1), (S2)... reused across shots for
  the same character. Put age/voice/accent context outside the <d> block;
  dialogue text goes verbatim inside: (S1) says: <d>[Chinese] 原话</d>.
  Off-screen voice uses "says in an off-screen voiceover" and must state the
  on-screen character's lips remain closed.
- overall_soundscape must never be left empty -- H3 is audio-visual joint
  generation and an empty field means the model invents uncontrolled sound.
  Only write "N/A" if the user explicitly wants total silence.
- non_diegetic_music: instrument / tempo / rhythm / dynamics language, not
  abstract mood words ("sad music" is invalid; "a slow solo piano note with
  a swelling low string" is valid). No music -> write "N/A".
- On-screen text (signs, letters, titles) is H3's strong suit: quote it
  verbatim in double quotes inside integrated_multimodal_description, e.g.
  reading "靠山宗". Do not translate it.
- A cut must carry new information (subject/space/state/viewpoint/time).
  Reframing alone is a camera move, not a cut.
- Reference character/scene images are attached separately by the platform,
  not embedded as an alignment instruction line; refer to them inline by
  role, e.g. "the character shown in the reference image, wearing ..." --
  give each reference material exactly one stated role, never let two
  references' roles overlap.
"""


def _dialect_for_target_video_model(target_video_model: str) -> tuple[VideoPromptProfile, str, str]:
    """Return (profile, target_model_literal, dialect_instructions).

    ``target_model`` uses the frozen contract's own vocabulary
    ("seedance_2" | "minimax_h3"), derived from the resolved prompt profile
    rather than hard-coded off the raw provider key, so this stays correct
    if a provider's profile binding ever changes.
    """
    from app.video_prompt_profiles import resolve_video_prompt_profile

    profile = resolve_video_prompt_profile(provider=target_video_model)
    if profile.render_format == "minimax_h3_native_fields":
        return profile, "minimax_h3", MINIMAX_H3_DIALECT_INSTRUCTIONS
    return profile, "seedance_2", SEEDANCE_DIALECT_INSTRUCTIONS


# ---------------------------------------------------------------------------
# 阶段一：节拍表 + 分段
# ---------------------------------------------------------------------------

class _AiBeat(BaseModel):
    beat_id: str
    summary: str
    segment_indexes: list[int] = Field(min_length=1)


class _AiSegmentPlan(BaseModel):
    segment_no: int
    synopsis: str
    source_segment_indexes: list[int] = Field(min_length=1)
    beat_ids: list[str] = Field(default_factory=list)


class _AiBeatSheetDraft(BaseModel):
    beat_sheet: list[_AiBeat] = Field(min_length=1)
    segments: list[_AiSegmentPlan] = Field(min_length=1)


def _validate_beat_sheet_draft(
    draft: _AiBeatSheetDraft, *, total_segments: int
) -> list[str]:
    errors: list[str] = []
    beat_ids = {beat.beat_id for beat in draft.beat_sheet}
    if len(beat_ids) != len(draft.beat_sheet):
        errors.append("beat_sheet 中 beat_id 必须唯一")
    for beat in draft.beat_sheet:
        bad = [i for i in beat.segment_indexes if i < 1 or i > total_segments]
        if bad:
            errors.append(f"beat {beat.beat_id} 引用了不存在的原文段号 {bad}")
    expected_nos = list(range(1, len(draft.segments) + 1))
    actual_nos = [s.segment_no for s in draft.segments]
    if actual_nos != expected_nos:
        errors.append(f"segments[].segment_no 必须为连续递增 1..{len(draft.segments)}，当前为 {actual_nos}")
    for seg in draft.segments:
        bad = [i for i in seg.source_segment_indexes if i < 1 or i > total_segments]
        if bad:
            errors.append(f"段 {seg.segment_no} 引用了不存在的原文段号 {bad}")
        unknown_beats = [b for b in seg.beat_ids if b not in beat_ids]
        if unknown_beats:
            errors.append(f"段 {seg.segment_no} 引用了不存在的 beat_id {unknown_beats}")
    return errors


def _manifest_brief_for_prompt(payload: dict[str, Any]) -> dict[str, Any]:
    """Compact asset_manifest summary handed to the model as light context.

    Only names/ids/segment_indexes -- not portrait binaries or provenance --
    so phase 1 (which only needs to recognize named entities while drafting
    the beat sheet) doesn't pay for the full manifest payload twice.
    """
    manifest = payload.get("asset_manifest") or {}
    return {
        "characters": [
            {
                "identity_id": c.get("identity_id"),
                "display_name": c.get("display_name"),
                "aliases": c.get("aliases") or [],
                "segment_indexes": c.get("segment_indexes") or [],
            }
            for c in (manifest.get("characters") or [])
        ],
        "scenes": [
            {
                "scene_id": s.get("scene_id"),
                "display_name": s.get("display_name"),
                "segment_indexes": s.get("segment_indexes") or [],
            }
            for s in (manifest.get("scenes") or [])
        ],
        "props": [
            {
                "label": p.get("label"),
                "segment_indexes": p.get("segment_indexes") or [],
            }
            for p in (manifest.get("props") or [])
        ],
    }


# ---------------------------------------------------------------------------
# 作者的话（paratext）复用映射台已算好的账（2.0.4，用户从链路观测里发现
# 「生成可执行分镜」节点的原文里混着作者感言并拍板）。判据不在本文件重新
# 造：直接读 payload["coverage_ledger"]["paratext"]，那是
# app.production.prep_pack._prep_pack_build_coverage_ledger 的既有产出
# （章节标题的确定性识别 ∪ 模型自报且未被拒的作者话/求票/报字数尾记）。
# ---------------------------------------------------------------------------

#: 占位说明不含任何原文字符——这是本次改造要保证的底线（用户验收标准：
#: provider_calls.request_json 里不能再出现作者感言原文），单纯提示"这里
#: 本来有一段但不是正文"，段号本身仍然保留在 "[段N] ..." 里。
_PARATEXT_PLACEHOLDER_TEXT = (
    "（作者的话，非正文——已按映射台 coverage_ledger.paratext 账略去原文，"
    "不要据此生成画面、台词或节拍）"
)


def _paratext_segment_indexes(payload: dict[str, Any]) -> set[int]:
    """映射台已经算好的 paratext 段号集合，直接读、不重新判定。

    旧契约分集兜底：``coverage_ledger`` 缺失、不是 dict，或者
    ``coverage_ledger.paratext`` 缺失、不是 list 时，返回空集——效果是两处
    source_block 构造函数退化成"每个段号都不是 paratext"，即改造前的全量
    路径。绝不能把"账不存在"误读成"全部段落都是 paratext"（那会让模型看到
    的原文变成清一色占位符，属于比不过滤更差的静默失效）。
    """
    ledger = payload.get("coverage_ledger")
    if not isinstance(ledger, dict):
        return set()
    raw = ledger.get("paratext")
    if not isinstance(raw, list):
        return set()
    result: set[int] = set()
    for item in raw:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _source_block_for_prompt(
    segments: list[SourceSegment], paratext_indexes: set[int]
) -> str:
    """拼出喂给模型的 "[段N] 原文" 文本块，paratext 段落原文替换成占位说明。

    段号绝不重新编号——source_segment_indexes 与 storyboard_source_bindings
    整条链路都靠"段号=index_source_segments 的 1-based 位置"这个假设成立，
    这里只替换某些行的正文内容，行本身（连同段号）照常出现在输出里，模型
    看到的是"段号有跳空内容"而不是"段号本身消失了"。
    """
    lines = []
    for index, segment in enumerate(segments, start=1):
        text = _PARATEXT_PLACEHOLDER_TEXT if index in paratext_indexes else segment.text
        lines.append(f"[段{index}] {text}")
    return "\n".join(lines)


def _paratext_exclusion_rule(paratext_indexes: set[int]) -> str | None:
    """给 rules[] 用的显式禁令文案；没有 paratext 段落时返回 None（不往
    rules[] 里塞一条空列表的噪音）。"""
    if not paratext_indexes:
        return None
    return (
        f"以下原文段号是作者的话/非正文（映射台已判定为 paratext，原文已略去，"
        f"只剩占位说明）：{sorted(paratext_indexes)}——不得把它们编入任何 beat 的 "
        "segment_indexes 或任何 segment 的 source_segment_indexes，也不得据此"
        "编造情节"
    )


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
    return notes


# ---------------------------------------------------------------------------
# 世界书标准外观/场景锚点接入（问题一修复，2026-08-26 真实 EP1 回归：孟浩在
# 10 段里换了三套衣服）。根因不是模型能力，是管道没接上——prep_pack 装配
# asset_manifest.characters[]/scenes[] 时只写 identity_id/display_name/
# portrait_id/scene_reference_id 等身份字段，从不带外观/场景描述本身；模型
# 只能从自己这一段的原文现推，原文没写衣着的段落只能各段各编。世界书里的
# 标准外观/场景锚点（character_portraits.appearance / scene_references.
# scene_canonical）一直都在，只是没被送给模型。
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


def _character_canonical_appearance(conn, portrait_id: str | None) -> str | None:
    """这个已解析 portrait_id 对应的世界书标准外观锚点串。

    ``portrait_id`` 在传入这里之前，已经由映射台
    ``app.production.prep_pack._resolve_portrait_id`` 按本集集号在
    ``character_portraits.ep_start``/``ep_end`` 区间里选定过一次（见该函数
    与 asset_manifest.characters[] 的装配处）——本函数只按这个已选定的 id
    取值，不重新做一遍区间选择，选取逻辑只有一套。
    """
    if not portrait_id:
        return None
    row = conn.execute(
        "SELECT appearance FROM character_portraits WHERE id=?", (portrait_id,),
    ).fetchone()
    if row is None:
        return None
    appearance = str(row["appearance"] or "").strip()
    return appearance or None


def _scene_canonical_description(conn, scene_reference_id: str | None) -> str | None:
    """场景侧同构：``scene_reference_id`` 同样已由
    ``app.production.prep_pack._resolve_scene_reference_id`` 按本集集号选定
    过一次，这里只按这个已选定的 id 取 ``scene_references.scene_canonical``。
    """
    if not scene_reference_id:
        return None
    row = conn.execute(
        "SELECT scene_canonical FROM scene_references WHERE id=?", (scene_reference_id,),
    ).fetchone()
    if row is None:
        return None
    canonical = str(row["scene_canonical"] or "").strip()
    return canonical or None


def _enrich_asset_manifest_canonical_visuals(conn, payload: dict[str, Any]) -> None:
    """原地把世界书标准外观/场景锚点补进 ``payload["asset_manifest"]``。

    在 ``_generate_beat_sheet``/``_generate_all_segment_prompts`` 之前调用一次
    （逐 identity 只查一次，不在逐段循环里重复查询）；``_segment_relevant_
    assets`` 之后按段筛选时拿到的就是同一批已带 appearance/scene_canonical
    的条目对象，不需要再改那个函数。只多一次查询，不建新表也不建新缓存。

    ``functional_extras``（群演/一次性人物）没有 portrait_id、天生没有标准
    外观：这里显式写一条说明而不是留空——留空会被模型读成"没有任何关于外观
    的信息"，导致同一群演在不同段落里各编一套，是问题一的同一种漂移换了个
    没有 portrait_id 的马甲，不是不同的问题。
    """
    manifest = payload.get("asset_manifest") or {}
    for character in manifest.get("characters") or []:
        appearance = _character_canonical_appearance(conn, character.get("portrait_id"))
        character["appearance"] = appearance or _NO_CANONICAL_APPEARANCE_NOTE
    for extra in manifest.get("functional_extras") or []:
        extra["appearance"] = _NO_CANONICAL_APPEARANCE_NOTE
    for scene in manifest.get("scenes") or []:
        canonical = _scene_canonical_description(conn, scene.get("scene_reference_id"))
        scene["scene_canonical"] = canonical or _NO_CANONICAL_SCENE_NOTE


async def _generate_beat_sheet(
    *,
    episode_id: str,
    episode_no: int,
    segments: list[SourceSegment],
    payload: dict[str, Any],
) -> _AiBeatSheetDraft:
    paratext_indexes = _paratext_segment_indexes(payload)
    source_block = _source_block_for_prompt(segments, paratext_indexes)
    rules = [
        "beat_sheet[].segment_indexes 与 segments[].source_segment_indexes 必须引用"
        "下方原文自带的 [段N] 编号，不得虚构或越界",
        "segments[].segment_no 必须从 1 开始连续递增",
        "不是原文每一句话都有剧情意义；无法视觉化的内心独白、纯环境铺垫可以不进入"
        "任何节拍，但已进入节拍的原文不得凭空编造情节",
        "segments[].synopsis 用一句话概括这个段落在讲什么",
        "段落数量由节拍的叙事单元数量决定，不是按原文段数或时长机械平分",
    ]
    paratext_rule = _paratext_exclusion_rule(paratext_indexes)
    if paratext_rule is not None:
        rules.append(paratext_rule)
    task_payload = {
        "task": (
            "通读本章原文，列出节拍表（beat_sheet）：每个节拍是一次情绪或信息的变化，"
            "不是一个句子；合并同质描写，删掉内心独白里无法视觉化的部分。然后把节拍按"
            "叙事单元归入段（一个段要能用一句话概括，例如「他扔掉了理想」「反派现身」），"
            "不是按时长平均切；段与段之间硬切。每段固定 15 秒、内含 3-4 个镜头（这不是"
            "你要填的字段，是下一步的产出约束，这里只需要正确分段）。"
        ),
        "rules": rules,
        "episode_no": episode_no,
        "known_assets": _manifest_brief_for_prompt(payload),
        "source_text_by_segment": source_block,
        "output_schema": _AiBeatSheetDraft.model_json_schema(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(task_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return await model_gateway.chat_structured(
        [
            {
                "role": "system",
                "content": (
                    "你是短剧分镜师。只输出符合 Schema 的一个 JSON 对象，不输出 Markdown"
                    "或解释。"
                ),
            },
            {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False)},
        ],
        model_type=_AiBeatSheetDraft,
        validate=lambda value: _validate_beat_sheet_draft(
            value, total_segments=len(segments)
        ),
        operation_id=f"storyboard_pack_beat_sheet_{episode_id}_{fingerprint}",
        max_tokens=6000,
        format_retry_limit=1,
        semantic_retry_limit=2,
        temperature=0.4,
        call_meta={
            "stage_key": "storyboard_pack_beat_sheet",
            "call_role": "storyboard_beat_sheet",
            "initiator_label": "分镜台节拍表",
            "episode_id": episode_id,
            "contract_version": STORYBOARD_PACK_VERSION,
        },
        repair_context=f"原文共 {len(segments)} 段，段号范围 1..{len(segments)}",
    )


# ---------------------------------------------------------------------------
# 阶段二：逐段提示词
# ---------------------------------------------------------------------------

class _AiDialogueLine(BaseModel):
    speaker_identity_id: str
    line: str
    source_segment_index: int


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


class _AiStoryboardSegmentDraft(BaseModel):
    # 2.0.3：整集批量调用要求模型在每一项里自报 segment_no，代码才能把
    # 数组结果分配回 beat_draft.segments 里对应的那个 _AiSegmentPlan（数组
    # 顺序不作为身份依据——不强制模型必须按顺序输出）。单段场景下这个字段
    # 同样必填（不给默认值），保持所有构造点显式，不留一个只在旧调用路径
    # 里才成立的隐藏假设。
    segment_no: int = Field(ge=1)
    prompt_text: str = Field(min_length=1)
    shot_count: int = Field(ge=MIN_SHOTS_PER_SEGMENT, le=MAX_SHOTS_PER_SEGMENT)
    dialogue: list[_AiDialogueLine] = Field(default_factory=list)
    resources: _AiSegmentResources = Field(default_factory=_AiSegmentResources)
    degraded_capabilities: list[str] = Field(default_factory=list)


class _AiAllSegmentsDraft(BaseModel):
    """阶段二整集批量调用的顶层输出契约：一个 JSON 对象，segments[] 里
    恰好装下本集全部段落各自的 _AiStoryboardSegmentDraft（含 segment_no）。
    """

    segments: list[_AiStoryboardSegmentDraft] = Field(min_length=1)


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
) -> list[str]:
    """Blocking (format-only) checks -- the only things that can make
    ``model_gateway.chat_structured`` retry or fail this segment.

    2026-08-26（用户拍板，第一版分镜提示词不设任何内容门禁）：内容类判断
    （台词说话人是否在场、对白/资源是否能溯源到映射台已知身份）一律不许
    再出现在这个函数里——不是因为它们不该算，是因为算完之后的结论不能是
    "拦截生成"。它们移到 ``_segment_content_advisories``，在模型已经产出
    通过格式校验的 draft 之后再算一遍，结果记进 degraded_capabilities，
    不参与重试/失败判定。这里只留"下一环节会真的用不了"的形状问题：
    prompt_text 是否为空/超限、H3 的三个固定字段名是否存在——写错字段名
    H3 不会报错，只会静默降级成自由文本理解，这条不是内容质量判断，是
    接口语法对不对。
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
    return errors


def _validate_all_segments_draft(
    draft: _AiAllSegmentsDraft,
    *,
    expected_segment_nos: list[int],
    dialect_render_format: str,
) -> list[str]:
    """整集批量调用的格式校验：segment_no 完整性 + 逐项复用单段格式检查。

    与 ``_validate_segment_draft`` 同一条界线（2026-08-26 用户拍板，第一版
    分镜提示词不设内容门禁）：这里只挡"下一环节会真的用不了"的形状问题，
    不判断内容质量。segment_no 完整性是这一层新增的、单段版本不需要的
    检查——旧路径里"这一段对应哪个 _AiSegmentPlan"由代码按调用顺序保证，
    批量调用把这个身份判定交还给模型自报，就必须显式核验它没有漏报/多报/
    重复报。
    """
    errors: list[str] = []
    seen_nos = [item.segment_no for item in draft.segments]
    if sorted(seen_nos) != sorted(expected_segment_nos):
        missing = sorted(set(expected_segment_nos) - set(seen_nos))
        extra = sorted(set(seen_nos) - set(expected_segment_nos))
        duplicated = sorted({no for no in seen_nos if seen_nos.count(no) > 1})
        detail_parts = []
        if missing:
            detail_parts.append(f"缺失 {missing}")
        if extra:
            detail_parts.append(f"包含不存在的 {extra}")
        if duplicated:
            detail_parts.append(f"重复 {duplicated}")
        errors.append(
            f"segments[].segment_no 必须恰好等于 {expected_segment_nos} 各一次："
            + "；".join(detail_parts)
        )
    for item in draft.segments:
        errors.extend(
            f"segment_no={item.segment_no}：{message}"
            for message in _validate_segment_draft(
                item, dialect_render_format=dialect_render_format,
            )
        )
    return errors


def _segment_content_advisories(
    draft: _AiStoryboardSegmentDraft,
    *,
    known_character_ids: set[str],
    known_scene_ids: set[str],
    source_segment_indexes: list[int],
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
    allowed_segments = set(source_segment_indexes)
    segment_character_ids = {c.identity_id for c in draft.resources.characters}
    for index, line in enumerate(draft.dialogue):
        if line.speaker_identity_id not in segment_character_ids:
            advisories.append(
                f"[STORYBOARD_PACK_DIALOGUE_SPEAKER_ABSENT][未拦截] dialogue[{index}] "
                f"的说话人「{line.speaker_identity_id}」不在本段 resources.characters 内，"
                "没有在场证据"
            )
        if line.source_segment_index not in allowed_segments:
            advisories.append(
                f"[STORYBOARD_PACK_DIALOGUE_NO_SOURCE][未拦截] dialogue[{index}]"
                f".source_segment_index={line.source_segment_index} "
                f"不在本段引用的原文段号 {sorted(allowed_segments)} 内"
            )
    # 真实 EP7 回归（2026-08-26）：这两条判断曾经是
    # `if known_character_ids and identity_id not in known_character_ids`（scene
    # 侧同构）。EP7 的 prep_pack 这次没能把「孟浩」解析进 asset_manifest.
    # characters/functional_extras——本集 known_character_ids 因此恰好是空
    # 集，`known_character_ids and ...` 短路成 False，整条判断被跳过：模型
    # 对同一个角色一口气自造了「character:」「char:」「ch:」三种前缀，8 条
    # 引用一条告警都没有，比 EP6 用旧映射包跑、至少还挂出
    # CHARACTER_UNKNOWN 降级标记的年代更静默。空取值域不是「这一项没什么好
    # 查的，跳过」，而是「取值域里什么都不合法，任何非空 identity_id 都必须
    # 判越界」——`set()` 对任何非空字符串的 `not in` 天然为真，去掉真值
    # 判断后空取值域会自动让每一条引用都不合法，不需要为它另开分支。
    for index, character in enumerate(draft.resources.characters):
        if character.identity_id not in known_character_ids:
            advisories.append(
                f"[STORYBOARD_PACK_RESOURCE_CHARACTER_UNKNOWN][未拦截] "
                f"resources.characters[{index}].identity_id=「{character.identity_id}」"
                "不是映射台已知的人物身份，已按纯文字描述处理"
            )
    for index, scene in enumerate(draft.resources.scenes):
        if scene.scene_id not in known_scene_ids:
            advisories.append(
                f"[STORYBOARD_PACK_RESOURCE_SCENE_UNKNOWN][未拦截] "
                f"resources.scenes[{index}].scene_id=「{scene.scene_id}」"
                "不是映射台已知场景，已按纯文字描述处理"
            )
    # 场景资源漏填 vs 本来就没有可引用场景（2.0.5，真实 EP4 回归：9 段里 5 段
    # resources.scenes 是空，起初怀疑是"整集批量产出稀释了注意力"，但核对
    # 这 5 段各自的 relevant_assets.scenes 后发现它们本身就是空列表——映射台
    # 的 asset_manifest.scenes 只覆盖了 EP4 全部 54 段原文里的前 20 段，后
    # 34 段（含这 5 段）从未被登记进任何场景条目，模型没有任何合法 scene_id
    # 可用，留空是唯一诚实的选择，不是模型的错。两种情况必须分开报告，不能
    # 用同一个判断掩盖：relevant_assets.scenes 非空却仍留空，是"可能漏填"，
    # 需要人核对（也可能是这段确实没有独立场景，比如纯特写/纯对话，代码不
    # 替模型做这个判断）；relevant_assets.scenes 本身为空，是"映射台没有为
    # 这段原文范围登记场景资源"，根子在上游，不是这次分镜生成能补的。
    if not draft.resources.scenes:
        if segment_relevant_scene_ids:
            advisories.append(
                "[STORYBOARD_PACK_RESOURCE_SCENE_MISSING][未拦截] 本段 "
                f"relevant_assets.scenes 提供了可引用场景"
                f"（{sorted(segment_relevant_scene_ids)}），但 resources.scenes 为空："
                "可能是本段确实没有独立场景（例如纯特写/纯对话），也可能是遗漏，"
                "需要人工核对"
            )
        else:
            advisories.append(
                "[STORYBOARD_PACK_RESOURCE_SCENE_MANIFEST_GAP][未拦截] 本段原文段号"
                f"{sorted(source_segment_indexes)} 在映射台 asset_manifest.scenes 里"
                "没有任何登记条目覆盖，relevant_assets.scenes 为空，resources.scenes "
                "留空是诚实的选择，不是这次分镜生成的遗漏；如需为本段挂场景参考图，"
                "需要先补齐映射台对这段原文范围的场景发现"
            )
    return advisories


async def _generate_all_segment_prompts(
    *,
    episode_id: str,
    episode_no: int,
    beat_draft: _AiBeatSheetDraft,
    segments: list[SourceSegment],
    payload: dict[str, Any],
    target_video_model: str,
    bible: Bible | None,
) -> dict[int, _AiStoryboardSegmentDraft]:
    """整集一次调用产出全部段落的 prompt_text（2.0.3，替代逐段并行调用）。

    与旧的逐段版本（每段一次独立 chat_structured、asyncio.gather 并行发出）
    相比，这里把本集全部段落的节拍、原文、素材清单一次性交给模型，模型能
    同时看到相邻/跨段的上下文，从而自己保证角色外观、场景转换、镜头语言
    的跨段连续性——旧路径下生成第 N 段那次调用既看不到第 N-1 段写了什么、
    也不知道第 N+1 段会写什么，跨段视野为零，是割裂感的结构性根因（见
    STORYBOARD_PACK_VERSION 2.0.3 changelog 的完整测量数据与风险判断）。

    每段仍然各用一份按 ``_segment_relevant_assets`` 筛选出的 relevant_assets
    （承袭 2.0.1/2.0.2 已验证的过滤逻辑，不重写），额外再给模型一份全集
    source_text_by_segment 与完整 beat_sheet 作为全局上下文，让它在写每一段
    时都能看见其余段落的原文与节拍摘要。返回值按 segment_no 建索引，供调用
    方按 beat_draft.segments 的顺序取回、组装回 StoryboardPackSegment。
    """
    profile, target_model_literal, dialect_instructions = _dialect_for_target_video_model(
        target_video_model
    )
    beats_by_id = {beat.beat_id: beat for beat in beat_draft.beat_sheet}
    paratext_indexes = _paratext_segment_indexes(payload)
    full_source_text = _source_block_for_prompt(segments, paratext_indexes)
    # functional_extras（群演/一次性人物）没有 identity_id，用自己的
    # visual_entity_id 当作 resources.characters[].identity_id 的合法来源
    # （问题三：模型确实会正确引用群演的 visual_entity_id，例如真实 EP1
    # 第10段绿袍男子=entity:fdd28fea634a6cdc；漏掉这一路会让本来合法的引用
    # 被 _segment_content_advisories 误判成"不是映射台已知的人物身份"）。
    known_character_ids = {
        str(c.get("identity_id") or "") for c in (payload.get("asset_manifest") or {}).get("characters") or []
    } | {
        str(e.get("visual_entity_id") or "")
        for e in (payload.get("asset_manifest") or {}).get("functional_extras") or []
    }
    known_scene_ids = {
        str(s.get("scene_id") or "") for s in (payload.get("asset_manifest") or {}).get("scenes") or []
    }
    expected_segment_nos = [plan.segment_no for plan in beat_draft.segments]
    segment_units = [
        {
            "segment_no": plan.segment_no,
            "synopsis": plan.synopsis,
            "beats": [
                {"beat_id": beat_id, "summary": beats_by_id[beat_id].summary}
                for beat_id in plan.beat_ids
                if beat_id in beats_by_id
            ],
            "duration_s": SEGMENT_DURATION_S,
            "source_segment_indexes": plan.source_segment_indexes,
            "relevant_assets": _segment_relevant_assets(payload, plan.source_segment_indexes),
        }
        for plan in beat_draft.segments
    ]
    # 场景漏填 vs 本来无场景的判据（见 _segment_content_advisories 下方
    # 2.0.5 changelog 注释）：复用刚构建的同一份 relevant_assets，不重新算
    # 一遍 _segment_relevant_assets——每个 segment_no 对应它自己在
    # segment_units 里已经算好的候选 scene_id 集合。
    relevant_scene_ids_by_segment_no: dict[int, set[str]] = {
        unit["segment_no"]: {
            str(s.get("scene_id") or "") for s in unit["relevant_assets"]["scenes"]
        }
        for unit in segment_units
    }
    task_payload = {
        "task": (
            "为下面这一整集的每一段原文和节拍各写一整段可直接投喂视频生成模型的"
            "提示词（segments[].prompt_text）。segments[] 里的每一项都必须是完整、"
            "可直接复制使用的一整块文本，不要拆成多个片段或只写关键词——你产出的"
            "每个字符串都会被原样保存并原样提交给视频生成接口，不会再被代码拼接、"
            "改写或补充任何后缀。\n\n"
            "这是本集全部段落的一次性联合产出，不是逐段割裂地各写各的：你能同时"
            "看到全部段落的节拍、原文与素材清单，必须把这份整集视野真正用起来，"
            "让相邻及跨段的画面保持连贯——同一角色的外观/服装在全集所有出现它的"
            "段落里必须完全一致（下面的规则里也有强制要求，不是建议）；一段的"
            "结尾场景与下一段的开头场景如果不同，下一段的第一个镜头要能看出"
            "「从哪来」的交代（例如沿用同一光影/环境线索，或用一个明确的转场镜头"
            "承接），不能让人觉得两段是互不相干、各自拼凑出来的；镜头语言（运镜"
            "选择、镜头开场方式）避免连续多段使用完全相同的模式——同类型运镜可以"
            "出现，但要与相邻段错开，读起来才不会像同一个模板在不断重复。"
        ),
        "rules": [
            f"segments[] 必须恰好包含以下 {len(expected_segment_nos)} 个 "
            f"segment_no，每个出现且只出现一次，不得遗漏也不得新增："
            f"{expected_segment_nos}；数组顺序不重要，身份以 segment_no 为准。",
            "relevant_assets 里每个角色/场景都带一个外观/场景字段（角色和"
            "群演统一叫 appearance；场景叫 scene_canonical）：内容是一段"
            "具体描述时，那就是这个角色/场景在本集的标准锚点，写它时必须"
            "逐字沿用这段描述本身，不得改写、精简、替换或按本段情境调整；"
            "内容是「没有标准外观/场景……」这类说明文字时，才由你自行确定"
            "特征，并让同一角色/场景在本集所有涉及它的段落里保持同一套"
            "自定特征——这条现在必须跨全部 segments[] 一起满足，不是各段"
            "各自满足：同一 identity_id/scene_id 出现在多个段落时，它的外观/"
            "场景描述必须逐字相同。",
            "本段与相邻段（segment_no 前后各一）之间如果场景发生了变化，本段"
            "的第一个镜头必须能看出与上一段场景的空间/时间关系，不能让两段像"
            "是从完全不相关的素材里各剪一段拼起来的；连续相邻的几段也不要"
            "重复使用完全相同的开场运镜/构图模式（例如不要每一段第一镜都是"
            "「推近」），具体用哪种运镜由这一段的剧情动作决定，但要主动参考"
            "相邻段已经用过的开场方式。",
            "dialogue[] 与 prompt_text 两处的台词必须互相覆盖、逐句一致："
            "dialogue[] 列出的每一句台词都必须能在同一段的 prompt_text 里找到"
            "对应原话，prompt_text 里写出的台词原话也必须同时登记进同一段的 "
            "dialogue[]，不得只在一处出现，也不得跨段登记。",
            "每一段 prompt_text 里出场或说话的角色都必须同时列进该段自己的 "
            "resources.characters。resources.characters[].identity_id 的合法取值"
            "只有两处、必须逐字整串复制（含冒号与前缀，一个字符都不能改写、"
            "简化或模仿）：该段 relevant_assets.characters[] 每一项自带的 "
            "identity_id 字段本身，或者该段 relevant_assets.functional_extras[] "
            "每一项自带的 visual_entity_id 字段本身——这两个列表里已经出现的"
            "字符串，就是这一段能够使用的全部合法值，不存在第三种取值来源，也"
            "不允许你按看到的格式风格自己拼一个新字符串（哪怕格式看起来和已有"
            "的很像）。如果这一段确实出场或说话的某个角色，在该段 "
            "relevant_assets.characters 和 relevant_assets.functional_extras 两处"
            "都找不到对应条目——本集素材库还没有收录这个角色——identity_id 就"
            "直接写这个角色在原文里的称谓原文本身，不加冒号、不加任何前缀，"
            "尤其不要模仿已收录角色的 id 写法（那种带前缀的写法是「素材库已"
            "收录」这个事实本身的标记，没有收录记录时自己套用等于冒充一个不"
            "存在的收录状态）。",
            "每一段 prompt_text 里画面实际发生的场景都必须同时列进该段自己的 "
            "resources.scenes——这一条和上面 resources.characters 的要求同等"
            "重要，一次性产出全部段落时不得因为已经填好角色清单就略过场景"
            "清单，每一段都要单独核对。resources.scenes[].scene_id 的合法取值"
            "只有一处、必须逐字整串复制：该段 relevant_assets.scenes[] 每一项"
            "自带的 scene_id 字段本身，不允许自己新造、简化或从其他段的场景"
            "挪用。如果该段 relevant_assets.scenes 本身是空列表——映射台没有"
            "为这段原文范围登记任何场景——resources.scenes 留空是唯一诚实的"
            "选择，不必也不应该勉强套用一个不属于这段的场景 id；但只要 "
            "relevant_assets.scenes 非空且本段画面确实发生在其中某个场景，"
            "就必须把对应 scene_id 列进 resources.scenes，不得因为篇幅或注意力"
            "被其他字段占用而省略。",
            *([_paratext_exclusion_rule(paratext_indexes)] if paratext_indexes else []),
        ],
        "shot_count_range": [MIN_SHOTS_PER_SEGMENT, MAX_SHOTS_PER_SEGMENT],
        "beat_sheet": [
            {
                "beat_id": beat.beat_id,
                "summary": beat.summary,
                "segment_indexes": list(beat.segment_indexes),
            }
            for beat in beat_draft.beat_sheet
        ],
        "source_text_by_segment": full_source_text,
        "segments": segment_units,
        "visual_style": (
            bible.world.visual_style_canonical
            if bible is not None and bible.world is not None
            else ""
        ),
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
            "segments": "数组，长度必须等于段数，每一项对应一个 segment_no",
            "segments[].segment_no": f"必须恰好是 {expected_segment_nos} 之一，且互不重复",
            "segments[].prompt_text": "完整可复制的提示词整块文本，按上面的方言约束写",
            "segments[].shot_count": f"{MIN_SHOTS_PER_SEGMENT}-{MAX_SHOTS_PER_SEGMENT} 之间的整数，须与该段 prompt_text 里实际写的镜头数一致",
            "segments[].dialogue": (
                "该段实际出现的台词（可以是原文对话的压缩/改写，不要求逐字，但不得偏离"
                "该段剧情）；每条必须给 speaker_identity_id（引用该段 relevant_assets."
                "characters 的 identity_id）与 source_segment_index（这句话对应原文的"
                "哪一段，必须在该段 source_segment_indexes 范围内）"
            ),
            "segments[].resources": "该段实际用到的人物/场景/道具，identity_id/scene_id 必须来自该段 relevant_assets；素材库没有对应图的（scene_reference_id 或 portrait_id 为空）如实留空，不得编造",
            "segments[].degraded_capabilities": "该段因模型能力缺失而做的降级处理清单（例如 Seedance 侧的屏上文字改「无字」+ 后期合成说明）；没有降级则留空数组，不得留空字符串占位",
        },
        "output_schema": _AiAllSegmentsDraft.model_json_schema(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(task_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    batch_draft = await model_gateway.chat_structured(
        [
            {
                "role": "system",
                "content": (
                    "你是短剧分镜师和视频生成提示词撰写者。"
                    f"当前目标模型是 {profile.model_family}。"
                    "只输出符合 Schema 的一个 JSON 对象，不输出 Markdown 或解释；"
                    "每段 prompt_text 字段内部可以换行，但整体是一个字符串。"
                ),
            },
            {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False)},
        ],
        model_type=_AiAllSegmentsDraft,
        validate=lambda value: _validate_all_segments_draft(
            value,
            expected_segment_nos=expected_segment_nos,
            dialect_render_format=profile.render_format,
        ),
        operation_id=f"storyboard_pack_segments_{episode_id}_{fingerprint}",
        max_tokens=max(
            SEGMENT_BATCH_TOKENS_FLOOR,
            SEGMENT_BATCH_TOKENS_PER_SEGMENT * len(expected_segment_nos),
        ),
        format_retry_limit=1,
        semantic_retry_limit=2,
        temperature=0.6,
        call_meta={
            "stage_key": "storyboard_pack_segment",
            "call_role": "storyboard_pack_segment_batch",
            "initiator_label": "分镜台整集提示词",
            "episode_id": episode_id,
            "segment_count": len(expected_segment_nos),
            "target_video_model": target_video_model,
            "contract_version": STORYBOARD_PACK_VERSION,
        },
        repair_context=(
            f"本集共 {len(expected_segment_nos)} 段，segments[].segment_no 必须"
            f"恰好覆盖 {expected_segment_nos} 各一次"
        ),
    )
    by_segment_no = {item.segment_no: item for item in batch_draft.segments}
    result: dict[int, _AiStoryboardSegmentDraft] = {}
    for plan in beat_draft.segments:
        draft = by_segment_no[plan.segment_no]
        advisories = _segment_content_advisories(
            draft,
            known_character_ids=known_character_ids,
            known_scene_ids=known_scene_ids,
            source_segment_indexes=plan.source_segment_indexes,
            segment_relevant_scene_ids=relevant_scene_ids_by_segment_no.get(plan.segment_no, set()),
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


class StoryboardPack(BaseModel):
    storyboard_version: str = STORYBOARD_PACK_VERSION
    episode_no: int
    target_model: str
    beat_sheet: list[StoryboardPackBeat]
    segments: list[StoryboardPackSegment]


def _load_indexed_source_segments(conn, ep) -> list[SourceSegment]:
    """Segment the chapter text exactly the way app.production.prep_pack does.

    Both call sites in prep_pack.py call ``index_source_segments(source_text)``
    with no override (default max_chars=900); this reuses the identical
    function so ``segment_index`` here means the same 1-based position that
    asset_manifest/appellation_map already anchor on.
    """
    source_text = _episode_source_text(conn, ep)
    return index_source_segments(source_text)


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
    _enrich_asset_manifest_canonical_visuals(conn, payload)

    bible: Bible | None = None
    project = conn.execute(
        "SELECT * FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    if project and project["bible_json"]:
        from app.portraits import bible_for_episode

        bible = bible_for_episode(
            ep["project_id"], Bible.model_validate(json.loads(project["bible_json"])), episode_no,
        )

    beat_draft = await _generate_beat_sheet(
        episode_id=episode_id, episode_no=episode_no, segments=segments, payload=payload,
    )
    # 防御性兜底（见 STORYBOARD_PACK_VERSION 2.0.4 changelog）：stage 1 的
    # rules[] 只是提示词层面的禁令，不是校验闸门，这里在通过格式校验之后、
    # 进入 phase 2 之前用同一份 paratext 账把漏网的引用滤掉。
    paratext_indexes = _paratext_segment_indexes(payload)
    paratext_strip_notes = _strip_paratext_from_beat_draft(beat_draft, paratext_indexes)

    # 2.0.3：全部段落的 prompt_text 一次调用产出（见 _generate_all_segment_
    # prompts 文档与 STORYBOARD_PACK_VERSION 的 2.0.3 changelog），不再是每段
    # 一次独立调用、asyncio.gather 并行发出——旧路径下并行意味着生成第 N 段
    # 的那次调用互相看不到彼此，是跨段割裂感的结构性根因。
    segment_drafts = await _generate_all_segment_prompts(
        episode_id=episode_id,
        episode_no=episode_no,
        beat_draft=beat_draft,
        segments=segments,
        payload=payload,
        target_video_model=target_video_model,
        bible=bible,
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
            # paratext_strip_notes 极罕见地非空时（见 _strip_paratext_from_beat_
            # draft 文档），留痕给每一段而不是挑一段单独记——这个信号描述的是
            # 整份 beat_draft 里出现的引用异常，不是某一段独有的问题。
            degraded_capabilities=[
                *segment_drafts[plan.segment_no].degraded_capabilities,
                *paratext_strip_notes,
            ],
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

    One 15s segment = one shots row (user-frozen decision): the 3-4 internal
    shot cuts live inside prompt_text as free text, never split into separate
    rows. shot_size/camera_move/camera_angle/first_frame_desc/last_frame_desc
    are left empty -- they describe a single continuous camera setup, a
    granularity this row no longer has; the marker
    ``prompt_contract_version=storyboard_pack/2.0.0`` is how every consumer
    (app/continuity.py, app/validators.py, app/domain/video_ops.py) knows to
    stop treating those columns as authoritative for this row instead of
    silently failing or silently passing on empty values.
    """
    from app.domain.storyboard_ops import _assert_storyboard_write_authorized
    from app.storyboard_workspace import chapter_sources, persist_source_binding

    _assert_storyboard_write_authorized(conn, episode_id, None)
    if segments is None:
        segments = _load_indexed_source_segments(conn, ep)
    full_source_text = _episode_source_text(conn, ep)
    authorized_sources = chapter_sources(episode_id, conn=conn)
    conn.execute("DELETE FROM shots WHERE episode_id=?", (episode_id,))
    beats_by_id = {beat.beat_id: beat for beat in pack.beat_sheet}
    shot_ids: list[str] = []
    for segment in pack.segments:
        character_ids = [
            str(c.get("identity_id") or "") for c in (segment.resources.get("characters") or [])
        ]
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
        shot_id = new_id("shot")
        shot_uid = new_id("shotuid")
        segment_record = segment.model_dump(mode="json")
        # Single source of truth: the model's own self-declared segment.beat_ids
        # (_AiSegmentPlan.beat_ids, carried through StoryboardPackSegment) --
        # the same list that built this segment's own prompt (see
        # _generate_all_segment_prompts's segment_units[].beats). _validate_beat_sheet_draft
        # already rejects any beat_id that doesn't exist in pack.beat_sheet, so
        # the lookup below cannot silently drop a real beat; the ``in
        # beats_by_id`` guard is defense in depth, not a coverage gap.
        # Previously this was re-derived from
        # ``set(beat.segment_indexes) & set(segment.source_segment_indexes)`` --
        # a different field (segment_indexes) standing in for beat_ids, which
        # could disagree with what the model actually declared/was prompted
        # with at the edges. See the "拿一个维度的代理担保另一个维度" note.
        matched_beats = [
            beats_by_id[beat_id] for beat_id in segment.beat_ids if beat_id in beats_by_id
        ]
        # ``beat_ids`` (bare id list) is the pre-existing key frontend/api.ts and
        # BoardPage.tsx already read (StoryboardPackSegment.beat_ids) -- kept
        # unchanged for that consumer plus any historical row shape. ``beats``
        # is the new self-contained field: each shot dict must be renderable on
        # its own (docs/STORYBOARD_PROMPT_IR_DESIGN.md's beat_sheet exists for
        # 留痕 -- a bare id conveys nothing without the summary next to it), so
        # this carries the frozen contract's own per-beat shape
        # (beat_id/summary/segment_indexes, no invented field names) rather
        # than making the frontend join against the episode-level beat_sheet.
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
                delivery="spoken_dialogue",
            ).model_dump()
            for line in segment.dialogue
        ]
        # continuity_mode/transition/first_frame_desc/last_frame_desc describe a
        # single continuous camera setup and are not meaningful once one row
        # covers 3-4 internal cuts; left at their non-committal defaults rather
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
                "", "", "", "",
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
    segments' prompt_text) is a single self-contained batch model call as of
    2.0.3 (retried internally by model_gateway.chat_structured's own
    format/semantic retry budget across the whole batch, not per segment), so
    there is no equivalent multi-shot repair loop to run -- a failure there
    fails the whole episode's generation in one shot, not one segment (see
    STORYBOARD_PACK_VERSION's 2.0.3 changelog for the accepted trade-off and
    the measurement behind it). On success this
    reuses the exact same completion contract the legacy path uses for its
    non-narrative-authority branch (app.storyboard_supervisor.py's own
    ``else: _finalize_storyboard_evidence(episode_id, evaluation.board)`` /
    ``cp.phase = "SUCCEEDED"`` tail) so publish/certificate/evidence and the
    confirmation gate see the same shape of "done" they already know how to
    handle.
    """
    from app.storyboard_supervisor import SupervisorCheckpoint, save_checkpoint
    from app.domain.storyboard_ops import (
        _board_from_shot_rows,
        _finalize_storyboard_evidence,
    )

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
