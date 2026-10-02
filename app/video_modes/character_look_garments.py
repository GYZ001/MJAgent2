"""人物造型照：本段 wardrobe 文本里出现的服装道具单品匹配与参考图解析。

背景（2026-10-02 第 1 集实测，见派单）：造型照只拿人物全身定妆照当种子图、按
``continuity_memo.wardrobe`` 纯文字描述图生图，文字复刻不了世界书道具卡登记
的单品细节——温念「外套」道具卡（``prop_references`` 图）实测是短款米白灯芯
绒、5 颗扣、藏青色罗纹袖口、无胸袋，造型照画出来的外套却多了胸前翻盖口袋、
没有藏青袖口；视频生成时同一段还会同时送造型照与「外套」道具图，两张互相
冲突，模型会混画，也和已采纳段落里的外套对不上（产品规则：服装单品细节以
道具卡为准）。本模块把本段 wardrobe 文本里实际点名的服装单品，匹配到世界书
``props``（物件库，见 ``app.props``），再查出它们各自的 ready 参考图，供
``character_look_views``/``character_looks_ensure`` 把这些单品图一并当种子图
——wardrobe 文字只决定"穿哪几件、怎么穿"，单品的款式/颜色/材质细节交给图。

``match_garment_props`` 是判据从数据推导的文本包含匹配（候选名单=本项目世界
书已登记的道具，不是服装词表——CLAUDE.md「禁止黑白名单」）：道具的正名/别名
整串字符按顺序出现在 wardrobe 文本里（允许中间插别的字，比如"深灰色针织长
围巾"命中"深灰色围巾"），且首尾跨度不能离谱地大（≤ 2×候选名长度），否则判定
不是在说这件道具——没有这条，短名称（如"外套"）会在长文本里隔很远的两个字
上误配对。已知局限：这是字符子序列匹配，不是语义匹配，描述里偶然按序出现
另一件道具全部字符时会误判（跨度上限只是缓解，不是杜绝）；两件名字互为子串
的道具（如"外套"与"温念厚外套"/"顾屿外套"）靠"点名句里压根没提到对方角色
名"自然互斥——wardrobe 文本本来就不含角色名，不是本模块专门处理的分支，
真实数据已验证不会误触发（见 ``tests/test_character_look_garments.py``）。
转述为部件名而非整体名时会漏中（比如整条裙子被转述成"裙摆"，而正名「浅蓝色
碎花长裙」没有一个"摆"字，子序列匹配判定不到）——不打算靠再加一张"部件词↔
整体词"的对照表去堵，那本身就是另一种服装词表，CLAUDE.md 明确禁止；已知
缺口标 ``xfail`` 留在测试里（见 ``tests/test_character_look_garments.py`` 的
``test_part_name_paraphrase_of_a_full_garment_is_a_known_miss``），不假装已覆盖。
两件不同道具出现"甲的正名恰好等于乙的别名"这类数据巧合、且都在同一跨度命中
时，裁决优先给靠正名本身命中的那件（不依赖 ``bible_props`` 列表顺序，见
``_best_prop_span``/``match_garment_props`` 内部注释）；但如果两件道具都是
靠别名命中同一跨度，文本本身已经歧义，裁决仍会落回列表顺序——这种情况本模
块判不出来，只能指望世界书不会出现这种别名撞车。

本模块不依赖 ``character_look_views``（后者反过来要 import 本模块做扫描期
匹配，若本模块回头 import 它会成环）：结构归一化在本模块内部单写一份最小版
本（只合并连续空白），不复用 ``character_look_views.normalize_look_key_text``
——与该模块 docstring 里「重复一行比引入跨模块私有依赖更安全」同一类取舍。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.props import prop_reference_for_episode  # 同层（L4）无环，见 LAYERS.toml app.props=4；与 app.video_modes.prop_references 同一真源

# 一段最多带几件服装单品参考图；种子图总数 = 1（全身定妆照）+ 本值，由
# character_looks_ensure 拼接。本仓库对 Seedream 图像生成的参考图张数没有
# 实测/硬编码上限（app/image_providers.py::apply_reference_images 是纯透传，
# 见该文件），上限 5 张远低于视频参考图协议声称的 9 张（app/video_modes/
# mode_selection.py::max_reference_images）与已实测场景（HiAgent 2 张、私有
# 部署 H3 4 张，见 docs/PROVIDER_CAPABILITY_NOTES.md），留足安全余量。
_MAX_GARMENT_REFS = 4


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "")


def _shortest_subsequence_span(text: str, pattern: str) -> tuple[int, int] | None:
    """在 ``text`` 里找 ``pattern`` 按顺序出现（允许中间插字符）的最短跨度子
    序列，返回闭区间下标 ``(start, end)``；完全找不到返回 ``None``。标准
    「最小覆盖子序列」滑动算法：从每个候选起点正向凑齐 pattern 找到最早的
    end，再从 end 反向收紧 start，取其中跨度最小的一次。
    """
    n, m = len(text), len(pattern)
    if m == 0 or n == 0:
        return None
    best: tuple[int, int] | None = None
    i = 0
    while i < n:
        pi, end, j = 0, -1, i
        while j < n and pi < m:
            if text[j] == pattern[pi]:
                pi += 1
                if pi == m:
                    end = j
                    break
            j += 1
        if end == -1:
            break
        pi, start, k = m - 1, end, end
        while k >= i and pi >= 0:
            if text[k] == pattern[pi]:
                pi -= 1
                if pi < 0:
                    start = k
                    break
            k -= 1
        if best is None or (end - start) < (best[1] - best[0]):
            best = (start, end)
        i = start + 1
    return best


def _best_prop_span(text: str, canonical: str, aliases: list[str]) -> tuple[int, int, bool] | None:
    """同一件道具的正名与别名里挑跨度最短、且满足「跨度 ≤ 2×候选名长度」这一
    条的那次命中；候选名长度 <2 不参与匹配（单字几乎必然在任意文本里凑出假
    子序列，不具区分力）。返回 ``(start, end, is_canonical)``——``is_canonical``
    标记这次命中是靠道具正名本身，还是靠某个别名；供 ``match_garment_props``
    在两件不同道具命中同一跨度时做有证据的裁决（见该函数内的说明），不依赖
    ``bible_props`` 的列表顺序。"""
    best: tuple[int, int] | None = None
    best_is_canonical = False
    for idx, candidate in enumerate([canonical, *aliases]):
        if len(candidate) < 2:
            continue
        span = _shortest_subsequence_span(text, candidate)
        if span is None:
            continue
        start, end = span
        if (end - start + 1) > 2 * len(candidate):
            continue
        if best is None or (end - start) < (best[1] - best[0]):
            best = span
            best_is_canonical = idx == 0
    return (best[0], best[1], best_is_canonical) if best is not None else None


def match_garment_props(wardrobe_text: str, bible_props: list[dict[str, Any]]) -> list[str]:
    """返回本段 wardrobe 文本点名的服装道具正名列表，按文本中首次出现位置
    排序，上限 ``_MAX_GARMENT_REFS``。``bible_props`` 是世界书 ``props``（每项
    至少有 ``name``，可选 ``aliases``）——候选集合来自本项目实际登记的道具，
    不是写死的服装词表（CLAUDE.md「判据从数据推导」）。
    """
    text = _collapse_whitespace(wardrobe_text)
    if not text:
        return []
    spans: list[tuple[int, int, str, bool]] = []
    for prop in bible_props or []:
        canonical = str((prop or {}).get("name") or "").strip()
        if not canonical:
            continue
        aliases = [str(a).strip() for a in ((prop or {}).get("aliases") or []) if str(a).strip()]
        span = _best_prop_span(text, canonical, aliases)
        if span is not None:
            spans.append((span[0], span[1], canonical, span[2]))
    # 排序两条判据都是从数据本身推导、不依赖 bible_props 列表顺序：① 区间越长
    # （名称越长）越具体，优先保留；② 跨度相同时，靠道具正名本身命中的优先于
    # 靠别名命中的——两件不同道具可能出现「甲的正名＝乙的别名」这种数据巧合
    # （如道具「风衣」把「外套」登记成别名，而另有道具正名就叫「外套」），此时
    # wardrobe 文本里出现的「外套」应该先信「正名是外套」这件道具，而不是按谁
    # 在 bible_props 里排在前面决定（原实现曾经这样，是 bug：同一段文本换一下
    # 世界书道具的登记顺序就会命中不同的道具，见 tests 里的
    # test_canonical_name_match_wins_over_alias_match_regardless_of_prop_order）。
    # 两件道具都是靠别名命中同一跨度时，裁决仍落回列表顺序——这种情况意味着
    # wardrobe 文本本身有歧义（两个不同道具的别名完全同字同序），不是本函数
    # 能从文本内部判出来的，已知局限见模块 docstring。
    spans.sort(key=lambda s: (s[1] - s[0], 1 if s[3] else 0), reverse=True)
    kept: list[tuple[int, int, str]] = []
    for start, end, name, _is_canonical in spans:
        if any(ks <= start and end <= ke for ks, ke, _ in kept):
            continue
        kept.append((start, end, name))
    kept.sort(key=lambda s: s[0])
    seen: set[str] = set()
    result: list[str] = []
    for _, _, name in kept:
        if name in seen:
            continue
        seen.add(name)
        result.append(name)
        if len(result) >= _MAX_GARMENT_REFS:
            break
    return result


def resolve_garment_refs(
    conn: Any, project_id: str, episode_no: int, names: list[str],
) -> list[dict[str, Any]]:
    """按 ``match_garment_props`` 给出的道具正名逐个查 ready 参考图，供造型照
    生成当额外种子图。查不到/未 ready/文件已不在磁盘上的道具直接跳过——不
    编造占位图（CLAUDE.md「不得兜底填充」）。取号规则与分镜段道具参考图
    （``app.video_modes.prop_references``）同一个 ``app.props.prop_reference_
    for_episode``，不会漂移。返回顺序与 ``names`` 一致。
    """
    out: list[dict[str, Any]] = []
    for name in names:
        row = prop_reference_for_episode(conn, project_id, name, episode_no)
        if not row:
            continue
        status = str(row["status"] or "")
        image_path = str(row["image_path"] or "").strip()
        if status != "ready" or not image_path or not Path(image_path).is_file():
            continue
        out.append({"name": name, "prop_reference_id": str(row["id"]), "image_path": image_path})
    return out
