"""容量拆分续段的画面去重（app.production.storyboard_staging_repeat）。

2026-09-13 我欲封天第 1 集重跑逐帧看成片：镜 1–6 是同一个男孩坐在同一块石头上抱着
同一个葫芦整整 90 秒。这 6 段是按口播容量拆出来的续段，机位词都换过（固定/推近/拉远/
横摇/仰拍），所以既有的机位防重复被满足了，但调度一模一样。这里的用例直接取自那次
产物与第 3 集镜 9–13（三人有动作、不该拦）的真实提示词片段。
"""
from __future__ import annotations

from app.production.storyboard_capacity_normalize import CAPACITY_SPLIT_MARKER
from types import SimpleNamespace

from app.production.storyboard_staging_repeat import (
    canonical_phrases,
    chain_prompt_texts,
    is_continuation,
    repeated_staging,
    repeated_staging_errors,
    staging_continuation_rule,
    sub_shots,
)

STYLE = "国漫3D动画电影质感的虚构室外大青山山顶场景，黄昏时分，带有精致统一的光影与统一电影画面风格，为非真人数字渲染的自然山地环境，"
MENGHAO = "@孟浩 十四五岁少年身形，黑发束起，身着灰布外宗修士袍，"
GOURD = "国漫3D动画电影质感的天然葫芦，短颈圆腹造型，表面带有自然浅褐色纹理，颈部系有浅棕色绳结，整体尺寸约成人手掌大小，"

# 第 1 集镜 2 与镜 3（续段）——真实形态的缩写：三拍完全一样，只换了机位词
EP1_SHOT2 = (
    f"镜头1：推近 {MENGHAO}坐在大青山山顶的岩石上，眉头皱起，嘴角勾起一抹自嘲的弧度，张嘴说话\n"
    f"镜头2：特写 @孟浩 的手，手指轻轻摩挲着身侧{GOURD}光影精致\n"
    f"镜头3：拉远 {STYLE}@孟浩 坐在山顶边缘，望向远方沉落的夕阳\n"
    "全片贯穿：环境音是山风。"
)
EP1_SHOT3 = (
    f"镜头1：固定 {STYLE}暖橙色霞光铺满整个山巅，{MENGHAO}坐在大青山山顶的岩石上垂着头，眉头皱起\n"
    f"镜头2：特写 @孟浩 身侧的{GOURD}被手指轻轻摩挲着，光影精致写实\n"
    f"镜头3：拉远 {STYLE}@孟浩 坐在山顶边缘，望向远方沉落的夕阳，身影单薄\n"
    "全片贯穿：环境音是山风。"
)
# 第 3 集镜 10 与镜 11（续段）——三个人物、各有新动作
EP3_SHOT10 = (
    "镜头1：固定中景，@孟浩 站在靠山宗北区杂役处室外的灰色大石旁，手中握着扁平椭圆片状玉简\n"
    "镜头2：固定近景，马脸青年盘膝坐在灰色大石上，神色平淡，嘴唇开合说出：“凝气入体，融散全身”\n"
    "镜头3：固定中景，@小胖子 从远处屋舍方向跑来，气喘吁吁，嘴唇开合喊出：“怎么样，怎么样了？”\n"
    "全片贯穿：环境音是风声。"
)
EP3_SHOT11 = (
    "镜头1：固定中景，@孟浩 站在灰色大石旁，手中握着玉简，嘴唇开合说出：“还无法散及全身”\n"
    "镜头2：固定近景，@马脸青年 盘膝坐在灰色大石上，看向孟浩，嘴唇开合说出：“我问的是那只鸡怎么样了。”\n"
    "镜头3：固定近景，@小胖子 站在远处屋舍门口方向，挠了挠头，嘴唇开合说出：“差不多了吧。”\n"
    "全片贯穿：环境音是风声。"
)
CONT = "孟浩感叹科举之路不通" + CAPACITY_SPLIT_MARKER
# asset_manifest 注入提示词的固定描述——真实调用里由 canonical_phrases(payload) 取出
PHRASES = [MENGHAO.replace("@孟浩 ", "").rstrip("，"), STYLE.rstrip("，")]


def test_sub_shots_are_split_on_shot_labels_and_stop_at_the_trailer() -> None:
    beats = sub_shots(EP1_SHOT2)
    assert len(beats) == 3
    assert beats[0].startswith("推近")
    assert "全片贯穿" not in beats[-1]


