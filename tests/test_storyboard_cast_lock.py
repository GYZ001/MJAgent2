"""同框人物清单锁定的确定性回填（app.production.storyboard_cast_lock）。

2026-09-28 真实回归：《顾念长安》第 1 集第 9 段背景自动出现撞脸路人与陌生女性、真顾屿却不在
温念桌边，第 8 段也冒出未铺垫的顾客——全片贯穿约束模板里的「人数锁定」29/30 段逐字相同、不
含实际人数，没有约束力。判据从数据推导：直接读本段 resources.characters 的 visibility 字段。

2026-10-01 名单语义改写（第 1 集重做第二轮分镜独立核查，``/tmp/mjtest/ep1_redo/
segments_r2.json`` 第 14/30/31 段）：旧句「画面中只有……共 N 人」被视频模型逐帧执行成
「每个镜头都要有 N 人」，与单人反打特写互相矛盾；改写为「本段(现实/闪回)画面出场人物共
N 人：……；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。」——「共 N 人」明确是
全段名单人数，不是单镜人数。剥离正则同时认旧收尾「画面中只有」与新收尾「本段……画面出场
人物」两种写法，本文件下方 ``test_old_format_lock_sentence_is_migrated_to_new_wording``
等用例专门核验这一点：存量分镜（旧写法）传入也会被正确识别并改写成新写法，不是必须逐字匹配
才能幂等。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_cast_lock import ensure_cast_lock_in_prompt


def _character(identity_id: str, display_name: str, *, visible: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        identity_id=identity_id, display_name=display_name,
        visibility="visible" if visible else "voice_only",
    )


def _draft(prompt_text: str, characters: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(prompt_text=prompt_text, resources=SimpleNamespace(characters=characters))


def test_appends_lock_sentence_with_actual_names_and_count():
    """红：修复前的旧行为是全片贯穿约束里逐字相同的固定模板，不含实际人数——用手写的
    旧版本函数（永远返回空模板）证明它测不出「路人闯入」这类问题；绿：新函数按数据算出
    人数与正名，写成全段名单语义（「共 N 人」是全段人数，不是单镜人数）。"""
    draft = _draft(
        "镜头1：温念坐在桌边，顾屿走近。",
        [_character("bible:温念", "温念"), _character("bible:顾屿", "顾屿")],
    )
    assert ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text.endswith(
        "本段画面出场人物共2人：@温念、@顾屿；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )

    def _old_fixed_template_lock(_draft) -> None:
        """修复前的旧行为：全片贯穿约束模板里的固定一句，不读任何实际数据。"""
        _draft.prompt_text = _draft.prompt_text.rstrip() + "\n人数锁定：画面中只有我方人物，不出现其他人物。"

    red_draft = _draft(
        "镜头1：温念坐在桌边，顾屿走近。",
        [_character("bible:温念", "温念"), _character("bible:顾屿", "顾屿")],
    )
    _old_fixed_template_lock(red_draft)
    assert "@温念" not in red_draft.prompt_text and "@顾屿" not in red_draft.prompt_text
    assert "只有我方人物" in red_draft.prompt_text, "旧模板确实不含实际人数——红态验证成立"


def test_does_not_duplicate_when_new_format_already_present():
    draft = _draft(
        "镜头1：@温念 坐在桌边。\n"
        "本段画面出场人物共1人：@温念；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。",
        [_character("bible:温念", "温念")],
    )
    before = draft.prompt_text
    assert ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text == before


def test_old_format_lock_sentence_is_migrated_to_new_wording():
    """存量分镜里还是旧写法「画面中只有……共N人」——这句话被视频模型逐帧误读成
    「每个镜头都要有 N 人」（见模块 docstring 2026-10-01 条），不能只满足于识别、
    必须真的改写成新的全段名单语义，不是原样保留。"""
    draft = _draft(
        "镜头1：@温念 坐在桌边。\n画面中只有@温念共1人，不出现其他人物或路人。",
        [_character("bible:温念", "温念")],
    )
    assert ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text == (
        "镜头1：@温念 坐在桌边。\n"
        "本段画面出场人物共1人：@温念；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )
    assert "画面中只有" not in draft.prompt_text, "旧写法必须被整体替换，不是原样保留"


def test_dedupes_repeated_lock_sentence_from_earlier_call_or_model_mimicry():
    """2026-09-28 真实回归：《顾念长安》第 1 集 29 段里 24 段这句话逐字重复了两次
    （模型自己按方言规则也写了一遍同形状的话，与本函数追加的那句格式上恰好一样），
    另一段两处写法不同（其一缺 @、多一个空格）。旧版 ``lock_sentence in prompt`` 逐字
    包含检查在写法不同这个案例里失效才重复追加；新判据按结构标记整体剥离再统一写回
    唯一一句，两种情况都收敛成一句新写法（即便输入是旧写法的重复）。"""
    exact_dup = _draft(
        "镜头1：@温念 坐在桌边。\n画面中只有@温念、@顾屿共2人，不出现其他人物或路人。"
        "\n续接服装：@顾屿 蓝色外套。\n画面中只有@温念、@顾屿共2人，不出现其他人物或路人。",
        [_character("bible:温念", "温念"), _character("bible:顾屿", "顾屿")],
    )
    assert ensure_cast_lock_in_prompt(exact_dup) == []
    assert exact_dup.prompt_text.count("不出现其他人物或路人。") == 1
    assert exact_dup.prompt_text.endswith(
        "本段画面出场人物共2人：@温念、@顾屿；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )

    mismatched_format = _draft(
        "镜头1：@温念 坐在桌边。\n画面中只有@温念、顾屿 共2人，不出现其他人物或路人。"
        "\n续接服装：@顾屿 蓝色外套。",
        [_character("bible:温念", "温念"), _character("bible:顾屿", "顾屿")],
    )
    assert ensure_cast_lock_in_prompt(mismatched_format) == []
    assert mismatched_format.prompt_text.count("不出现其他人物或路人。") == 1
    assert mismatched_format.prompt_text.endswith(
        "本段画面出场人物共2人：@温念、@顾屿；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )


def test_segment_without_visible_characters_is_left_untouched():
    """纯画外音/旁白段：没有可见角色，不写这句话——"无可见角色"本身是诚实的事实。"""
    draft = _draft("镜头1：空镜，风声。", [_character("bible:温念", "温念", visible=False)])
    assert ensure_cast_lock_in_prompt(draft) == []
    assert "画面中只有" not in draft.prompt_text
    assert "画面出场人物" not in draft.prompt_text


def test_empty_prompt_text_is_left_untouched():
    draft = _draft("", [_character("bible:温念", "温念")])
    assert ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text == ""


def test_strips_whole_line_when_model_writes_open_vocabulary_scene_prefix():
    """2026-09-30 真实回归（B 机 provider_calls id=81302，第 1 集第 8 段 opus 原始输出）：
    模型自写的锁定句前面带了一个开放词前缀「咖啡馆」（不是 (?:现实|闪回) 认得的两个词之一），
    旧判据只剥「画面中只有……」这一段，把「咖啡馆」原样留在原处，落库后变成一个孤立残词行。
    场景词是开放集合枚举不完，改成按结构判断：锁定句从行首开始写（前面同一行没有句末标点）
    时，连前缀带整行一起剥掉，不留残词。"""
    draft = _draft(
        "镜头8：温念独自坐在咖啡馆窗边，看着手机。\n"
        "咖啡馆画面中只有@温念 一人入镜，回忆画面中没有人物，不出现其他人物或路人。",
        [_character("bible:温念", "温念")],
    )
    assert ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text == (
        "镜头8：温念独自坐在咖啡馆窗边，看着手机。\n"
        "本段画面出场人物共1人：@温念；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )
    assert draft.prompt_text.count("咖啡馆") == 1, "残留的开放前缀词「咖啡馆」不该再单独成行"


def test_strips_whole_line_for_another_open_vocabulary_prefix():
    """同一真实回归第 22 段：模型写的前缀是「当下」而不是「咖啡馆」——两个不同的开放词
    都要被同一条结构判据覆盖，证明修法不是在给前缀列举新词条。"""
    draft = _draft(
        "镜头22：两人并肩走在走廊。\n"
        "当下画面中只有@温念、@顾屿 共2人，不出现其他人物或路人。",
        [_character("bible:温念", "温念"), _character("bible:顾屿", "顾屿")],
    )
    assert ensure_cast_lock_in_prompt(draft) == []
    assert "当下" not in draft.prompt_text
    assert draft.prompt_text == (
        "镜头22：两人并肩走在走廊。\n"
        "本段画面出场人物共2人：@温念、@顾屿；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )


def test_recognized_real_and_flashback_prefixes_still_strip_whole_line():
    """既有「现实」「闪回」前缀（_CAST_LOCK_SENTENCE_PATTERN 本就认得的两个词）不受影响：
    整条复合锁定句同样被整体剥离、重写成最新一句，不是本次修法想动的行为。"""
    draft = _draft(
        "镜头16：床沿，六岁的顾屿趴着数数。\n"
        "现实画面中只有@温念共1人；闪回画面中只有六岁的顾屿，不出现其他人物或路人。",
        [_character("bible:温念", "温念")],
    )
    assert ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text == (
        "镜头16：床沿，六岁的顾屿趴着数数。\n"
        "本段画面出场人物共1人：@温念；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )


def test_lock_sentence_in_middle_of_line_with_prior_complete_sentence_keeps_prefix():
    """锁定句出现在一行中间、前面同一行已经是一句带句末标点的完整话时，保持剥离前的
    现有行为不变：只剥锁定句本身，不动前面那句正文——那是真实的镜头描述，不是锁定句
    自己的残留前缀。"""
    draft = _draft(
        "镜头20：两人对视，气氛凝固。现实画面中只有@顾屿、@温念共2人，不出现其他人物或路人。",
        [_character("bible:顾屿", "顾屿"), _character("bible:温念", "温念")],
    )
    assert ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text == (
        "镜头20：两人对视，气氛凝固。\n"
        "本段画面出场人物共2人：@顾屿、@温念；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )


def test_three_visible_characters_dedupes_and_preserves_order():
    draft = _draft(
        "镜头1：三人对峙。",
        [
            _character("bible:温念", "温念"),
            _character("bible:顾屿", "顾屿"),
            _character("bible:温念", "温念"),  # 重复条目
            _character("bible:林姐", "林姐"),
        ],
    )
    ensure_cast_lock_in_prompt(draft)
    assert draft.prompt_text.endswith(
        "本段画面出场人物共3人：@温念、@顾屿、@林姐；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )


def test_constraint_line_keyword_expansion_is_collapsed_to_bare_word():
    """2026-10-01 真实回归：模型把方言约束行里系统自己的固定词「人数锁定」自行展开
    成带名单的括号说明（『人数锁定（每个镜头画面中只有@温念 与@顾屿 共2人，不出现
    其他住户、房东或路人）』），这句以『……或路人）』收尾、不是句号，不匹配
    ``_CAST_LOCK_SENTENCE_PATTERN``，与 canonical 锁定句两句并存、互不核对。按结构
    （『人数锁定』这四个字后面紧跟的括号展开）整体剥成短词本身，不枚举名字——名字
    换成任何人、人数换成任何数字都要剥。"""
    draft = _draft(
        "镜头1：温念和顾屿对视。\n"
        "人数锁定（每个镜头画面中只有@温念 与@顾屿 共2人，不出现其他住户、房东或路人）\n"
        "全片贯穿：约束——面部一致、手指正确、人数锁定",
        [_character("bible:温念", "温念"), _character("bible:顾屿", "顾屿")],
    )
    assert ensure_cast_lock_in_prompt(draft) == []
    assert draft.prompt_text == (
        "镜头1：温念和顾屿对视。\n"
        "人数锁定\n"
        "全片贯穿：约束——面部一致、手指正确、人数锁定\n"
        "本段画面出场人物共2人：@温念、@顾屿；每个镜头只画出该镜头文字写到的人，不出现其他人物或路人。"
    )
    assert "或路人）" not in draft.prompt_text, "展开句的括号残留必须被剥掉，不能只剥 canonical 锁定句"


def test_constraint_line_keyword_expansion_collapsed_regardless_of_who_is_named():
    """判据是结构（『人数锁定』紧跟的括号），不是枚举过的名字列表——换一批完全不同
    的名字、不同的人数，同样要被剥。"""
    draft = _draft(
        "镜头1：林姐拿起电话。\n"
        "人数锁定（每个镜头画面中只有@林姐 一人入镜，不出现任何同事或顾客）",
        [_character("bible:林姐", "林姐")],
    )
    assert ensure_cast_lock_in_prompt(draft) == []
    assert "人数锁定（" not in draft.prompt_text
    assert draft.prompt_text.startswith("镜头1：林姐拿起电话。\n人数锁定\n")
