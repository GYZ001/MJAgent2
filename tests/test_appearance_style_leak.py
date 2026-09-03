"""app.portraits.appearance_style_guard 的样本核验。

Fixture 数据全部取自 B 库真实项目（proj_ce9fcf749b23《跑不快的孩子》、
proj_ecabd38b7261《三国演义_白话文版》、proj_a5d711b0a337《西游记》）的
``projects.bible_json`` 逐字导出，不是虚构样例——WS4 派单点名的「跑不快 5 人、
三国 5 人、西游 2 人」共 12 个泄漏样本。``VISUAL_STYLE`` 是这三个项目共用的
``world.visual_style_canonical`` 逐字值。
"""
from __future__ import annotations

from app.portraits.appearance_style_guard import strip_visual_style_leak
from app.portraits.constants import APPEARANCE_MAX, APPEARANCE_MIN

VISUAL_STYLE = "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"

# name -> appearance_canonical（B 库逐字原文，2026-09-03 只读导出）
LEAKED_SAMPLES = {
    "豪尔赫": "中年男性，深棕短发，身形偏瘦，身着洗旧深色外套，整体着装朴素日常，符合国漫3D电影质感",
    "塞莉娅": "中年女性，深棕色长发束低马尾，身着浅灰棉上衣与深色长裤，身形温和舒展，符合国漫3D电影质感的日常着装",
    "莱曼": "中年男性，短发灰白，身着2006年德国国家队门将比赛服，站姿沉稳，光影精致的国漫3D动画电影质感",
    "哈维": "国漫3D电影质感下，中年男性形象，深棕短发，身着巴萨红蓝竖条纹主场球衣，搭配白色球裤与球袜，站姿沉稳",
    "马丁内斯": "中年男性，深棕色短发，身着阿根廷国家队门将比赛服，站姿沉稳，光影精致，符合国漫3D动画电影质感",
    "汉灵帝": "中年东汉帝王形象，乌黑束发配玄色帝王冕冠，身着绣纹宽袖朱红朝服，国漫3D质感的端正站姿",
    "张宝": "中年男性，束发裹黄巾，身着粗布义军战甲，身形挺拔硬朗，面容刚毅质朴，符合国漫3D质感的汉末义军将领形象",
    "张飞": "国漫3D动画质感，身高八尺，豹头环眼，燕颔虎须，身着皂色武将劲装，手持丈八蛇矛，面容威严",
    "关羽": "国漫3D动画质感，中年男性武将，黑发束冠，身着深色劲装外披披风，身形挺拔",
    "曹操": "三十余岁男性，黑发高束玉冠，身着深色宽袖官袍配玉带，神态沉稳威严，国漫3D精致光影质感",
    "须菩提祖师": "国漫3D电影质感：中老年男性形象，银白束发道髻，身着月白宽袖道袍，手持拂尘，站姿沉稳庄重",
    "孙悟空": "国漫3D质感的石猴形象，黄毛尖脸，梳总角发髻，着素色道袍，脚蹬云纹布鞋，身形矫健灵动",
}

# 同一批项目里没有泄漏的真实样本（proj_f8cf2eeb2e66《我欲封天》），用来防止假阳性。
CLEAN_SAMPLES = [
    "十六七岁少年，身形偏瘦个子不高，皮肤微黑，留清爽黑短发，常着杂役衫/绿色外宗长衫，身形挺拔",
    "青年男性，黑长直披肩发，常着素白广袖仙侠长袍，身形挺拔，容貌俊朗周正",
]

_STYLE_TOKENS = ("国漫3D", "3D动画", "画电影质感", "光影精致", "精致光影", "非真人照片", "统一电影画面")


def test_all_twelve_leaked_samples_are_cleaned() -> None:
    for name, appearance in LEAKED_SAMPLES.items():
        cleaned, dropped = strip_visual_style_leak(appearance, VISUAL_STYLE)
        assert dropped, f"{name}: 期望检测到画风泄漏但没有剥离任何分句"
        for token in _STYLE_TOKENS:
            assert token not in cleaned, f"{name}: 剥离后仍残留画风片段 {token!r}：{cleaned!r}"
        assert APPEARANCE_MIN <= len(cleaned) <= APPEARANCE_MAX, (
            f"{name}: 剥离后长度 {len(cleaned)} 不在 {APPEARANCE_MIN}~{APPEARANCE_MAX} 区间：{cleaned!r}"
        )


def test_clean_samples_are_left_unchanged_no_false_positive() -> None:
    for appearance in CLEAN_SAMPLES:
        cleaned, dropped = strip_visual_style_leak(appearance, VISUAL_STYLE)
        assert cleaned == appearance
        assert dropped == []


def test_empty_appearance_or_style_returns_original() -> None:
    assert strip_visual_style_leak("", VISUAL_STYLE) == ("", [])
    assert strip_visual_style_leak("中年男性", "") == ("中年男性", [])
    assert strip_visual_style_leak("", "") == ("", [])


def test_pure_style_description_does_not_empty_the_field() -> None:
    """极端情况下如果清理会把全部内容清空，宁可原样返回也不能让外观锚点整体消失。"""
    cleaned, dropped = strip_visual_style_leak(VISUAL_STYLE, VISUAL_STYLE)
    assert cleaned == VISUAL_STYLE
    assert dropped == []