def test_is_continuation_follows_the_marker_written_by_the_splitter() -> None:
    assert is_continuation(CONT)
    assert not is_continuation("孟浩感叹科举之路不通")
    assert not is_continuation("")


def test_monologue_chain_is_caught_even_though_camera_words_changed() -> None:
    """镜头词全换了（推近/特写/拉远 → 固定/推近/特写），调度没换：人、石头、葫芦、夕阳。"""
    hits = repeated_staging(sub_shots(EP1_SHOT2), sub_shots(EP1_SHOT3), drop_phrases=PHRASES)
    assert len(hits) >= 2, hits
    errors = repeated_staging_errors([(2, EP1_SHOT2)], EP1_SHOT3, current_segment_no=3, synopsis=CONT, drop_phrases=PHRASES)
    assert len(errors) == 1
    assert "第 3 段是第 2 段起同一场戏的续段" in errors[0]
    assert "回到已出现过的画面最多用一个子镜" in errors[0]


def test_multi_character_chain_with_new_business_passes() -> None:
    """第 3 集那条链：三个人物各有新动作（跑来→站定挠头、说不同的话），不是重拍。"""
    assert repeated_staging_errors([(10, EP3_SHOT10)], EP3_SHOT11, current_segment_no=11, synopsis=CONT, drop_phrases=[]) == []


def test_boilerplate_shared_by_every_subshot_does_not_count_as_repetition() -> None:
    """风格句与人物固定描述每个子镜都带，剔掉之后才比调度；否则任何两段都「相似」。"""
    a = f"镜头1：固定 {STYLE}{MENGHAO}从岩石上站起身，拍了拍袍角的尘土\n镜头2：跟随 {STYLE}{MENGHAO}沿着山脊向下走去，背影渐小\n"
    b = f"镜头1：固定 {STYLE}{MENGHAO}停在一处断崖前，俯身向下张望\n镜头2：推近 {STYLE}{MENGHAO}伸手抓住崖边的藤条，用力拽了两下\n"
    assert repeated_staging(sub_shots(a), sub_shots(b), drop_phrases=PHRASES) == []


def test_single_callback_subshot_is_allowed() -> None:
    """回到上一段的画面一次是合法的回切；只有 ≥2 个子镜重拍才阻断。"""
    cur = (
        f"镜头1：固定 {MENGHAO}猛地站起，把葫芦高高举过头顶\n"
        f"镜头2：跟随 葫芦脱手飞出，在暮色里划出一道弧线坠向山下\n"
        f"镜头3：拉远 {STYLE}@孟浩 坐在山顶边缘，望向远方沉落的夕阳\n"
    )
    hits = repeated_staging(sub_shots(EP1_SHOT2), sub_shots(cur), drop_phrases=PHRASES)
    assert len(hits) <= 1
    assert repeated_staging_errors([(2, EP1_SHOT2)], cur, current_segment_no=3, synopsis=CONT, drop_phrases=PHRASES) == []


def test_gate_only_applies_to_continuation_segments() -> None:
    """正常换段本来就允许回到同一场景——标记不在就一律放行。"""
    assert repeated_staging_errors([(2, EP1_SHOT2)], EP1_SHOT3, current_segment_no=3, synopsis="换了一场戏", drop_phrases=PHRASES) == []
    assert repeated_staging_errors([], EP1_SHOT3, current_segment_no=1, synopsis=CONT, drop_phrases=PHRASES) == []


def test_continuation_rule_lists_previous_beats_and_states_the_positive_ask() -> None:
    rule = staging_continuation_rule(EP1_SHOT2, previous_segment_no=2, synopsis=CONT)
    assert rule and "第 2 段同一场戏的续段" in rule
    assert "镜头1：" in rule and "镜头3：" in rule
    assert "@孟浩" not in rule  # 参考图引用不该进规则文案
    assert "每个子镜都要给观众新的东西看" in rule
    assert staging_continuation_rule(EP1_SHOT2, previous_segment_no=2, synopsis="换了一场戏") is None
    assert staging_continuation_rule("", previous_segment_no=0, synopsis=CONT) is None


