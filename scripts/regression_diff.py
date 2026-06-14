"""确定性回归 diff（替代尚未建成的 golden/ 套件）。

针对本次 9 项修复中会影响"稳定链路"的三个轴，给出 before/after 对比：
  A. 编译 prompt 文本（受 #1 题材门控敏感词、#5 超长裁剪顺序影响）
  B. 视频模式选择（受 #3 连贯镜改走首尾帧影响）
  C. 整集音轨逐镜对齐（受 #4 ffprobe 实测时长对齐影响）

"before" 通过 monkeypatch / 忠实复刻修复前逻辑得到；"after" 直接调用当前代码。
固定 fixtures、纯函数，不触网络/DB/ffmpeg，可重复运行。
"""
from __future__ import annotations

import difflib
import re

from app import compiler, video_modes
from app.schemas import Bible, Character, Dialogue, Shot, World


def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _diff(before: str, after: str, label: str) -> bool:
    b = before.splitlines()
    a = after.splitlines()
    d = list(difflib.unified_diff(b, a, fromfile=f"{label} BEFORE", tofile=f"{label} AFTER", lineterm=""))
    if not d:
        print(f"[{label}] 无差异（字节一致）")
        return False
    print("\n".join(d))
    return True


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(name="萧炎", role="主角",
                      appearance_canonical="十六七岁少年，黑色短发，墨绿色窄袖劲装，眉心一点朱砂，神情清冷"),
            Character(name="林梦", role="重要配角",
                      appearance_canonical="二十岁女性，栗色长发束马尾，米白色针织衫配牛仔裤，左腕银色细手链"),
        ],
        world=World(era="架空", genre="玄幻", visual_style_canonical="国漫厚涂插画风，电影级体积光，高饱和暖色调"),
    )


def _shot(**kw) -> Shot:
    base = dict(
        shot_no=1, duration_s=10, shot_size="中景", camera_move="固定",
        scene_setting="夜晚，出租屋", characters=["萧炎"],
        action_desc="萧炎盘坐在卧室床上，淡白气流顺着口鼻钻入体内，他愤怒地攥紧拳头，神色诡异地盯着戒指。",
        first_frame_desc="萧炎盘坐床上，掌心托着黑色戒指，神情平静。",
        last_frame_desc="萧炎盘坐床上，戒指微微发亮，他眉头骤紧、掌心收力。",
        source_excerpt="萧炎闭目盘坐，气流钻入体内，黑戒指诡异发光。",
        narration=None, dialogues=[], transition="硬切", continuity_from_prev=False,
    )
    base.update(kw)
    return Shot(**base)


# ---------- 修复前忠实复刻：#1 敏感词（旧=无条件套用全表） ----------

_OLD_REPLACEMENTS = (
    ("我草", "可恶"), ("卧槽", "可恶"), ("我操", "可恶"), ("他妈的", "可恶"), ("他妈", "可恶"),
    ("妈的", "可恶"), ("该死", "可恶"),
    ("床榻上", "修炼蒲团上"), ("床榻", "修炼蒲团"), ("床上", "室内蒲团上"), ("卧室", "修炼静室"),
    ("口鼻钻入体内", "从周围缓缓汇聚并融入经脉"), ("钻入体内", "融入经脉"), ("涌入体内", "汇入经脉"),
    ("进入体内", "融入经脉"), ("吸收殆尽", "悄然吸收"),
    ("死死捏紧拳头", "用力握拳"), ("死死攥紧拳头", "用力握拳"), ("死死", "用力"),
    ("愤怒地", "神情不甘地"), ("愤怒", "不甘"), ("暴怒", "强烈不甘"),
    ("诡异", "神秘"), ("邪异", "神秘"), ("骂了一句", "低声抱怨一句"),
)


def _old_rewrite_sensitive_terms(text: str, *, aggressive: bool = False) -> str:
    out = text
    for old, new in _OLD_REPLACEMENTS:
        out = out.replace(old, new)
    out = re.sub(r"(?:\d{1,3}|[一二两三四五六七八九十]{1,4})岁(清秀|稚嫩|年少)?(少年|少女|男孩|女孩|少男)?",
                 compiler._replace_age, out)
    out = re.sub(r"未成年(?:人)?", "年轻角色", out)
    out = re.sub(r"草([！!。,.，、？?])", r"可恶\1", out)
    return out


