"""小说体说话人归属：最近含名分句里的名字不在分句开头时（可能是宾语），同句更早分句另有候选人
就留空交给模型——2026-09-27《顾念长安》第 4 集整集分镜因「打电话给顾屿，急声说」被误判拦死。"""
from app.production.storyboard_dialogue_attribution import attribute_prose_speaker

NAMES = ["温念", "顾屿", "陆一舟", "张飞", "刘备", "林姐", "小满"]


def _speaker(text: str) -> str:
    start = text.index("“")
    end = text.index("”", start) + 1
    return attribute_prose_speaker(text, start, end, NAMES)


def test_object_name_in_nearest_clause_with_subject_earlier_in_sentence_is_left_to_model() -> None:
    text = "陆一舟见她许久没回消息，又听说这场雨来得又急又大，赶紧打电话给顾屿，急声说：“温念好像出门往你这边来了，快去找找她！”"
    assert _speaker(text) == ""


def test_addressee_after_preposition_is_not_taken_as_speaker() -> None:
    assert _speaker("温念点点头，对顾屿说：“谢谢你收留我。”") == ""


def test_name_at_clause_start_still_wins() -> None:
    assert _speaker("刘备惊问张飞，张飞道：“俺也不知。”") == "张飞"
    assert _speaker("顾屿低声说：“温念，好久不见。”") == "顾屿"


def test_single_candidate_in_sentence_is_kept_even_if_not_at_clause_start() -> None:
    assert _speaker("雨停了。这时顾屿说：“走吧。”") == "顾屿"


def test_previous_sentence_candidate_does_not_count_as_conflict() -> None:
    assert _speaker("温念愣住了。只听顾屿说：“是我求了阿姨三个月。”") == "顾屿"
