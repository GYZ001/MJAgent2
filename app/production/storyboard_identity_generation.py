"""身份合同在分镜模型调用边界的装配与验证，不参与数据库写入。"""
import logging

from app.production.storyboard_identity_contract import (
    canonical_segment_identities, identity_contract_errors, registered_subject_errors, stamp_identity_contract,
)
from app.production.storyboard_speech_render import (
    attach_quote_provenance, render_segment_speech, speech_template_errors,
)
from app.production.storyboard_identity_validation import final_identity_prompt_errors, quote_provenance_errors
from app.production.storyboard_reference_tag_repair import repair_segment_reference_tags

log = logging.getLogger(__name__)

IDENTITY_GENERATION_RULES = [
    "每个出场或发声主体单独列入 resources.characters；visibility=visible 表示实际出镜，voice_only 表示本段仅有声音。内心独白的人也可能可见，按实际画面填写。旁白只有声音，不列入人物资源。",
    "subject_kind：known_character_identities 列出本集映射已确认角色正名及 identity_id；原文指向该角色本人时用 character，参考图为空也保持角色身份；独立无名人物用 extra；复数人群用 crowd。群演可以使用 relevant_assets.functional_extras[].visual_entity_id；未收录的无名人保留原文称谓并独立描述。身份由原文关系决定，不由外观相似或可用角色卡决定。目录存在不代表本段出镜，可见性按原文和实际画面填写。",
    "每句 dialogue 有唯一 utterance_id（U01、U02 等）。delivery_kind 区分 spoken_dialogue（画内开口）、offscreen_dialogue（人物画外对白）、inner_monologue（人物内心独白）、narration（叙述者旁白）。后三类 delivery=offscreen_voice，画内对白 delivery=spoken_dialogue。",
    "在 prompt_text 的准确发声时机写 {{speech:U01}} 这样的占位符，每句一次；台词原话、声音归属仅写入 dialogue[]，系统会按合同展开为实际声道标签和原话。镜头动作仍由你完整撰写。",
    "必保台词清单的原话必须逐字保留；speaker_identity_id、excluded_speaker_identity_ids、delivery_kind、quote_id 和来源偏移是原文证据：明确归属须保持，听者不能充当发声者。没有明确归属时使用有证据的无名人物，并填写 attribution_evidence；旁白只用于原文叙述者发声。source_quote_id 引用对应 quote_id。",
    "resources.characters[].display_name 使用输入角色正名或群演 label；有图的可见角色用 @完整名字 后接空格或标点，群演用独立描述。仅有声音的角色使用 speech 占位符发声，无需 @人物图片。",
    "叙述者的 speaker_identity_id 固定填写旁白，delivery_kind=narration，delivery=offscreen_voice，resources.characters 只列人物。source_segment_index 沿用原文 [段N] 编号，与视频 segment_no 分开；必保台词包括原文拼音、异体字、错别字均照录，source_quote_id 逐字取自对应 quote_id。",
    "闪回/回忆画面里出现的人物，若外观明显不是这个角色当前的年龄或形态（童年、少年、年迈等），写进 resources.flashback_figures（label 用称呼如「六岁的顾屿」，description 写清年龄区间、脸型、发型、服装颜色材质等至少三项可视觉验证特征），不要列入 resources.characters、也不要用 @人名指他——@ 的意思是「这一镜用这个角色当前的定妆照」，闪回人物没有这张图可用，镜头正文直接用这个称呼和 description 里的特征描述这个人。闪回画面里如果人物就是角色卡当前的年龄和形态，仍按普通角色处理，正常列入 resources.characters、可以用 @。",
    "resources.characters[].wardrobe_matches_default：本段这个人物的穿着是否就是人物谱定妆照默认造型（即第一次出场时的那套服装）。本段规则如果已经按全集服装表告知了某个人物该填 yes 还是 no，直接照填，不要自己重新判断；没有被告知时，按本段原文与 continuity_memo 自行判断——确实没有任何换装/脱下/新增配饰证据填 yes，有证据表明穿着不同填 no，拿不准填 unsure。这个字段决定生成时送全身照还是头像照，填 unsure 时会保守按头像照处理（只锁长相，服装以本段文字为准），不会因为漏填而让画面服装被参考图压过正文。",
]


