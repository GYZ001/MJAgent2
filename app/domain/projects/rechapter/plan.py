"""只读编排：产出一份完整的 ``ProjectPlan``，不写库、不开事务。

前提校验（"项目不是一章一集就整个项目拒绝"）、冻结判据计算、逐段重切与
校验、以及由此派生的 chapters/episodes 写入清单，全部在这里完成；真正落库
交给 ``apply.apply_project``。
"""
from __future__ import annotations

import json

from app.planning import chapter_preview

from app.domain.projects.rechapter.frozen import frozen_chapter_idx, frozen_episode_rows
from app.domain.projects.rechapter.models import ChapterRow, ChapterWrite, EpisodeWrite, ProjectPlan, SegmentPlan
from app.domain.projects.rechapter.segment import maximal_segments, plan_one_segment


def _load_chapters(conn, project_id: str) -> list[ChapterRow]:
    rows = conn.execute(
        "SELECT id, idx, title, content FROM chapters WHERE project_id=? ORDER BY idx",
        (project_id,),
    ).fetchall()
    return [
        ChapterRow(id=row["id"], idx=row["idx"], title=row["title"] or "", content=row["content"] or "")
        for row in rows
    ]


def _validate_one_to_one(conn, project_id: str, chapters: list[ChapterRow]) -> str | None:
    """本机制只支持"一章一集"的项目；不满足就整个项目拒绝，不做部分处理。"""
    episodes = conn.execute(
        "SELECT episode_no, source_chapters FROM episodes WHERE project_id=?", (project_id,)
    ).fetchall()
    if not episodes:
        return "项目没有任何分集，无法校验一章一集前提，拒绝"
    chapter_idx_set = {c.idx for c in chapters}
    seen: set[int] = set()
    for row in episodes:
        try:
            source = json.loads(row["source_chapters"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return f"第{row['episode_no']}集 source_chapters 不是合法 JSON，拒绝"
        if len(source) != 1:
            return f"第{row['episode_no']}集对应 {len(source)} 个章节，不是一章一集，拒绝"
        if int(source[0]) != int(row["episode_no"]):
            return f"第{row['episode_no']}集绑定章节 idx={source[0]}，与集号不一致，拒绝"
        seen.add(int(source[0]))
    if seen != chapter_idx_set:
        missing = sorted(chapter_idx_set - seen)[:5]
        extra = sorted(seen - chapter_idx_set)[:5]
        return f"章节与分集未形成一一对应：无分集的章节 {missing}，无章节的分集 {extra}，拒绝"
    return None


def _derive_title(title: str, idx: int) -> str:
    """与 ``app.planning._insert_regex_plan_episodes`` 同款派生口径。"""
    return title or f"第{idx}章"


def _segment_chapter_writes(segment: SegmentPlan) -> list[ChapterWrite]:
    old_count, new_count = len(segment.old_chapters), len(segment.new_chapters)
    writes: list[ChapterWrite] = []
    for i in range(min(old_count, new_count)):
        old, new = segment.old_chapters[i], segment.new_chapters[i]
        content, title = str(new["content"]), str(new["title"])
        if content == old.content and title == old.title:
            continue  # 新旧字节完全一致，不产生空操作写入——"changed" 判据要诚实
        writes.append(ChapterWrite(
            action="update", idx=old.idx, id=old.id, title=title,
            content=content, char_count=len(content), paratext_json=new.get("paratext_json"),
        ))
    for i in range(old_count, new_count):
        new = segment.new_chapters[i]
        content = str(new["content"])
        writes.append(ChapterWrite(
            action="insert", idx=segment.start_idx + i, title=str(new["title"]),
            content=content, char_count=len(content), paratext_json=new.get("paratext_json"),
        ))
    for i in range(new_count, old_count):
        old = segment.old_chapters[i]
        writes.append(ChapterWrite(action="delete", idx=old.idx, id=old.id))
    return writes


def _episode_row(conn, project_id: str, episode_no: int) -> dict | None:
    row = conn.execute(
        "SELECT id, title, synopsis FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, episode_no),
    ).fetchone()
    return dict(row) if row else None


def _matching_episode_write(conn, project_id: str, idx: int, old: ChapterRow, new: dict) -> tuple[EpisodeWrite | None, str | None]:
    """派生值没变就不产生空操作写入；变了但旧集已被人工改过，保留并报告跳过
    原因；旧集仍等于旧章节的机械派生值，才用新派生值覆盖。"""
    derived_old = (_derive_title(old.title, old.idx), chapter_preview(old.content))
    derived_new = (_derive_title(str(new["title"]), idx), chapter_preview(str(new["content"])))
    if derived_old == derived_new:
        return None, None
    episode = _episode_row(conn, project_id, idx)
    if episode is None:
        return None, None
    if (episode["title"], episode["synopsis"]) != derived_old:
        return None, f"第{idx}集标题/简介已被人工改动，未覆盖"
    write = EpisodeWrite(
        action="update", episode_no=idx, episode_id=episode["id"],
        title=derived_new[0], synopsis=derived_new[1],
    )
    return write, None


def _segment_episode_writes(conn, project_id: str, segment: SegmentPlan) -> tuple[list[EpisodeWrite], list[str]]:
    writes: list[EpisodeWrite] = []
    skips: list[str] = []
    old_count, new_count = len(segment.old_chapters), len(segment.new_chapters)
    for i in range(min(old_count, new_count)):
        idx = segment.start_idx + i
        write, skip = _matching_episode_write(conn, project_id, idx, segment.old_chapters[i], segment.new_chapters[i])
        if write:
            writes.append(write)
        if skip:
            skips.append(skip)
    for i in range(old_count, new_count):
        idx = segment.start_idx + i
        new = segment.new_chapters[i]
        writes.append(EpisodeWrite(
            action="insert", episode_no=idx,
            title=_derive_title(str(new["title"]), idx), synopsis=chapter_preview(str(new["content"])),
        ))
    for i in range(new_count, old_count):
        idx = segment.start_idx + i
        episode = _episode_row(conn, project_id, idx)
        if episode is not None:
            writes.append(EpisodeWrite(action="delete", episode_no=idx, episode_id=episode["id"]))
    return writes, skips


def plan_project(conn, project_id: str) -> ProjectPlan:
    """只读计算一个项目的完整重切计划；永不写库。"""
    chapters = _load_chapters(conn, project_id)
    if not chapters:
        return ProjectPlan(project_id=project_id, ok=False, reject_reason="项目没有任何章节")
    reject_reason = _validate_one_to_one(conn, project_id, chapters)
    if reject_reason:
        return ProjectPlan(project_id=project_id, ok=False, reject_reason=reject_reason)

    frozen_episodes = frozen_episode_rows(conn, project_id)
    frozen_ids = {str(e["id"]) for e in frozen_episodes}
    frozen_idx = frozen_chapter_idx(conn, project_id, frozen_episodes)
    chapters_by_idx = {c.idx: c for c in chapters}
    all_idx = sorted(chapters_by_idx)
    max_idx = all_idx[-1]

    plan = ProjectPlan(
        project_id=project_id, ok=True, reject_reason="",
        frozen_episode_ids=frozen_ids, frozen_idx=frozen_idx, old_chapter_count=len(chapters),
    )
    for start, end in maximal_segments(all_idx, frozen_idx):
        segment = plan_one_segment(chapters_by_idx, start, end, is_tail=(end == max_idx))
        plan.segments.append(segment)
        if not segment.accepted:
            continue
        plan.chapter_writes.extend(_segment_chapter_writes(segment))
        episode_writes, skips = _segment_episode_writes(conn, project_id, segment)
        plan.episode_writes.extend(episode_writes)
        plan.unedited_episode_skips.extend(skips)

    delta = sum(len(s.new_chapters) - len(s.old_chapters) for s in plan.segments if s.accepted)
    plan.new_chapter_count = plan.old_chapter_count + delta
    return plan
