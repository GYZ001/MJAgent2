from app.schemas import Bible, Character, Dialogue, EpisodeScreenplay, Shot, Storyboard, World
from app.validators import (_contiguous_scene_move, _has_movement_cue, _has_transition_hint,
                            normalize_action_desc, validate_storyboard,
                            storyboard_shot_count_range,
                            validate_storyboard_preserves_key_content,
                            validate_storyboard_shot_covers_outline)


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(name="萧炎", role="主角",
                      appearance_canonical="十五岁少年，黑发束起，黑色劲装，眉眼倔强坚毅",
                      personality="坚韧"),
            Character(name="萧薰儿", role="配角",
                      appearance_canonical="十五岁少女，长发垂肩，淡青衣裙，眼神清澈温润",
                      personality="温柔"),
        ],
        world=World(era="玄幻古代", genre="玄幻", visual_style_canonical="国风玄幻漫剧厚涂风，暖冷对比光"),
    )


def test_movement_cue_counts_as_transition_explanation() -> None:
    """复现真实失败样本：动作里写了人物移动（转身离开/走到），就是承接说明；
    但这句一个旧的固定承接词都没命中，旧实现会误判“缺少承接说明”。"""
    action = "萧炎不愿留在广场受旁人议论，转身离开测验广场，走到外侧的小路上，左手摩挲袖中黑戒指"
    assert _has_movement_cue(action, "")
    assert not _has_transition_hint("日，萧家测验广场外小路", action, "", "")


def test_contiguous_sublocation_move_is_explained() -> None:
    assert _contiguous_scene_move("日，萧家测验广场", "日，萧家测验广场边缘")
    assert _contiguous_scene_move("日，萧家测验广场边缘", "日，萧家测验广场外小路")


def test_distinct_scene_jump_is_not_treated_as_contiguous() -> None:
    assert not _contiguous_scene_move("夜，地下室", "日，海边沙滩")
    assert not _contiguous_scene_move("夜，城南客栈", "夜，城南码头")


def test_storyboard_no_false_missing_transition_for_walk_to_adjacent_area() -> None:
    """端到端复现 ep1 镜5 的换场误报：从广场边缘走到广场外小路，动作写清了移动，
    不应再出现“缺少承接说明”。"""
    board = Storyboard(
        episode_no=1,
        shots=[
            Shot(shot_no=1, duration_s=10, shot_size="全景", camera_move="固定",
                 scene_setting="日，萧家测验广场边缘", characters=["萧炎", "萧薰儿"],
                 action_desc="萧薰儿走到广场边缘的萧炎面前站定，微微弯腰唤他萧炎哥哥，萧炎扯了扯嘴角",
                 first_frame_desc="日光下萧炎独自立在广场边缘，神情自嘲，萧薰儿正走近",
                 last_frame_desc="萧薰儿在萧炎面前微微弯腰，萧炎侧脸僵硬，画面渐暗",
                 source_excerpt="萧薰儿走到萧炎面前，唤他萧炎哥哥。",
                 narration="", transition="硬切", continuity_from_prev=False),
            Shot(shot_no=2, duration_s=10, shot_size="中景", camera_move="跟随",
                 scene_setting="日，萧家测验广场外小路", characters=["萧炎", "萧薰儿"],
                 action_desc="萧炎不愿留在广场受人议论，转身离开测验广场，走到外侧的小路上，萧薰儿快步跟上",
                 first_frame_desc="广场外小路上，萧炎背对人群迈步，萧薰儿在身后跟来",
                 last_frame_desc="小路尽头，萧炎停下脚步，萧薰儿立在他身侧",
                 source_excerpt="萧炎转身离开广场，走上外侧的小路。",
                 narration="人人都弃他如敝履，三年斗气倒退的秘密究竟藏着什么玄机。",
                 transition="淡出淡入", continuity_from_prev=False),
        ],
    )

    errors = validate_storyboard(board, _bible(), target_duration_s=20)

    assert not any("缺少承接说明" in e for e in errors), errors


