"""静默区间计算：纯函数，只依赖标准库。

「静默」指全片时间轴上没有任何台词字幕（``app.subtitles`` 对齐出的
cue，含独白台词生成前的普通对白）覆盖的区间——不是音频响度意义上的静音，
是"没有需要被听清的人声内容"的时间窗，供统一配乐（有台词段压低/静默段抬高）
与主角内心独白（只在没有台词的地方插入新的语音）复用同一份计算。

判据从数据推导（CLAUDE.md）：区间来自本集字幕对齐结果实际产出的 cue 时间，
不写死镜号/段号。
"""
from __future__ import annotations

Interval = tuple[float, float]


def merged_occupied_intervals(spans: list[Interval]) -> list[Interval]:
    """合并重叠/相邻（含乱序输入）的区间；非正长度区间丢弃。"""
    cleaned = sorted((s, e) for s, e in spans if e > s)
    if not cleaned:
        return []
    merged: list[list[float]] = [list(cleaned[0])]
    for start, end in cleaned[1:]:
        last = merged[-1]
        if start <= last[1]:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def speech_free_windows(spans: list[Interval], total_duration_s: float) -> list[Interval]:
    """``spans``（台词区间）在 ``[0, total_duration_s]`` 内的互补区间。

    ``total_duration_s`` 非正时返回空列表（没有时间轴可言，不是"全程静默"）。
    """
    if total_duration_s <= 0:
        return []
    occupied = merged_occupied_intervals([
        (max(0.0, s), min(total_duration_s, e)) for s, e in spans
    ])
    windows: list[Interval] = []
    cursor = 0.0
    for start, end in occupied:
        if start > cursor:
            windows.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total_duration_s:
        windows.append((cursor, total_duration_s))
    return windows


def windows_at_least(windows: list[Interval], min_duration_s: float) -> list[Interval]:
    """只保留时长 >= ``min_duration_s`` 的窗口；供"独白只在够长的静默区插入"使用。"""
    return [(s, e) for s, e in windows if e - s >= min_duration_s]


def window_containing(windows: list[Interval], instant_s: float) -> Interval | None:
    """按二分近似（窗口数很小，线性扫描足够）返回覆盖 ``instant_s`` 的窗口。"""
    for start, end in windows:
        if start <= instant_s <= end:
            return (start, end)
    return None