def generated_identity_errors(
    draft, *, payload: dict, source_indexes: list[int], required_dialogue: list[dict],
    dialect: str = "", narrator_voice_character: str = "",
) -> list[str]:
    segment = draft.model_dump(mode="json")
    segment.update(source_segment_indexes=source_indexes, required_dialogue=required_dialogue)
    normalized = canonical_segment_identities(segment, payload)
    # 模型产出进入校验之前的确定性修补（模型提名、代码核验）：@X 连写紧随镜头描述时
    # 按最长合法名前缀补一个空格。draft 是 model_gateway.chat_structured 校验通过后
    # 原样返回、再传给 finalize_generated_identity 的同一个对象，这里就地写回
    # draft.prompt_text 能让后续 finalize 重新 model_dump 时也拿到修补后的文本，
    # 不产生「校验用修补后的副本、落盘用未修补的原文」的半次修补。
    fixed = repair_segment_reference_tags(normalized)
    if fixed:
        draft.prompt_text = normalized["prompt_text"]
        if normalized.get("speech_template"):
            draft.speech_template = normalized["speech_template"]
        log.info("[STORYBOARD_REFERENCE_TAG_REPAIR] @ 引用与紧随文字之间已补空格：%s", "、".join(fixed))
    errors = [*identity_contract_errors(segment), *identity_contract_errors(normalized, require_explicit=True),
              *registered_subject_errors(normalized, payload),
              *speech_template_errors(normalized, require_tokens=True), *quote_provenance_errors(normalized)]
    if not errors:
        render_segment_speech(normalized, dialect=dialect, narrator_voice_character=narrator_voice_character)
        errors.extend(final_identity_prompt_errors(normalized))
    return list(dict.fromkeys(errors))


def finalize_generated_identity(
    draft, *, payload: dict, source_indexes: list[int], required_dialogue: list[dict],
    dialect: str, narrator_voice_character: str = "",
):
    """完整候选先验证后展开；不存在只改台词数据、不改提示词的半次修补。

    ``narrator_voice_character``：项目设置的旁白固定音色角色正名，见
    ``app.production.storyboard_speech_render.rendered_utterance`` 文档；空串
    （默认）＝改动前行为逐字不变。本函数只有一条生产调用链——``app.production.
    storyboard_pack._generate_all_segment_prompts``（生成与「仅重新编写本段」
    重生成共用），调用方永远现查项目设置显式传入，默认值只服务没有项目上下文
    的直接调用（如测试）。

    2026-09-29 更正：这条 docstring 曾说「对话修订、身份工作台等编辑路径尚未
    接入该设置、继续用旧行为」——不准确，那两条路径根本不经过本函数，各自
    直接调 ``render_segment_speech``；它们当时确实也没接入项目设置（读的是段落
    里生成时刻留下的旧字段，项目设置改过之后编辑会把旁白标签冻结在旧值上，
    真实回归 proj_ca86b15ab7d7 EP1），但修法在各自模块（``app.domain.
    storyboard_ops.identity_workspace.prepare_identity_candidate``、
    ``app.production.storyboard_dialogue_revision.revise_segment_dialogue``），
    与本函数无关，这里不再重复描述其行为。
    """
    errors = generated_identity_errors(
        draft, payload=payload, source_indexes=source_indexes, required_dialogue=required_dialogue,
        dialect=dialect, narrator_voice_character=narrator_voice_character,
    )
    if errors:
        raise ValueError("；".join(errors))
    segment = draft.model_dump(mode="json")
    segment.update(source_segment_indexes=source_indexes, required_dialogue=required_dialogue)
    normalized = canonical_segment_identities(segment, payload)
    attach_quote_provenance(normalized)
    render_segment_speech(normalized, dialect=dialect, narrator_voice_character=narrator_voice_character)
    stamp_identity_contract(normalized)
    return type(draft).model_validate(normalized)