def test_storyboard_still_flags_unexplained_scene_jump() -> None:
    """真正无解释的硬跳（换地点、无移动、无时间线索、地点不相干）仍必须报缺少承接。"""
    board = Storyboard(
        episode_no=1,
        shots=[
            Shot(shot_no=1, duration_s=10, shot_size="全景", camera_move="固定",
                 scene_setting="夜，地下密室", characters=["萧炎"],
                 action_desc="萧炎盯着密室石壁上的古老纹路，眉头紧锁，指尖缓缓抚过冰冷的刻痕",
                 first_frame_desc="昏暗密室里萧炎独自站在石壁前，神情凝重",
                 last_frame_desc="萧炎收回手掌，垂眸沉思，密室一片死寂",
                 source_excerpt="萧炎在密室中观察石壁纹路。",
                 narration="", transition="硬切", continuity_from_prev=False),
            Shot(shot_no=2, duration_s=10, shot_size="远景", camera_move="固定",
                 scene_setting="日，城外山巅", characters=["萧炎"],
                 action_desc="萧炎独自伫立在山巅之上，望着翻涌的云海，神情复杂，衣袍无风自动",
                 first_frame_desc="开阔山巅上萧炎背对镜头眺望远方云海",
                 last_frame_desc="萧炎侧过身，目光投向天际，云海翻涌",
                 source_excerpt="萧炎站在山巅望着云海。",
                 narration="", transition="淡出淡入", continuity_from_prev=False),
        ],
    )

    errors = validate_storyboard(board, _bible(), target_duration_s=20)

    assert any("缺少承接说明" in e for e in errors), errors


def _compact_shot(no: int) -> Shot:
    sizes = ["远景", "中景", "特写"]
    return Shot(
        shot_no=no,
        duration_s=8,
        shot_size=sizes[(no - 1) % len(sizes)],
        camera_move="固定",
        scene_setting="日，萧家测验广场",
        characters=["萧炎"],
        action_desc=(
            f"萧炎承接上一刻的沉默站在测验广场边缘，萧炎第{no}次抬眼看向人群，"
            "手掌缓缓收紧又放开，脸上自嘲逐渐压成克制的平静"
        ),
        first_frame_desc=f"测验广场边缘，萧炎垂眼站定，右手刚刚收向袖口，第{no}次呼吸压低。",
        last_frame_desc=f"同一机位下，萧炎已经抬眼望向人群，右手握紧，神情比开头更冷。",
        source_excerpt="少年面无表情，安静的回到了队伍的最后一排。",
        narration="",
        dialogues=[],
        transition="硬切",
        continuity_from_prev=no > 1,
    )


def test_storyboard_allows_extra_split_shots_for_dense_dialogue() -> None:
    """50s 基础是 5 镜，内容密时可拆到 10 镜（50s 上限），只要仍在总时长上限内。"""
    board = Storyboard(episode_no=1, shots=[_compact_shot(i) for i in range(1, 11)])
    for shot in board.shots:
        shot.duration_s = 5

    errors = validate_storyboard(board, _bible(), target_duration_s=50)

    assert not any("镜头数" in e for e in errors), errors


def test_storyboard_still_caps_excessive_split_shots() -> None:
    board = Storyboard(episode_no=1, shots=[_compact_shot(i) for i in range(1, 20)])
    for shot in board.shots:
        shot.duration_s = 5

    errors = validate_storyboard(board, _bible(), target_duration_s=50)

    assert any("镜头数" in e for e in errors), errors


def test_normalize_action_desc_strips_template_sequence_marker() -> None:
    assert normalize_action_desc("先，齐肩黑发发扎低马尾的曲惜从咖啡厅隔板后探身") == (
        "齐肩黑发发扎低马尾的曲惜从咖啡厅隔板后探身"
    )
    assert normalize_action_desc("首先：谷言从怔神中回过神") == "谷言从怔神中回过神"
    assert normalize_action_desc("先……曲惜笑着上前半步") == "曲惜笑着上前半步"