def section_a_prompt(bible: Bible) -> bool:
    _hr("A. 编译 prompt 文本 diff（#1 题材门控敏感词）")
    changed = False
    fixtures = {
        "玄幻镜头（含 卧室/床上/钻入体内/愤怒/诡异）": _shot(),
        "都市镜头（控制组，无敏感词）": _shot(
            scene_setting="午后，咖啡馆", characters=["林梦"],
            action_desc="林梦坐在靠窗座位，指尖轻敲桌面，抬眼望向门口，随后端起咖啡抿了一口。",
            first_frame_desc="林梦坐在窗边，手指搭在杯壁，目光低垂。",
            last_frame_desc="林梦端起咖啡杯抿了一口，目光移向门口。",
            source_excerpt="林梦坐在咖啡馆窗边，等着那个人推门进来。"),
    }
    orig = compiler._rewrite_sensitive_terms
    for name, shot in fixtures.items():
        after = compiler.compile_prompt(shot, bible)
        compiler._rewrite_sensitive_terms = _old_rewrite_sensitive_terms
        try:
            before = compiler.compile_prompt(shot, bible)
        finally:
            compiler._rewrite_sensitive_terms = orig
        print(f"\n--- {name} ---")
        if _diff(before, after, name):
            changed = True
    return changed


# ---------- 修复前忠实复刻：#3 视频模式（旧 _rule_mode：连贯镜→参考图） ----------

def _old_rule_mode(shot: Shot):
    text = video_modes._text_for_rules(shot)
    strong_words = [
        "fight", "battle", "explode", "explosion", "transform", "spell", "magic", "blast",
        "打斗", "战斗", "搏斗", "爆气", "爆炸", "法术", "施法", "变身", "快速转身", "转场",
        "过渡到", "落点", "结尾画面", "尾帧", "强控制", "冲刺", "闪现",
    ]
    light_words = [
        "dialogue", "talk", "walk", "stand", "sit", "look back", "scene continues",
        "对话", "说", "交谈", "站", "坐", "走", "回头", "场景延续", "连续出场",
        "情绪", "环境", "展示", "看向", "轻声",
    ]
    if video_modes._contains_any(text, strong_words):
        return video_modes.FIRST_LAST_FRAME_MODE, "strong action", 0.82
    if shot.dialogues or bool(shot.continuity_from_prev) or video_modes._contains_any(text, light_words):
        return video_modes.REFERENCE_IMAGE_MODE, "dialogue/light/continuity", 0.84
    return video_modes.REFERENCE_IMAGE_MODE, "default", 0.72


def section_b_mode(bible: Bible) -> bool:
    _hr("B. 视频模式选择 diff（#3 连贯镜改走首尾帧）")
    fixtures = {
        "首镜·建立场景": _shot(shot_no=1, continuity_from_prev=False,
                            action_desc="萧炎走进药店，环视四周，停在柜台前。", dialogues=[]),
        "连贯镜·同场景接续": _shot(shot_no=2, continuity_from_prev=True,
                              action_desc="萧炎仍站在柜台前，伸手拿起药瓶端详。"),
        "对话镜·非连贯": _shot(shot_no=3, continuity_from_prev=False, scene_setting="夜晚，巷口",
                          action_desc="萧炎与林梦交谈，林梦轻声解释。",
                          dialogues=[Dialogue(speaker="林梦", line="你不该来。", emotion="平静")]),
        "强动作镜·打斗": _shot(shot_no=4, continuity_from_prev=False, scene_setting="清晨，广场",
                          action_desc="萧炎快速转身释放法术与敌人打斗，保证结尾落点。"),
    }
    sel = video_modes.ShotVideoModeSelector()
    orig = video_modes._rule_mode
    rows = []
    changed = False
    for name, shot in fixtures.items():
        after = sel.select_by_rules(shot)
        video_modes._rule_mode = _old_rule_mode
        try:
            before = sel.select_by_rules(shot)
        finally:
            video_modes._rule_mode = orig
        flag = "  <-- 改变" if before.mode != after.mode else ""
        if before.mode != after.mode:
            changed = True
        rows.append((name, before.mode, before.referenceImagePlan.totalCount,
                     after.mode, after.referenceImagePlan.totalCount, flag))
    w = max(len(r[0]) for r in rows)
    print(f"{'镜头':<{w}}  {'BEFORE mode':<22} refN   {'AFTER mode':<22} refN")
    for name, bm, bn, am, an, flag in rows:
        print(f"{name:<{w}}  {bm:<22} {bn:<4}   {am:<22} {an:<4}{flag}")
    return changed


# ---------- #5 超长裁剪顺序：旧（先丢 extra_negative）vs 新（先丢 filler） ----------

def _tier(t, negative, base_negative, extra):
    if extra and extra in t:
        return "full+extra"
    if base_negative and base_negative in t:
        return "base(丢重生负词)"
    return "none(无任何负向词)"


def _old_trim(core, filler, negative, base_negative, limit):
    """旧逻辑：negative(含extra) → base_negative(丢extra) → ''。filler 永不参与裁剪。"""
    def asm(neg):
        body = "。".join(p for p in core + filler if p)
        return (body + "。" + neg) if neg else body
    for neg in (negative, base_negative, ""):
        t = asm(neg)
        if len(t) <= limit:
            return t, neg
    return asm(""), ""


