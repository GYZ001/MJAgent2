from app.schemas import Bible, Character, Dialogue, EpisodeScreenplay, Shot, Storyboard, World
from app.textmatch import longest_run_ratio
from app.validators import (_contiguous_scene_move, adjacent_spoken_repeat_errors,
                            key_line_catalog, normalize_action_desc, validate_storyboard,
                            storyboard_shot_count_range,
                            validate_storyboard_preserves_key_content,
                            validate_storyboard_shot_covers_outline)


def test_longest_run_ratio_is_exact_for_repetitive_long_text() -> None:
    needle = "甲" * 200 + "关键动作" + "乙" * 200
    haystack = "丙" * 20_000 + "甲" * 120 + "关键动作" + "丁" * 20_000

    assert longest_run_ratio(needle, haystack) == (120 + len("关键动作")) / len(needle)


def test_longest_run_ratio_keeps_contiguous_match_semantics() -> None:
    assert longest_run_ratio("甲乙丙丁", "前缀甲乙后缀丙丁") == 0.5
    assert longest_run_ratio("甲，乙！丙", "前缀甲乙丙后缀") == 1.0
    assert longest_run_ratio("", "任意文本") == 1.0
    assert longest_run_ratio("存在", "") == 0.0


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
            Shot(shot_no=1, duration_s=5, shot_size="全景", camera_move="固定",
                 scene_setting="日，萧家测验广场边缘", characters=["萧炎", "萧薰儿"],
                 action_desc="萧薰儿走到广场边缘的萧炎面前站定，唤他萧炎哥哥，萧炎扯出一个自嘲表情",
                 first_frame_desc="日光下萧炎独自立在广场边缘，神情自嘲，萧薰儿正走近",
                 last_frame_desc="萧薰儿在萧炎面前弯腰示意，萧炎侧脸僵硬，画面渐暗",
                 source_excerpt="萧薰儿走到萧炎面前，唤他萧炎哥哥。",
                 narration="", transition="硬切", continuity_from_prev=False),
            Shot(shot_no=2, duration_s=5, shot_size="中景", camera_move="跟随",
                 scene_setting="日，萧家测验广场外小路", characters=["萧炎", "萧薰儿"],
                 action_desc="萧炎不愿留在广场受人议论，转身离开测验广场，走到外侧的小路上，萧薰儿快步跟上",
                 first_frame_desc="广场外小路上，萧炎背对人群迈步，萧薰儿在身后跟来",
                 last_frame_desc="小路尽头，萧炎停下脚步，萧薰儿立在他身侧",
                 source_excerpt="萧炎转身离开广场，走上外侧的小路。",
                 narration="",
                 transition="淡出淡入", continuity_from_prev=False),
        ],
    )

    errors = validate_storyboard(board, _bible(), target_duration_s=20)

    assert not any("缺少承接说明" in e for e in errors), errors


def test_storyboard_scene_jump_does_not_depend_on_transition_words() -> None:
    """换场由 scene fields 和 continuity_mode 判定，不扫描叙事用词。"""
    board = Storyboard(
        episode_no=1,
        shots=[
            Shot(shot_no=1, duration_s=5, shot_size="全景", camera_move="固定",
                 scene_setting="夜，地下密室", characters=["萧炎"],
                 action_desc="萧炎盯着密室石壁上的古老纹路，眉头紧锁，指尖缓缓抚过冰冷的刻痕",
                 first_frame_desc="昏暗密室里萧炎独自站在石壁前，神情凝重",
                 last_frame_desc="萧炎收回手掌，垂眸沉思，密室一片死寂",
                 source_excerpt="萧炎在密室中观察石壁纹路。",
                 narration="", transition="硬切", continuity_from_prev=False),
            Shot(shot_no=2, duration_s=5, shot_size="中景", camera_move="固定",
                 scene_setting="夜，城外山巅", characters=["萧炎"],
                 action_desc="萧炎独自伫立在山巅之上，望着翻涌的云海，神情复杂，衣袍无风自动",
                 first_frame_desc="开阔山巅上萧炎背对镜头眺望远方云海",
                 last_frame_desc="萧炎侧过身，目光投向天际，云海翻涌",
                 source_excerpt="萧炎站在山巅望着云海。",
                 narration="", transition="淡出淡入", continuity_from_prev=False),
        ],
    )

    errors = validate_storyboard(board, _bible(), target_duration_s=20)

    assert not any("缺少承接说明" in e for e in errors), errors