def test_normalize_action_desc_keeps_real_words() -> None:
    assert normalize_action_desc("先前曲惜已经把纸杯放回桌面") == "先前曲惜已经把纸杯放回桌面"
    assert normalize_action_desc("先生推门而入，谷言抬头") == "先生推门而入，谷言抬头"


def test_storyboard_count_range_scales_with_target_duration() -> None:
    # 镜头数上限按「目标时长 / 单镜最短时长」折算，而非统一顶到 18 镜
    assert storyboard_shot_count_range(40) == (4, 8)
    assert storyboard_shot_count_range(50) == (5, 10)
    assert storyboard_shot_count_range(70) == (7, 14)
    assert storyboard_shot_count_range(90) == (9, 18)


# ---------- 分镜防丢失：关键内容保留校验 ----------

def _screenplay_with_manifest(**overrides) -> EpisodeScreenplay:
    base = dict(
        episode_no=1,
        key_lines=["三年斗气十段，废物也配姓萧？", "我萧炎，从今天起，绝不再让人看轻。"],
        key_plot_points=["萧炎测验只剩三段斗气被当众羞辱", "萧薰儿在众人嘲讽中走到萧炎身边为他解围"],
    )
    base.update(overrides)
    return EpisodeScreenplay(**base)


def _board_preserving_key_content() -> Storyboard:
    return Storyboard(
        episode_no=1,
        shots=[
            Shot(shot_no=1, duration_s=10, shot_size="全景", camera_move="固定",
                 scene_setting="日，萧家测验广场", characters=["萧炎"],
                 action_desc="萧炎站在测验石碑前，碑面只亮起三段微光，萧炎垂手攥拳，脸色铁青",
                 first_frame_desc="测验广场上萧炎手贴石碑，神情紧绷",
                 last_frame_desc="石碑仅亮三段，萧炎攥拳垂眸，画面定在羞辱一刻",
                 source_excerpt="测验石碑只亮起三段斗气，全场哗然。",
                 dialogues=[Dialogue(speaker="萧炎", line="三年斗气十段，废物也配姓萧？", emotion="愤怒")],
                 transition="硬切", continuity_from_prev=False),
            Shot(shot_no=2, duration_s=10, shot_size="中景", camera_move="跟随",
                 scene_setting="日，萧家测验广场", characters=["萧炎", "萧薰儿"],
                 action_desc="萧薰儿在众人嘲讽中走到萧炎身边，伸手扶住他手臂为他解围，萧炎抬眼",
                 first_frame_desc="萧薰儿快步走近被孤立的萧炎",
                 last_frame_desc="萧薰儿立在萧炎身侧，萧炎眼神重新聚起",
                 source_excerpt="萧薰儿排开众人，走到萧炎身边。",
                 dialogues=[Dialogue(speaker="萧炎", line="我萧炎，从今天起，绝不再让人看轻。", emotion="坚定")],
                 transition="硬切", continuity_from_prev=True),
        ],
    )


def test_storyboard_preservation_passes_when_key_content_present() -> None:
    errors = validate_storyboard_preserves_key_content(
        _board_preserving_key_content(), _screenplay_with_manifest())
    assert errors == []


def test_storyboard_preservation_flags_dropped_key_line() -> None:
    """分镜把剧本标记的一句金句整句丢掉——必须点名报"丢失了…关键台词"。"""
    board = _board_preserving_key_content()
    # 抹掉第 2 镜那句决定性台词，换成无关口水话。
    board.shots[1].dialogues = [Dialogue(speaker="萧炎", line="走吧。", emotion="平静")]
    board.shots[1].action_desc = "萧薰儿走到萧炎身边站定，两人沉默对视片刻，随后一起转身走开"

    errors = validate_storyboard_preserves_key_content(board, _screenplay_with_manifest())

    assert any("关键台词" in e for e in errors), errors