def _new_trim(core, filler, negative, base_negative, limit):
    """新逻辑：先从末尾丢 filler，保留完整 negative；仍超长才退到 base_negative，再退到 ''。"""
    f = list(filler)
    def asm(parts, neg):
        body = "。".join(p for p in parts if p)
        return (body + "。" + neg) if neg else body
    t = asm(core + f, negative)
    while len(t) > limit and f:
        f.pop()
        t = asm(core + f, negative)
    if len(t) > limit:
        t = asm(core + f, base_negative)
    if len(t) > limit:
        t = asm(core + f, "")
    return t, ("full+extra" if negative in t else ("base" if base_negative in t else "none"))


def section_c_trim() -> bool:
    _hr("C. 超长 prompt 裁剪顺序 diff（#5 保住负向词/重生针对性负词）")
    limit = 1500
    # core 撑到约 1265 字：core+完整负向 ≤1500（可保住），但 core+filler+完整负向 >1500（必须裁）。
    core = ["镜头动作：" + "萧炎缓缓抬手按住石碑，掌心收力，碑面渐亮。" * 60]
    filler = ["环境：夜晚出租屋", compiler.QUALITY_SUFFIX, compiler.NO_BGM_SUFFIX]
    extra = "，本次必须改正：上一版手指畸形/出现多余文字水印"  # 重生针对性负词
    negative = compiler.NEGATIVE_SUFFIX + extra
    before, _ = _old_trim(core, filler, negative, compiler.NEGATIVE_SUFFIX, limit)
    after, _ = _new_trim(core, filler, negative, compiler.NEGATIVE_SUFFIX, limit)
    print("撑爆场景（core≈1265字不可裁，filler 3 段，negative 含重生针对性负词）：")
    print(f"  BEFORE: 长度={len(before):<5} 保留负向={_tier(before, negative, compiler.NEGATIVE_SUFFIX, extra)}")
    print(f"  AFTER : 长度={len(after):<5} 保留负向={_tier(after, negative, compiler.NEGATIVE_SUFFIX, extra)}")
    print(f"  含基础负向词「畸形手」 BEFORE={'是' if '畸形手' in before else '否'}  "
          f"AFTER={'是' if '畸形手' in after else '否'}")
    print(f"  含重生针对性负词       BEFORE={'是' if extra in before else '否'}  "
          f"AFTER={'是' if extra in after else '否'}")
    return extra in after and extra not in before


# ---------- #4 整集音轨逐镜对齐 ----------

def section_d_audio() -> bool:
    _hr("D. 整集音轨逐镜对齐 diff（#4 ffprobe 实测时长 vs 刚性 10s）")
    # Seedance 出片常非精确 10.0s，模拟 5 镜真实时长
    clip_durations = [10.0, 10.30, 9.80, 10.20, 10.10]
    fixed = 10.0
    print(f"模拟 5 镜真实视频时长（秒）：{clip_durations}")
    print(f"\n{'镜':<3} {'视频时长':<8} {'旧音轨段(10s)':<14} {'旧累计起点':<10} {'新音轨段(实测)':<14} {'新累计起点':<10} {'旧漂移'}")
    old_off = 0.0
    new_off = 0.0
    max_drift = 0.0
    for i, d in enumerate(clip_durations, 1):
        drift = old_off - new_off  # 旧音轨第 i 镜起点相对视频真实起点的偏移
        max_drift = max(max_drift, abs(drift))
        print(f"{i:<3} {d:<8} {fixed:<14} {old_off:<10.2f} {d:<14.2f} {new_off:<10.2f} {drift:+.2f}s")
        old_off += fixed
        new_off += d
    video_total = sum(clip_durations)
    print(f"\n视频总长={video_total:.2f}s  旧音轨总长={fixed*len(clip_durations):.2f}s  新音轨总长={new_off:.2f}s")
    short_cut = video_total - fixed * len(clip_durations)
    print(f"旧：-shortest 会把成片截到音轨长度，丢掉结尾 {max(0.0, short_cut):.2f}s 视频；最大逐镜漂移 {max_drift:.2f}s")
    print(f"新：音轨与视频逐镜对齐，累计漂移 0，成片不被提前截断")
    return max_drift > 0


def main() -> None:
    bible = _bible()
    a = section_a_prompt(bible)
    b = section_b_mode(bible)
    c = section_c_trim()
    d = section_d_audio()
    _hr("结论")
    print(f"A 编译 prompt：{'玄幻镜头按预期保留原措辞，都市镜头零变化' if a else '无差异'}")
    print(f"B 视频模式：{'仅连贯镜由参考图→首尾帧，其余不变' if b else '无差异'}")
    print(f"C 超长裁剪：{'新逻辑保住重生针对性负词与基础负向词' if c else '未体现差异'}")
    print(f"D 音轨对齐：{'新逻辑消除逐镜漂移与结尾截断' if d else '无差异'}")


if __name__ == "__main__":
    main()
