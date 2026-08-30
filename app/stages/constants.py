"""剧本蓝图生成与 IR 保真阶段共用的系统提示词前缀、token/重试预算常量。"""
from __future__ import annotations



from app.narrative_blueprint import (
    BLUEPRINT_PROMPT_VERSION,
)


SYSTEM_PREFIX = (
    "你是专业的竖屏漫剧（动态漫画短剧）编剧与分镜师。\n"
    "你的观众看的是 AI 生成视频，不是摄影机实拍；请为模型能力写作，不为文学完整度炫技。\n"
    "输出规则：只输出一个 JSON 对象，无 Markdown 围栏，无解释文字；字符串内部的英文双引号必须写成 JSON 转义形式。\n"
    "所有内容使用简体中文。"
)

SCREENPLAY_BASELINE_PROMPT_VERSION = "screenplay-compact-ir-5.5.1"
SCREENPLAY_BLUEPRINT_PROMPT_VERSION = BLUEPRINT_PROMPT_VERSION
BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION = "blueprint-semantic-review.v5"
# IR shape drift is normalized locally. A second AgentLoop iteration would
# resend the entire chapter and candidate for a few field-level corrections,
# erasing the latency/token savings of the compact contract.
SCREENPLAY_STRUCTURAL_BOOTSTRAP_ITERATIONS = 1
SCREENPLAY_IR_MIN_TOKENS = 20480
SCREENPLAY_IR_MAX_TOKENS = 36864

BLUEPRINT_SHARD_MIN_TOKENS = 6144
BLUEPRINT_SHARD_MAX_TOKENS = 16384
BLUEPRINT_SHARD_MAX_ATTEMPTS = 3
# A transport stall authors nothing, so it is not a semantic attempt and gets
# its own bounded budget.  Production: shard 13 of ep_a0e90058f83c spent
# attempts 1 and 2 on invalid candidates, then attempt 3 stalled at 0 received
# characters after 182.8s -- the episode died on a call that never delivered a
# single byte, with no candidate to show for it.
BLUEPRINT_SHARD_MAX_STALL_RETRIES = 2
BLUEPRINT_REVIEW_FORMAT_RETRY_LIMIT = 1
# A full (non-targeted) review of a converged blueprint can carry a dozen+
# must-fix issues; 8192 output tokens is exactly the truncation cliff observed
# in production (finish_reason=length -> OUTPUT_TRUNCATED, replayed forever).
# The text model supports up to 32768 output tokens.
BLUEPRINT_REVIEW_MAX_TOKENS = 16384
# Extra attempts for a single independent semantic reviewer when the provider
# never received the request (delivery_state == not_sent, replay_safe). A
# transient not-sent failure of one reviewer must not discard the whole
# multi-round blueprint generation; genuinely-unknown outcomes still fail closed.
BLUEPRINT_REVIEW_PROVIDER_RETRY_LIMIT = 1
# Consensus needs two independent samples.  When exactly one reviewer never
# delivered an opinion at all, draw ONE more sample under this number instead of
# discarding a validated blueprint that cost ~30 minutes to build.  It is a new
# deterministic operation, never a replay of the unresolved call, and it is
# bounded to one per review round.
BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE = 3
# Runaway-generation breakers.  These three values are *floors*: a blueprint is
# produced leaf-by-leaf, so the honest cost of one activation scales with the
# planned leaf count, not with a constant.  ``_BlueprintGenerationBudget``
# raises all three from the deterministic leaf plan (see ``adopt_shard_plan``)
# so the breakers only ever fire on genuine runaway, never on the nominal path
# of a long episode.
BLUEPRINT_GENERATION_MAX_PROVIDER_CALLS = 32
BLUEPRINT_GENERATION_MAX_OUTPUT_TOKENS = 131072
BLUEPRINT_GENERATION_MAX_WALL_SECONDS = 1800.0
# 与身份合同同源的标定：会先思考再作答的模型，其 reasoning token 与正文共用
# completion 预算，所以"够写下补丁"并不等于"够跑完这次调用"。
IR_FIDELITY_PATCH_MAX_TOKENS = 16384
# Per planned leaf: one full-shard call plus at most one typed ownership
# repair call (``BLUEPRINT_SHARD_MAX_ATTEMPTS`` allows a third attempt, which
# the shared headroom below absorbs together with dynamic splits and the
# patch/review stages).
BLUEPRINT_LEAF_PROVIDER_CALLS = 2
BLUEPRINT_LEAF_CALL_HEADROOM = 8
BLUEPRINT_GENERATION_MAX_SPLIT_DEPTH = 4
# 场次语义门禁耗尽修复轮次后仍未收口，往往不是文案问题，而是**蓝图把这个 source unit
# 分错了类**（例如把一句人物内在特质标成纯环境）：环境 slot 不许写人物内容，源文却整句
# 都是人物，两条路都通不过 —— 合同可证明无解，而场次层唯一的补救手段（重写文案）
# 修不好一个分类错误。生产 EP2 的 SS002 因此累计打了 254 次 provider 调用、
# 整片重写 8 次，每一轮双审共识都给出完全相同的判定。
#
# 这一层的正确动作是把证据交回**拥有该决定的那一层**：带着「哪些 unit 下游无解、
# 审查员原话是什么」重建一次蓝图。它不是盲目重摇——反馈会进入分片的 source payload，
# 既改变 source_hash（使已缓存分片不被复用），也作为显式约束进入提示词。
# 严格限一次：真正无解的输入不会因为多试几次而变得有解。
SCREENPLAY_BLUEPRINT_SEMANTIC_REBUILD_LIMIT = 1