def test_storyboard_preservation_noop_without_manifest() -> None:
    """剧本未声明必保留清单（旧数据/兜底）时，本校验直接放行，不制造误报。"""
    errors = validate_storyboard_preserves_key_content(
        _board_preserving_key_content(),
        EpisodeScreenplay(episode_no=1, key_lines=[], key_plot_points=[]))
    assert errors == []


def test_storyboard_shot_covers_outline_requires_current_shot_text() -> None:
    shot = _board_preserving_key_content().shots[0]
    errors = validate_storyboard_shot_covers_outline(
        shot,
        "中年测验员当众宣读萧炎斗之力三段并定性为低级",
        shot.shot_no,
    )
    assert any("未落实本镜大纲 covers" in e for e in errors)

    shot.action_desc += "，中年测验员当众宣读萧炎斗之力三段，并定性为低级。"
    assert validate_storyboard_shot_covers_outline(
        shot,
        "中年测验员当众宣读萧炎斗之力三段并定性为低级",
        shot.shot_no,
    ) == []


def test_shot_covers_reports_only_the_missing_atom() -> None:
    """复合 covers 里本镜只落实了一部分事实——报错只点名缺失的那一条，不把已落实的也飘红。"""
    shot = _board_preserving_key_content().shots[0]  # action_desc 含「碑面只亮起三段微光」
    errors = validate_storyboard_shot_covers_outline(
        shot, "碑面只亮起三段微光，引发全场哄笑讥讽不断", shot.shot_no)
    assert len(errors) == 1
    assert "引发全场哄笑讥讽不断" in errors[0]
    assert "三段微光" not in errors[0]  # 已落实的事实不再点名


def test_shot_covers_credits_prior_and_later_shots() -> None:
    """承接放行：本镜未拍的原子，若已在前序镜头落实(向前)或大纲排给后续镜头(向后)，都不算本镜漏戏。"""
    shot = _board_preserving_key_content().shots[0]
    covers = "碑面只亮起三段微光，引发全场哄笑讥讽不断"
    # 向前承接：上一镜已经拍了群嘲
    assert validate_storyboard_shot_covers_outline(
        shot, covers, shot.shot_no, prior_text="围观族人爆出一阵哄笑讥讽不断") == []
    # 向后承接：大纲把群嘲排给了后面的镜头
    assert validate_storyboard_shot_covers_outline(
        shot, covers, shot.shot_no, later_planned_covers="引发全场哄笑讥讽不断") == []


def test_shot_covers_tolerates_synonym_paraphrase() -> None:
    """covers 写"被测验员当众宣告为低级"，本镜实际拍成"测验员…宣读…级别：低级"——
    同一件事的同义改写不应判漏戏（避免逐字纠词把已落实的一拍卡死、反复重试到上限）。"""
    shot = _board_preserving_key_content().shots[0]
    shot.narration = "测验员漠然宣读：萧炎，斗之力，三段！级别：低级！"
    errors = validate_storyboard_shot_covers_outline(
        shot, "萧炎被测验员当众宣告为低级", shot.shot_no)
    assert errors == [], errors


def test_shot_covers_tolerates_abstract_to_concrete_paraphrase() -> None:
    """covers 写抽象概括词（成绩/追捧），本镜拍成具体场景（测出七段/人群赞叹）——
    同义改写不应判漏戏。这是镜03死循环的根因：模型把"萧媚七段成绩引发追捧"正确具象化为
    "测出七段+人群赞叹"，但 2-gram 字面匹配认不出，误判为未落实 covers，反复重试到上限。"""
    shot = _board_preserving_key_content().shots[0]
    shot.action_desc = "萧媚小跑上前触摸魔石碑，碑面亮起'斗之气：七段！'，人群赞叹声浪骤起"
    shot.narration = "人群赞叹：七段！真了不起！不愧是家族种子级人物！"
    errors = validate_storyboard_shot_covers_outline(
        shot, "萧媚七段成绩引发追捧", shot.shot_no)
    assert errors == [], errors


