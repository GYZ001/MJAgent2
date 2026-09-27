"""角色发现候选定妆包的收尾/发布：补齐多视角包，并把候选行原子接入当前
（开区间）分段。

拆自 ``portrait_io.py`` 的 ``_generate_discovered_character_portrait``——原本
是它内部的闭包嵌套函数，被这次修复新增的冲突消解 + 覆盖回归检查顶穿了
``app/FILE_CONVENTIONS.toml`` 的单函数行数棘轮，改成本文件里的顶层函数
（显式传参代替闭包）后各自都在新文件的默认阈值内。

背景（《顾念长安》proj_c89e1d2fa4be 第 1 集温念定妆照并发丢失事故）：候选行
与既有当前行冲突时，旧实现直接 ``DELETE`` 掉其中一方，无日志、真丢数据。
``_resolve_candidate_current_conflict`` 是重定之后的唯一权威处理点，设计见
其 docstring。
"""
from __future__ import annotations

from app.errors import ContentGenerationError

from .portrait_coverage_guard import (
    _close_portrait_segment,
    _uncovered_episode_snapshot,
    _warn_if_new_coverage_gap,
)


def _resolve_candidate_current_conflict(
    conn, *, project_id: str, name: str, ep_start: int, row, current,
) -> bool:
    """候选行（``row``，起始集 ``ep_start``）与既有当前行 ``current``（可能为
    ``None``）冲突时的唯一处理点，返回该候选最终是否应成为新的当前（开区间）
    行。永远不物理删除任何一方——``UNIQUE(project_id, character_name,
    ep_start)`` 保证两行的 ``ep_start`` 不可能相等，于是只有两种情况：

    - 当前行起始集更早（``current.ep_start < ep_start``）：候选从 ``ep_start``
      起接管，当前行收窄到候选生效前一集。原有语义，不变。
    - 当前行起始集更晚（``current.ep_start > ep_start``）：多集并行各自发现/
      补齐同一角色时的竞态——当前行已经是更晚生效、可能已被后续流程依赖的
      当前段，候选不该把它顶掉。按"起始集先后排序决定谁继续覆盖到未来"处理：
      当前行原样保留、继续是当前行；候选退化为覆盖
      ``[ep_start, current.ep_start-1]`` 的历史分段（此区间恒非空），不物理
      删除、也不再被当成"当前"——调用方据此跳过 bible 展示外观改写，否则
      界面会展示一张已经不是当前造型的图，违反"界面承诺与实际行为一致"。
    """
    portrait_id = str(row["id"])
    if not current or current["id"] == portrait_id:
        return True
    if int(current["ep_start"] or 1) < ep_start:
        _close_portrait_segment(
            conn, character_name=name, portrait_id=current["id"],
            before_ep_start=current["ep_start"], before_ep_end=current["ep_end"],
            new_ep_end=ep_start - 1, caller_episode_no=ep_start,
            reason="discovery_candidate_supersedes_earlier_current",
        )
        return True
    _close_portrait_segment(
        conn, character_name=name, portrait_id=portrait_id,
        before_ep_start=ep_start, before_ep_end=row["ep_end"],
        new_ep_end=int(current["ep_start"]) - 1, caller_episode_no=ep_start,
        reason="discovery_candidate_superseded_by_later_current",
        pack_status="ready",
    )
    return False


