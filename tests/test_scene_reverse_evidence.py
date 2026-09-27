"""app.scene_reverse.evidence：反打证据判定与 @场景名·反打 点名约定。"""
import json

from app.scene_reverse import evidence as ev


def _view(qa, role="reverse_angle"):
    return {"view_role": role, "qa_json": qa}


def test_only_explicit_passed_check_counts():
    passed = {"reverse_check": {"checked": True, "passed": True, "reason": "朝向相反"}}
    assert ev.reverse_check_passed(_view(json.dumps(passed, ensure_ascii=False)))
    assert ev.reverse_check_passed(_view(passed))
    # 存量图没有证据、判定未完成、判定不通过、字段类型不对：一律不算通过
    assert not ev.reverse_check_passed(_view(None))
    assert not ev.reverse_check_passed(_view("{bad json"))
    assert not ev.reverse_check_passed(_view({"reverse_check": {"checked": False, "passed": None}}))
    assert not ev.reverse_check_passed(_view({"reverse_check": {"checked": True, "passed": False}}))
    assert not ev.reverse_check_passed(_view({"reverse_check": {"checked": "true", "passed": "true"}}))
    # 证据挂在别的视角上不算
    assert not ev.reverse_check_passed(_view(passed, role="establishing"))


def test_mentions_match_given_scene_names_verbatim():
    text = "镜头1：@图片2 站在@修表铺·反打 门口。镜头2：@修表铺 柜台前。"
    assert ev.mentioned_reverse_scenes(text, ["修表铺", "老街"]) == {"修表铺"}
    assert ev.mentioned_reverse_scenes(text, []) == set()
    assert ev.mentioned_reverse_scenes("", ["修表铺"]) == set()


def test_unmatched_mentions_are_reported_and_longest_name_wins():
    text = "镜头1：@修表铺·反打。镜头2：@后院·反打。镜头3：@修表铺二楼·反打。"
    assert ev.unmatched_reverse_mentions(text, ["修表铺", "修表铺二楼"]) == ["@后院·反打"]
    assert ev.unmatched_reverse_mentions(text, []) == ["@修表铺·反打", "@后院·反打", "@修表铺二楼·反打"]


def test_demote_turns_mention_into_plain_words():
    text = "镜头2：固定 近景，背景是@修表铺·反打，@修表铺二楼·反打 的楼梯。"
    out = ev.demote_reverse_mentions(text, ["修表铺", "修表铺二楼"])
    assert "·反打" not in out and "@修表铺" not in out
    assert "修表铺反打方向" in out and "修表铺二楼反打方向" in out


def test_switch_defaults_on_and_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(ev, "get_setting", lambda key: None)
    assert ev.reverse_angle_reference_enabled()
    monkeypatch.setattr(ev, "get_setting", lambda key: "false")
    assert not ev.reverse_angle_reference_enabled()