def test_scene_contiguity_key_ignores_sublocation_suffix() -> None:
    """同一地点的子机位标签归一到同一主键：'广场' 与 '广场·中央石台' 不算两个场景。"""
    from app.validators import _scene_contiguity_key
    base = _scene_contiguity_key("日，乌坦城萧家测验广场")
    assert _scene_contiguity_key("日，乌坦城萧家测验广场·中央石台") == base
    assert _scene_contiguity_key("日，乌坦城萧家测验广场-树荫下") == base


def test_continuity_same_scene_new_focus_char_with_movement_passes() -> None:
    """同场景换焦点人物（群像戏"下一个上场的人"）：上一镜拍萧炎，本镜拍萧媚上前测验，
    场景时间都没变，action_desc 写了"小跑上前"入场承接——不应因没有共同角色而误判接镜断裂。
    这是镜03死循环根因：校验器只看共同角色，与错误文案"或在 action_desc 写明承接"自相矛盾。"""
    board = Storyboard(
        episode_no=1,
        shots=[
            Shot(shot_no=1, duration_s=12, shot_size="特写", camera_move="固定",
                 scene_setting="日，萧家测验广场", characters=["萧炎"],
                 action_desc="萧炎垂眸凝视紧攥的左手，血丝渗出，喉结滚动，未发一言",
                 first_frame_desc="萧炎左手特写", last_frame_desc="同机位血丝渗出",
                 source_excerpt="", narration="", dialogues=[],
                 transition="硬切", continuity_from_prev=False),
            Shot(shot_no=2, duration_s=13, shot_size="中景", camera_move="固定",
                 scene_setting="日，萧家测验广场", characters=["萧媚"],
                 action_desc="萧媚小跑上前，伸手轻触魔石碑，碑面亮起七段光芒，人群赞叹声浪骤起",
                 first_frame_desc="萧媚触碑", last_frame_desc="萧媚转身",
                 source_excerpt="", narration="人群赞叹：七段！真了不起！",
                 dialogues=[], transition="硬切", continuity_from_prev=True),
        ],
    )
    errors = validate_storyboard(board, _bible(), target_duration_s=50)
    assert not any("没有共同角色" in e for e in errors), errors


def test_continuity_same_scene_new_focus_char_without_movement_fails() -> None:
    """同场景换焦点人物，但 action_desc 完全没写入场移动承接——此时才该报错，
    提示模型补"上前/跑出"等承接动作。确保放宽不是无条件的。"""
    board = Storyboard(
        episode_no=1,
        shots=[
            Shot(shot_no=1, duration_s=12, shot_size="特写", camera_move="固定",
                 scene_setting="日，萧家测验广场", characters=["萧炎"],
                 action_desc="萧炎垂眸凝视紧攥的左手，血丝渗出，喉结滚动",
                 first_frame_desc="萧炎左手特写", last_frame_desc="同机位血丝渗出",
                 source_excerpt="", narration="", dialogues=[],
                 transition="硬切", continuity_from_prev=False),
            Shot(shot_no=2, duration_s=13, shot_size="中景", camera_move="固定",
                 scene_setting="日，萧家测验广场", characters=["萧媚"],
                 action_desc="萧媚立于碑前，碑面亮起七段光芒，人群赞叹",
                 first_frame_desc="萧媚触碑", last_frame_desc="萧媚转身",
                 source_excerpt="", narration="",
                 dialogues=[], transition="硬切", continuity_from_prev=True),
        ],
    )
    errors = validate_storyboard(board, _bible(), target_duration_s=50)
    assert any("没有共同角色" in e for e in errors), errors
