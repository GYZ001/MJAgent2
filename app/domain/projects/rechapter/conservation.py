"""每段正文守恒校验：重切之后旧文本一个字都不许丢。

原始上传文件不落盘（见 ``app.domain.projects.rechapter`` 包文档），章节一旦
丢字就永久找不回来；曾经真实发生过一次——切章器把紧挨着误判标题「第四节课
语文课……」的真标题行 ``--- 第481章 少女情怀 ---`` 当成"无正文标题"整行丢
弃（``app.ingest._split_chapters_with_removed`` 里 ``if remainder:`` 为假时
直接跳过，连标题文字本身都不进任何新章节）。这类丢失不体现在章数/标题对比
上（旁边的章节数量、序号都可能照常），只有真的把两段文本逐字比对才能发现，
所以必须单独立一道闸门，不能指望 ``segment._stability_ok`` 顺带发现。

比对前双方各自剔除全部空白——切章流程里的 ``strip()``/换行规整属于排版，
不是内容丢失。用线性扫描 + 小窗口重新对齐，不用 ``difflib``：``difflib`` 的
最优对齐在百万字级输入上会超时（本仓库最大样本约 280 万字）。代价是窗口外
发生的错位会被当成"无法对齐"而保守地整段判丢失——宁可因为窗口太小多拒绝
几段（人工能看报告去确认），也不要因为窗口开太大让真正的丢字滑过去。
"""
from __future__ import annotations

import re

from app.domain.projects.rechapter.models import ConservationResult

_WHITESPACE_RE = re.compile(r"\s+")
#: 重新对齐时的搜索窗口与探针长度——两者都是经验取值：探针 40 字足以在这类
#: 白话文小说里唯一定位到正确的重现位置；窗口 5000 字覆盖"一整个被误吞的
#: 短标题行+相邻段落"这类局部错位，不追求无界搜索（那等价于退化回 O(n^2)）。
_WINDOW_CHARS = 5000
_PROBE_CHARS = 40
_MAX_SAMPLES = 5
_SAMPLE_CHARS = 60


def strip_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub("", text or "")


def _record_sample(fragment: str, samples: list[str]) -> None:
    if fragment and len(samples) < _MAX_SAMPLES:
        samples.append(fragment[:_SAMPLE_CHARS])


def _realign(haystack: str, probe_source: str, start: int) -> int:
    """在 ``haystack[start:start+_WINDOW_CHARS]`` 里找 ``probe_source`` 的前
    ``_PROBE_CHARS`` 个字符，找不到返回 -1。"""
    probe = probe_source[:_PROBE_CHARS]
    if not probe:
        return -1
    return haystack.find(probe, start, start + _WINDOW_CHARS)


def _diff_conserved(a: str, b: str) -> tuple[int, int, list[str]]:
    """逐字比对已去空白的 ``a``（旧）与 ``b``（新）。返回 (丢失字数, 新增
    字数, 丢失片段样例，最多 5 条各截 60 字)。"""
    i = j = lost = gained = 0
    samples: list[str] = []
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        k = _realign(a, b[j:], i)
        m = _realign(b, a[i:], j)
        if k != -1 and (m == -1 or k - i <= m - j):
            _record_sample(a[i:k], samples)
            lost += k - i
            i = k
        elif m != -1:
            gained += m - j
            j = m
        else:
            _record_sample(a[i:], samples)
            lost += len(a) - i
            return lost, gained, samples
    if i < len(a):
        _record_sample(a[i:], samples)
        lost += len(a) - i
    if j < len(b):
        gained += len(b) - j
    return lost, gained, samples


def check_conservation(original_text: str, new_chapters: list[dict]) -> ConservationResult:
    """``original_text``：段内文本已剔除残片前缀之后的那部分（即
    ``plan_one_segment`` 里实际喂给切章器的 ``process_text``）。
    ``new_chapters``：切章器的输出，按原顺序拼接后与 ``original_text`` 比对。
    """
    old_stripped = strip_whitespace(original_text)
    new_stripped = strip_whitespace("".join(str(c.get("content") or "") for c in new_chapters))
    lost, gained, samples = _diff_conserved(old_stripped, new_stripped)
    return ConservationResult(lost_chars=lost, gained_chars=gained, lost_samples=samples)
