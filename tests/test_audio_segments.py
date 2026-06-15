"""旁白与台词的分段/分音色合成时序校验（音画问题修复）。"""
from app import audio


def _row(narration, dialogues):
    return {"narration": narration, "dialogues": dialogues}


def test_narration_setup_plays_before_dialogue():
    """情境/事件旁白是铺垫，应排在台词【之前】（先给情境，人物再反应）。"""
    segs = audio.spoken_segments(_row(
        "次日清晨，新闻推送的内容竟和王浩昨晚补的细节完全吻合",
        [{"speaker": "王浩", "line": "青陵江旧码头？女性遗体？"}]))
    assert [s["role"] for s in segs] == ["narration", "dialogue"]
    assert segs[0]["text"].startswith("次日清晨")
    assert segs[1]["speaker"] == "王浩"


def test_inner_os_narration_plays_before_dialogue():
    segs = audio.spoken_segments(_row(
        "内心OS：除非这不是名单，而是一张角色表",
        [{"speaker": "王浩", "line": "这根本不是名单！"}]))
    assert segs[0]["role"] == "narration" and segs[1]["role"] == "dialogue"


def test_omniscient_hook_narration_plays_after_dialogue():
    """全知结尾悬念钩旁白（“可他不知道…”）排在台词【之后】收尾。"""
    segs = audio.spoken_segments(_row(
        "可他不知道，门外的危险才刚刚开始",
        [{"speaker": "王浩", "line": "我一定会查清楚！"}]))
    assert [s["role"] for s in segs] == ["dialogue", "narration"]


def test_dialogue_only_has_no_narration_segment():
    segs = audio.spoken_segments(_row("", [{"speaker": "王浩", "line": "只有台词"}]))
    assert len(segs) == 1 and segs[0]["role"] == "dialogue"


def test_spoken_text_matches_segment_order():
    row = _row("情境旁白", [{"speaker": "王浩", "line": "台词"}])
    assert audio.spoken_text(row) == "情境旁白。台词"


def test_narration_voice_differs_from_dialogue_voice(monkeypatch):
    """旁白音色与角色台词音色取自不同设置，避免“主角在念旁白”。"""
    settings = {"audio_voice": "Cherry", "audio_narration_voice": "Ethan"}
    monkeypatch.setattr(audio, "get_setting", lambda k, *a, **kw: settings.get(k))
    assert audio._voice_for("dialogue") == "Cherry"
    assert audio._voice_for("narration") == "Ethan"