def test_storyboard_allows_explicit_time_jump_with_new_scene_establishing_frame() -> None:
    """真实回归：白天斗技堂切到夜晚卧室已有时间跳转和淡入，不应要求旁白解释。"""
    board = Storyboard(
        episode_no=1,
        shots=[
            Shot(shot_no=1, duration_s=5, shot_size="近景", camera_move="固定",
                 scene_setting="白天，萧家斗技堂", characters=["萧炎"],
                 action_desc="萧炎抱紧卷轴走向斗技堂门口，回头看了一眼身后的高大书架",
                 first_frame_desc="白天斗技堂内，萧炎抱着卷轴站在书架旁",
                 last_frame_desc="萧炎走到门边，室内光线渐暗，准备淡出淡入下一场景",
                 source_excerpt="萧炎抱着卷轴离开了斗技堂。", narration="",
                 transition="硬切", continuity_from_prev=False),
            Shot(shot_no=2, duration_s=5, shot_size="中景", camera_move="固定",
                 scene_setting="夜晚，萧炎卧室", characters=["萧炎"],
                 action_desc="萧炎将怀中的黑色卷轴放在卧室木桌上，抬手按住桌沿站定",
                 first_frame_desc="夜晚卧室内，萧炎抱着黑色卷轴站在木桌前",
                 last_frame_desc="同一机位，黑色卷轴已经放在桌上，萧炎在桌边站定",
                 source_excerpt="夜里萧炎回到房间，将卷轴放在桌上。", narration="",
                 transition="淡出淡入", continuity_from_prev=False,
                 continuity_mode="scene_change",
                 state_in="夜幕降临后，萧炎回到卧室，怀中仍抱着卷轴。"),
        ],
    )

    errors = validate_storyboard(board, _bible(), target_duration_s=20)

    assert not any("缺少承接说明" in error for error in errors), errors


