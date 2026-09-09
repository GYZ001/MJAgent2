"""分镜首轮与修复轮共用原文、身份及台词权威，明确两种段号的编号域。"""
import json


def storyboard_repair_context(payload: dict) -> str:
    """只省略重复 Schema；修复需要的原文和约束不随重试丢失。"""
    context = {key: value for key, value in payload.items() if key != "output_schema"}
    context["source_numbering"] = (
        "segment_no 是视频分镜段号；source_segment_index 是原文 [段N] 编号。"
        "原文编号沿用 source_text_by_segment 和 required_dialogue，不用分镜段号替换。"
        "必保引文的 text、quote_id、来源段号和偏移逐字取自 required_dialogue；"
        "原文中的拼音、异体字和错别字也属于当前引用，不自行校正。"
        "叙述者的 speaker_identity_id 固定写为旁白，delivery_kind=narration，"
        "delivery=offscreen_voice，resources.characters 只列人物。"
    )
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))


def known_character_identities(payload: dict) -> list[dict]:
    """全局正名资格与本段可用参考图分开；不把目录人物自动加入画面。"""
    return [
        {key: item.get(key) for key in ("identity_id", "display_name", "aliases")}
        for item in (payload.get("asset_manifest") or {}).get("characters") or []
    ]