async def _publish_discovered_candidate(
    conn, project_id: str, name: str, style: str, candidate_appearance: str, row,
    *, ep_start: int, pack_supported: bool, primary_qa: dict | None,
) -> bool:
    """补齐候选的多视角包，再与既有当前行做冲突消解；返回候选是否成为当前行。
    多视角不受支持（旧库迁移期）时视为"直接成为当前"，与原有行为一致。"""
    portrait_id = str(row["id"])
    if not pack_supported:
        return True
    # 延迟导入避免循环依赖：app.multiview 模块级导入 app.portraits.card_owner /
    # current_ref，本模块属于 app.portraits 包，模块级互相导入会成环。
    from app.multiview import ensure_character_multiview_pack, pack_result_ok

    existing_status = str(row["pack_status"] or "")
    if existing_status == "ready":
        pack = {"status": "ready", "portrait_id": portrait_id, "reused": True}
    else:
        pack = await ensure_character_multiview_pack(
            project_id=project_id, portrait_id=portrait_id, character_name=name,
            appearance=candidate_appearance, visual_style=style, ep_start=ep_start,
            base_portrait_id=row["base_portrait_id"], primary_qa=primary_qa,
        )
    if not pack_result_ok(pack):
        conn.execute("UPDATE character_portraits SET pack_status='failed' WHERE id=?", (portrait_id,))
        conn.commit()
        raise ContentGenerationError(f"角色多视角包结构不完整：{name}")

    # 延迟导入避免循环依赖：_open_portrait 定义在 portrait_io.py，而
    # portrait_io.py 反过来要调用本模块——两者互相需要对方，只能有一侧走
    # 模块级 import，这里选延迟导入的一侧（另一侧见 _complete_discovered_
    # candidate 的同款注释）。
    from .portrait_io import _open_portrait

    before_uncovered = _uncovered_episode_snapshot(conn, project_id, name)
    current = _open_portrait(conn, project_id, name)
    becomes_current = _resolve_candidate_current_conflict(
        conn, project_id=project_id, name=name, ep_start=ep_start, row=row, current=current,
    )
    if becomes_current:
        conn.execute(
            "UPDATE character_portraits SET ep_end=NULL,pack_status=? WHERE id=?",
            ("ready", portrait_id),
        )
    conn.commit()
    _warn_if_new_coverage_gap(
        conn, project_id=project_id, character_name=name,
        caller_episode_no=ep_start, before_uncovered=before_uncovered,
    )
    return becomes_current


async def _complete_discovered_candidate(
    conn, project_id: str, name: str, style: str, appearance: str, row,
    *, ep_start: int, pack_supported: bool, primary_qa: dict | None = None,
    purge_on_failure: bool,
) -> dict:
    """补齐并发布同一个候选；重启恢复时不得再占用相同分段键。多视角包结算与
    当前行冲突消解见 ``_publish_discovered_candidate`` /
    ``_resolve_candidate_current_conflict``。"""
    portrait_id = str(row["id"])
    image_path = str(row["image_path"] or "")
    candidate_appearance = str(row["appearance"] or appearance)
    if pack_supported and str(row["pack_status"] or "") == "ready" and row["ep_end"] is None:
        # 包已就绪且已是开区间：纯复用、不写库。分镜前的补齐重试走到这里时若再写库，撞上写锁
        # 就会被外层当成「定妆包生成失败」（ERR-20260902-30223f 刘备：三视角齐全却报失败）。
        return {"portrait_id": portrait_id, "image_path": image_path, "pack_status": "ready", "reused": True, "gate_retry_exhausted": False}
    try:
        becomes_current = await _publish_discovered_candidate(
            conn, project_id, name, style, candidate_appearance, row,
            ep_start=ep_start, pack_supported=pack_supported, primary_qa=primary_qa,
        )
        if becomes_current:
            # 延迟导入避免循环依赖：见 _publish_discovered_candidate 里的同款注释。
            from .portrait_io import _update_bible_appearance

            _update_bible_appearance(conn, project_id, name, candidate_appearance, image_path)
            conn.commit()
    except Exception:
        # 新候选在本调用内失败可沿用原清理语义；重启前已经付费落盘的候选必须保留，
        # 让下一次恢复继续使用，不能因为恢复代码自身异常再次烧图。
        if purge_on_failure:
            # 延迟导入：只有失败清理这一条路径用到，沿用 portrait_io.py/cards.py
            # 等角色发现路径的既有写法，不为这一条错误路径承担模块级导入成本。
            from app.rejected_media import purge_character_portrait
            purge_character_portrait(conn, portrait_id)
        raise
    return {
        "portrait_id": portrait_id,
        "image_path": image_path,
        "pack_status": "ready",
        "reused": not purge_on_failure,
        "gate_retry_exhausted": False,
    }