def _compact_shot(no: int) -> Shot:
    sizes = ["远景", "中景", "特写"]
    return Shot(
        shot_no=no,
        duration_s=5,
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


def test_adjacent_repeated_dialogue_is_rejected_even_when_punctuation_differs() -> None:
    first = _compact_shot(1)
    second = _compact_shot(2)
    first.dialogues = [Dialogue(speaker="萧炎", line="我绝不会忘记今日受到的羞辱", emotion="坚定")]
    second.dialogues = [Dialogue(speaker="萧炎", line="不会忘记，今日受到的羞辱！", emotion="低沉")]

    errors = validate_storyboard(Storyboard(episode_no=1, shots=[first, second]), _bible(), 20)

    assert any("相邻重复台词" in error and "镜01" in error for error in errors), errors


def test_adjacent_new_dialogue_is_not_mistaken_for_a_repeat() -> None:
    first = _compact_shot(1)
    second = _compact_shot(2)
    first.dialogues = [Dialogue(speaker="萧炎", line="我绝不会忘记今日受到的羞辱", emotion="坚定")]
    second.dialogues = [Dialogue(speaker="萧炎", line="我要从今天开始重新修炼", emotion="坚定")]

    errors = adjacent_spoken_repeat_errors(Storyboard(episode_no=1, shots=[first, second]))

    assert errors == []


def test_storyboard_accepts_model_selected_duration_and_scales_spoken_budget() -> None:
    shot = _compact_shot(1)
    shot.duration_s = 10
    shot.dialogues = [Dialogue(speaker="萧炎", line="我一定会查清真相，让所有人知道当年究竟发生了什么", emotion="坚定")]
    board = Storyboard(episode_no=1, shots=[shot])

    ten_second_errors = validate_storyboard(board, _bible(), target_duration_s=50)
    assert not any("duration_s" in error or "口播上限" in error for error in ten_second_errors), ten_second_errors

    shot.duration_s = 5
    five_second_errors = validate_storyboard(board, _bible(), target_duration_s=50)
    assert any("口播上限" in error and "5s" in error for error in five_second_errors), five_second_errors


def test_storyboard_rejects_duration_outside_model_contract() -> None:
    shot = _compact_shot(1)
    shot.duration_s = 11

    errors = validate_storyboard(Storyboard(episode_no=1, shots=[shot]), _bible(), target_duration_s=50)

    assert any("duration_s=11" in error and "5~10s" in error for error in errors), errors


def test_storyboard_allows_extra_split_shots_for_dense_dialogue() -> None:
    """50s 基础是 5 镜，内容密时可拆到 10 镜（50s 上限），只要仍在总时长上限内。"""
    board = Storyboard(episode_no=1, shots=[_compact_shot(i) for i in range(1, 11)])
    for shot in board.shots:
        shot.duration_s = 5

    errors = validate_storyboard(board, _bible(), target_duration_s=50)

    assert not any("镜头数" in e for e in errors), errors


def test_storyboard_allows_as_many_split_shots_as_story_requires() -> None:
    board = Storyboard(episode_no=1, shots=[_compact_shot(i) for i in range(1, 20)])
    for shot in board.shots:
        shot.duration_s = 5

    errors = validate_storyboard(board, _bible(), target_duration_s=50)

    assert not any("镜头数" in e for e in errors), errors


def test_storyboard_allows_three_consecutive_shots_with_the_same_size() -> None:
    board = Storyboard(episode_no=1, shots=[_compact_shot(i) for i in range(1, 4)])
    for shot in board.shots:
        shot.shot_size = "近景"

    errors = validate_storyboard(board, _bible(), target_duration_s=50)

    assert not any("连续 3 个镜头景别" in error for error in errors), errors


def test_normalize_action_desc_strips_template_sequence_marker() -> None:
    assert normalize_action_desc("先，齐肩黑发发扎低马尾的曲惜从咖啡厅隔板后探身") == (
        "齐肩黑发发扎低马尾的曲惜从咖啡厅隔板后探身"
    )
    assert normalize_action_desc("首先：谷言从怔神中回过神") == "谷言从怔神中回过神"
    assert normalize_action_desc("先……曲惜笑着上前半步") == "曲惜笑着上前半步"


def test_normalize_action_desc_keeps_real_words() -> None:
    assert normalize_action_desc("先前曲惜已经把纸杯放回桌面") == "先前曲惜已经把纸杯放回桌面"
    assert normalize_action_desc("先生推门而入，谷言抬头") == "先生推门而入，谷言抬头"


def test_storyboard_count_range_is_not_derived_from_target_duration() -> None:
    """镜数不再由目标时长或固定产品上限裁剪。"""
    for target in (40, 50, 70, 90):
        lower, upper = storyboard_shot_count_range(target)
        assert lower == 1
        assert upper > 1_000_000


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
            Shot(shot_no=1, duration_s=5, shot_size="全景", camera_move="固定",
                 scene_setting="日，萧家测验广场", characters=["萧炎"],
                 action_desc="萧炎站在测验石碑前测验斗气，碑面只亮起三段斗气微光，他被当众羞辱，垂手攥拳脸色铁青",
                 first_frame_desc="测验广场上萧炎手贴石碑，神情紧绷",
                 last_frame_desc="石碑仅亮三段，萧炎攥拳垂眸，画面定在羞辱一刻",
                 source_excerpt="测验石碑只亮起三段斗气，全场哗然。",
                 dialogues=[Dialogue(speaker="萧炎", line="三年斗气十段，废物也配姓萧？", emotion="愤怒")],
                 transition="硬切", continuity_from_prev=False),
            Shot(shot_no=2, duration_s=5, shot_size="中景", camera_move="跟随",
                 scene_setting="日，萧家测验广场", characters=["萧炎", "萧薰儿"],
                 action_desc="萧薰儿在众人嘲讽中走到萧炎身边为他解围，伸手扶住他手臂，萧炎抬眼",
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

    assert any("主线台词" in e or "关键台词" in e for e in errors), errors
    assert any("dialogues" in e for e in errors), errors
    assert not any("narration" in e for e in errors), errors


def test_storyboard_preservation_rejects_reversed_key_dialogue_chain() -> None:
    board = _board_preserving_key_content()
    board.shots[0].dialogues, board.shots[1].dialogues = (
        board.shots[1].dialogues,
        board.shots[0].dialogues,
    )

    errors = validate_storyboard_preserves_key_content(
        board, _screenplay_with_manifest()
    )

    assert any("打乱了主线对白顺序" in e for e in errors), errors


def test_storyboard_order_check_uses_full_script_order_for_legacy_key_lines() -> None:
    """真实回归：旧剧本的 key_lines 按话题链排序时，不应反过来指责正确的分镜顺序。"""
    screenplay = EpisodeScreenplay(
        episode_no=1,
        full_script_text=(
            "【场1】白天 / 斗技堂\n萧炎：先离开这里。\n"
            "【场2】夜晚 / 卧室\n萧薰儿：最后再谈修炼。"
        ),
        key_lines=[
            "萧薰儿：最后再谈修炼。",
            "萧炎：先离开这里。",
        ],
    )
    first = _compact_shot(1)
    first.dialogues = [Dialogue(speaker="萧炎", line="先离开这里。", emotion="平静")]
    second = _compact_shot(2)
    second.dialogues = [Dialogue(speaker="萧薰儿", line="最后再谈修炼。", emotion="平静")]

    errors = validate_storyboard_preserves_key_content(
        Storyboard(episode_no=1, shots=[first, second]), screenplay,
    )

    assert not any("打乱了主线对白顺序" in error for error in errors), errors


def test_storyboard_ignores_legacy_narrator_key_line_without_renumbering_ids() -> None:
    screenplay = EpisodeScreenplay(
        episode_no=1,
        full_script_text=(
            "【场1】白天 / 山林\n旁白：砰、轰。\n"
            "萧炎：先离开这里。\n萧薰儿：最后再谈修炼。"
        ),
        key_lines=[
            "旁白：砰、轰。",
            "萧炎：先离开这里。",
            "萧薰儿：最后再谈修炼。",
        ],
    )
    first = _compact_shot(1)
    first.dialogues = [Dialogue(speaker="萧炎", line="先离开这里。", emotion="平静")]
    second = _compact_shot(2)
    second.dialogues = [Dialogue(speaker="萧薰儿", line="最后再谈修炼。", emotion="平静")]
    second.dialogues.append(Dialogue(speaker="萧炎", line="砰、轰。", emotion="平静"))

    errors = validate_storyboard_preserves_key_content(
        Storyboard(episode_no=1, shots=[first, second]), screenplay,
    )

    assert key_line_catalog(screenplay) == {
        "KL02": "萧炎：先离开这里。",
        "KL03": "萧薰儿：最后再谈修炼。",
    }
    assert not any("打乱了主线对白顺序" in error for error in errors), errors
    assert not any("丢失了剧本标记" in error for error in errors), errors


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
    shot.dialogues = [
        Dialogue(speaker="测验员", line="萧炎，斗之力，三段！级别：低级！", emotion="平静")
    ]
    shot.action_desc = (
        "测验员当众宣读萧炎结果，碑面只亮起三段微光，萧炎站在石碑前一动不动"
    )
    errors = validate_storyboard_shot_covers_outline(
        shot, "萧炎被测验员当众宣告为低级", shot.shot_no)
    assert errors == [], errors


def test_narrative_authority_does_not_interpret_covers_vocabulary() -> None:
    """权威路径由稳定 ID 和证据关系校验，不从 covers 词汇推断语义。"""
    shot = _board_preserving_key_content().shots[0]
    shot.action_desc = "萧媚小跑上前触摸魔石碑，碑面亮起'斗之气：七段！'，人群赞叹声浪骤起"
    errors = validate_storyboard_shot_covers_outline(
        shot,
        "任意开放词汇均不作为控制协议",
        shot.shot_no,
        narrative_authority=True,
    )
    assert errors == []


def test_scene_contiguity_key_ignores_sublocation_suffix() -> None:
    """同一地点的子机位标签归一到同一主键：'广场' 与 '广场·中央石台' 不算两个场景。"""
    from app.validators import _scene_contiguity_key
    base = _scene_contiguity_key("日，乌坦城萧家测验广场")
    assert _scene_contiguity_key("日，乌坦城萧家测验广场·中央石台") == base
    assert _scene_contiguity_key("日，乌坦城萧家测验广场-树荫下") == base


def test_continuity_same_scene_new_focus_char_with_movement_passes() -> None:
    """同场景换焦点人物不要求共同角色，但仍统一使用真实视频尾帧。"""
    board = Storyboard(
        episode_no=1,
        shots=[
            Shot(shot_no=1, duration_s=5, shot_size="特写", camera_move="固定",
                 scene_setting="日，萧家测验广场", characters=["萧炎"],
                 action_desc="萧炎垂眸凝视紧攥的左手，血丝渗出，喉结滚动，未发一言，肩背绷紧",
                 first_frame_desc="萧炎左手特写，掌心发力", last_frame_desc="同机位血丝渗出，他仍低头",
                 source_excerpt="萧炎看着自己的手，一言不发。", narration="", dialogues=[],
                 transition="硬切", continuity_from_prev=False, continuity_mode="same_scene_cut"),
            Shot(shot_no=2, duration_s=5, shot_size="中景", camera_move="固定",
                 scene_setting="日，萧家测验广场", characters=["萧媚"],
                 action_desc="萧媚小跑上前，伸手轻触魔石碑，碑面亮起七段光芒，人群赞叹声浪骤起",
                 first_frame_desc="萧媚从人群侧面上前", last_frame_desc="萧媚触碑后转身，碑面仍亮",
                 source_excerpt="萧媚走上前去，伸手触碰魔石碑。", narration="",
                 dialogues=[], transition="硬切", continuity_from_prev=False,
                 continuity_mode="reaction_cut"),
        ],
    )
    errors = validate_storyboard(board, _bible(), target_duration_s=50)
    assert not any("没有共同角色" in e for e in errors), errors
    assert board.shots[1].continuity_from_prev is True


def test_action_continuation_without_shared_char_or_movement_fails() -> None:
    """仅 action_continuation 才要求共同角色或移动承接；否则应失败。"""
    board = Storyboard(
        episode_no=1,
        shots=[
            Shot(shot_no=1, duration_s=5, shot_size="特写", camera_move="固定",
                 scene_setting="日，萧家测验广场", characters=["萧炎"],
                 action_desc="萧炎垂眸凝视紧攥的左手，血丝渗出，喉结滚动，肩背绷紧",
                 first_frame_desc="萧炎左手特写，掌心发力", last_frame_desc="同机位血丝渗出，他仍低头",
                 source_excerpt="萧炎看着自己的手，一言不发。", narration="", dialogues=[],
                 transition="硬切", continuity_from_prev=False, continuity_mode="same_scene_cut"),
            Shot(shot_no=2, duration_s=5, shot_size="中景", camera_move="固定",
                 scene_setting="日，萧家测验广场", characters=["萧媚"],
                 action_desc="萧媚立于碑前，碑面亮起七段光芒，人群赞叹，她抬眼扫过周围",
                 first_frame_desc="萧媚立于碑前静立", last_frame_desc="萧媚抬眼，碑面仍亮",
                 source_excerpt="萧媚站在碑前，碑面亮起光。", narration="",
                 dialogues=[], transition="硬切", continuity_from_prev=True,
                 continuity_mode="action_continuation"),
        ],
    )
    errors = validate_storyboard(board, _bible(), target_duration_s=50)
    assert any("没有共同角色" in e or "可见移动承接" in e for e in errors), errors
