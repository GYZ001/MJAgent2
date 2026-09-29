"""独白 cue 构建的唯一实现——``monologue_burn``（烧录）与 ``subtitle_shift``
（下载字幕轨）曾各自维护一份几乎相同的 ``_monologue_cues``，且都把整句独白
台词塞进单个不做切分的 ``Cue``：超过样式 ``max_chars_per_line`` 的整句在没有
自动换行的 ASS 样式（``WrapStyle: 2``，见 ``app.subtitles.ass._ass_header``，
不做词内自动折行）下会整行跑出画面右边缘（2026-09-29 生产实测
proj_ca86b15ab7d7 EP1 一句 42 字独白，尾部「掌心一直没有松开。」被裁掉）。

改用 ``app.subtitles.cues.split_display_pieces`` 把每句独白按对白字幕同一套
规则切成多条 <= ``max_chars_per_line`` 的显示片段；两条消费路径都只调用本
模块这一个函数，必然产出一致的切片（不会再出现「烧进像素的字幕」和「下载的
字幕文件」各自算出不同切分结果）。
"""
from __future__ import annotations

from app.final_edit_enhance.monologue_audio import MonologueAudioItem
from app.subtitles.cues import Cue, split_display_pieces

MONOLOGUE_SPEAKER_TAG = "内心独白"
_SPEAKER_PREFIX = f"{MONOLOGUE_SPEAKER_TAG}："  # app.subtitles.ass._cue_display_text 拼接前缀用的分隔符


def monologue_cues(
    items: list[MonologueAudioItem], *, offset_s: float, max_chars_per_line: int
) -> tuple[Cue, ...]:
    """把独白条目切成显示片段并包成 ``Cue``。「内心独白：」前缀只挂在每句的
    第一片上——多片重复挂标签比不做区分更干扰阅读；据此整句统一按「预留前缀
    宽度」的更窄预算切分（而不是只挤压首片），因为这里不看调用方样式的
    ``show_speaker``：``monologue_burn`` 固定显示该前缀，``subtitle_shift``
    跟随项目设置可能不显示，但两条路径必须对同一批独白条目切出同一份片段，
    预算不能因 ``show_speaker`` 不同而分叉，宁可未显示前缀时非首片也略短于
    预算上限。只有一片时片段与旧实现完全一致（文本/时间/utterance_id 不变），
    不因这次改动扰动没有溢出问题的短句。"""
    effective_max_chars = max(1, max_chars_per_line - len(_SPEAKER_PREFIX))
    cues: list[Cue] = []
    for i, item in enumerate(items):
        item_start_s = item.start_s + offset_s
        item_end_s = item.start_s + item.duration_s + offset_s
        pieces = split_display_pieces(item.text, item_start_s, item_end_s, effective_max_chars)
        multi = len(pieces) > 1
        for j, (text, start_s, end_s) in enumerate(pieces):
            utterance_id = f"MONO{i:02d}-{j:02d}" if multi else f"MONO{i:02d}"
            cues.append(Cue(
                shot_no=-1, utterance_id=utterance_id, text=text, start_s=start_s, end_s=end_s,
                speaker=MONOLOGUE_SPEAKER_TAG if j == 0 else "",
            ))
    return tuple(cues)