def test_monotony_accumulates_across_the_whole_chain_not_just_the_previous_segment() -> None:
    """镜 3 的葫芦特写重的是镜 2、镜 6 的拉远夕阳重的是镜 1——两两相邻各只命中 1 个子镜，
    整链一起比才看得见 90 秒的单调。"""
    head = (
        f"镜头1：固定 {STYLE}{MENGHAO}坐在山顶的岩石上，手指搭在葫芦上，眉头皱起\n"
        f"镜头2：拉远 {STYLE}@孟浩 坐在山顶边缘，望向远方沉落的夕阳，身影在暖橙色霞光里显得单薄\n"
    )
    mid = (
        f"镜头1：推近 @孟浩 的脸，睫毛垂落盖住眼眸，嘴角垮下，眼神空洞地望着前方\n"
        f"镜头2：特写 @孟浩 身侧的{GOURD}光影精致写实\n"
    )
    tail = (
        f"镜头1：仰拍 {MENGHAO}坐在岩石上，目光望向辽远的天空\n"
        f"镜头2：拉远 暖橙色夕阳落在 @孟浩 的脸上，他望向远方沉落的夕阳，身影在霞光里显得单薄\n"
        f"镜头3：特写 @孟浩 身侧的{GOURD}被夕阳镀上一层金边\n"
    )
    # 只看上一段：夕阳在 mid 里没有，葫芦在 head 里没有——各只命中 1 个
    assert len(repeated_staging(sub_shots(mid), sub_shots(tail), drop_phrases=PHRASES)) <= 1
    # 看整条链：夕阳重 head、葫芦重 mid，两个子镜都重
    errors = repeated_staging_errors([(1, head), (2, mid)], tail, current_segment_no=3, synopsis=CONT, drop_phrases=PHRASES)
    assert len(errors) == 1 and "第 1 段起同一场戏的续段" in errors[0]


def test_chain_prompt_texts_walks_back_to_the_chain_head_and_stops_there() -> None:
    plans = [SimpleNamespace(segment_no=1, synopsis="上一场戏"), SimpleNamespace(segment_no=2, synopsis="本场戏"),
             SimpleNamespace(segment_no=3, synopsis="本场戏" + CAPACITY_SPLIT_MARKER),
             SimpleNamespace(segment_no=4, synopsis="本场戏" + CAPACITY_SPLIT_MARKER)]
    drafts = {1: SimpleNamespace(prompt_text="P1"), 2: SimpleNamespace(prompt_text="P2"), 3: SimpleNamespace(prompt_text="P3")}
    assert chain_prompt_texts(plans, drafts, 4) == [(2, "P2"), (3, "P3")]  # 不越过链头进上一场戏
    assert chain_prompt_texts(plans, drafts, 3) == [(2, "P2")]
    assert chain_prompt_texts(plans, drafts, 2) == []  # 链头自己不是续段


def test_canonical_phrases_come_from_the_manifest_not_from_guessing() -> None:
    payload = {"asset_manifest": {
        "characters": [{"display_name": "孟浩", "appearance": "十四五岁少年身形，黑发束起"}, {"display_name": "群演", "appearance": ""}],
        "scenes": [{"display_name": "大青山山顶", "scene_canonical": "虚构室外大青山山顶场景，黄昏时分"}],
    }}
    assert canonical_phrases(payload) == ["十四五岁少年身形，黑发束起", "虚构室外大青山山顶场景，黄昏时分"]
    assert canonical_phrases({}) == []


def test_new_action_by_the_same_character_is_not_a_repeat_once_appearance_is_stripped() -> None:
    """外观句只在少数子镜里带着时，频率法剔不掉它（5 个子镜里出现 2 次够不上「样板」），
    于是「猛地站起举葫芦」会和「坐在岩石上皱眉」相似；按 asset_manifest 的已知文本先剥掉
    才是对的。（外观句在每个子镜都出现时频率法自己就能剔掉，那不是这条用例要证的事。）"""
    prev = (
        f"镜头1：推近 {MENGHAO}坐在大青山山顶的岩石上，眉头皱起，嘴角勾起一抹自嘲的弧度\n"
        f"镜头2：特写 一只手指轻轻摩挲着{GOURD}光影精致\n"
        f"镜头3：拉远 {STYLE}山脊上一个坐着的身影，望向远方沉落的夕阳\n"
        f"镜头4：固定 湛蓝辽阔的天空铺展在画面中，云层被夕阳染成金橙色\n"
    )
    cur = f"镜头1：固定 {MENGHAO}猛地站起，把葫芦高高举过头顶\n"
    assert repeated_staging(sub_shots(prev), sub_shots(cur), drop_phrases=[]) != []  # 不剥：误判相似
    assert repeated_staging(sub_shots(prev), sub_shots(cur), drop_phrases=PHRASES) == []  # 剥掉：是新动作
