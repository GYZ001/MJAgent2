"""身份合同在分镜模型调用边界的装配与验证，不参与数据库写入。"""
from app.production.storyboard_identity_contract import (
    canonical_segment_identities, identity_contract_errors, registered_subject_errors, stamp_identity_contract,
)
from app.production.storyboard_speech_render import (
    attach_quote_provenance, render_segment_speech, speech_template_errors,
)
from app.production.storyboard_identity_validation import final_identity_prompt_errors, quote_provenance_errors

IDENTITY_GENERATION_RULES = [
    "每个出场或发声主体单独列入 resources.characters；visibility=visible 表示实际出镜，voice_only 表示本段仅有声音。内心独白的人也可能可见，按实际画面填写。旁白只有声音，不列入人物资源。",
    "subject_kind：known_character_identities 列出本集映射已确认角色正名及 identity_id；原文指向该角色本人时用 character，参考图为空也保持角色身份；独立无名人物用 extra；复数人群用 crowd。群演可以使用 relevant_assets.functional_extras[].visual_entity_id；未收录的无名人保留原文称谓并独立描述。身份由原文关系决定，不由外观相似或可用角色卡决定。目录存在不代表本段出镜，可见性按原文和实际画面填写。",
    "每句 dialogue 有唯一 utterance_id（U01、U02 等）。delivery_kind 区分 spoken_dialogue（画内开口）、offscreen_dialogue（人物画外对白）、inner_monologue（人物内心独白）、narration（叙述者旁白）。后三类 delivery=offscreen_voice，画内对白 delivery=spoken_dialogue。",
    "在 prompt_text 的准确发声时机写 {{speech:U01}} 这样的占位符，每句一次；台词原话、声音归属仅写入 dialogue[]，系统会按合同展开为实际声道标签和原话。镜头动作仍由你完整撰写。",
    "必保台词清单的原话必须逐字保留；speaker_identity_id、excluded_speaker_identity_ids、delivery_kind、quote_id 和来源偏移是原文证据：明确归属须保持，听者不能充当发声者。没有明确归属时使用有证据的无名人物，并填写 attribution_evidence；旁白只用于原文叙述者发声。source_quote_id 引用对应 quote_id。",
    "resources.characters[].display_name 使用输入角色正名或群演 label；有图的可见角色用 @完整名字 后接空格或标点，群演用独立描述。仅有声音的角色使用 speech 占位符发声，无需 @人物图片。",
    "叙述者的 speaker_identity_id 固定填写旁白，delivery_kind=narration，delivery=offscreen_voice，resources.characters 只列人物。source_segment_index 沿用原文 [段N] 编号，与视频 segment_no 分开；必保台词包括原文拼音、异体字、错别字均照录，source_quote_id 逐字取自对应 quote_id。",
]


def generated_identity_errors(draft, *, payload: dict, source_indexes: list[int], required_dialogue: list[dict], dialect: str = "") -> list[str]:
    segment = draft.model_dump(mode="json")
    segment.update(source_segment_indexes=source_indexes, required_dialogue=required_dialogue)
    normalized = canonical_segment_identities(segment, payload)
    errors = [*identity_contract_errors(segment), *identity_contract_errors(normalized, require_explicit=True),
              *registered_subject_errors(normalized, payload),
              *speech_template_errors(normalized, require_tokens=True), *quote_provenance_errors(normalized)]
    if not errors:
        render_segment_speech(normalized, dialect=dialect)
        errors.extend(final_identity_prompt_errors(normalized))
    return list(dict.fromkeys(errors))


def finalize_generated_identity(draft, *, payload: dict, source_indexes: list[int], required_dialogue: list[dict], dialect: str):
    """完整候选先验证后展开；不存在只改台词数据、不改提示词的半次修补。"""
    errors = generated_identity_errors(draft, payload=payload, source_indexes=source_indexes, required_dialogue=required_dialogue, dialect=dialect)
    if errors:
        raise ValueError("；".join(errors))
    segment = draft.model_dump(mode="json")
    segment.update(source_segment_indexes=source_indexes, required_dialogue=required_dialogue)
    normalized = canonical_segment_identities(segment, payload)
    attach_quote_provenance(normalized)
    render_segment_speech(normalized, dialect=dialect)
    stamp_identity_contract(normalized)
    return type(draft).model_validate(normalized)
