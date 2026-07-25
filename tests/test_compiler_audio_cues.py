"""Seedance prompt 声轨指令：旁白=画外音解说嗓音、台词=角色本音对口型，并按时序排列。"""
from app.compiler import compile_prompt
from app.schemas import Bible, Character, Shot, World


def _bible() -> Bible:
    return Bible(
        characters=[Character(name="王浩", role="主角", appearance_canonical="年轻男性,黑色连帽衫",
                              personality="", speech_style="")],
        world=World(era="现代", genre="悬疑", visual_style_canonical="写实动漫画风"))


def _shot(**kw) -> Shot:
    data = dict(shot_no=2, duration_s=5, shot_size="中景", camera_move="固定",
                scene_setting="次日清晨,出租屋", characters=["王浩"],
                action_desc="王浩端着咖啡盯着屏幕，猛地瞪大眼睛，咖啡呛出",
                first_frame_desc="王浩端咖啡看屏幕", last_frame_desc="王浩瞪大眼睛咖啡呛出",
                source_excerpt="次日清晨他看到新闻", narration=None, dialogues=[],
                transition="硬切", continuity_from_prev=False)
    data.update(kw)
    return Shot(**data)


def test_narration_marked_as_offscreen_voiceover():
    """旁白必须标注为画外音、用与角色不同的嗓音、不对口型——避免主角声音念旁白。"""
    p = compile_prompt(_shot(narration="次日清晨，新闻和昨晚补的细节吻合",
                             dialogues=[{"speaker": "王浩", "line": "这不可能", "emotion": "惊恐"}]), _bible())
    assert "旁白" in p and "不对口型" in p
    assert "叙述者" in p or "嗓音" in p
    assert "王浩" in p and "这不可能" in p
    assert "开口" in p or "对口型" in p


def test_setup_narration_ordered_before_dialogue():
    p = compile_prompt(_shot(narration="次日清晨，新闻和昨晚补的细节吻合",
                             dialogues=[{"speaker": "王浩", "line": "这不可能", "emotion": "惊恐"}]), _bible())
    assert p.index("旁白") < p.index("这不可能")


def test_ending_hook_narration_ordered_after_dialogue():
    p = compile_prompt(_shot(narration="可他不知道，危险才刚刚开始",
                             dialogues=[{"speaker": "王浩", "line": "我一定查清楚", "emotion": "坚定"}]), _bible())
    assert p.index("我一定查清楚") < p.index("可他不知道")


def test_audio_timeline_covers_spoken_content():
    p = compile_prompt(_shot(dialogues=[{"speaker": "王浩", "line": "这不可能", "emotion": "惊恐"}]), _bible())
    assert "[AUDIO TIMELINE]" in p
    assert "这不可能" in p
    assert "片段结束前完整结束" in p
