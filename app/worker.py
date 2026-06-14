"""视频生成队列（PRD §4.5）：asyncio worker、幂等、成本熔断、重启恢复、自动质检与重抽。"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from app import config, hiagent, video_modes
from app.compiler import ensure_source_excerpt_in_prompt, idem_key as make_idem_key, sanitize_seedance_prompt, shot_cost_cny
from app.db import get_conn, get_setting, log_provider_call, new_id, now, rows_to_dicts
from app.hiagent import ProviderError

_queue: asyncio.Queue[str] = asyncio.Queue()
_workers: list[asyncio.Task] = []


# ---------- 落盘路径 ----------

def _video_path(project_id: str, episode_no: int, shot_no: int, version_no: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "shots" / str(shot_no)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"v{version_no}.mp4"


# ---------- 成本熔断 ----------

def episode_cost(episode_id: str) -> float:
    row = get_conn().execute(
        """SELECT COALESCE(SUM(v.cost_cny), 0) AS c FROM shot_versions v
           JOIN shots s ON s.id = v.shot_id
           WHERE s.episode_id = ? AND v.status IN ('succeeded', 'running', 'queued')""",
        (episode_id,),
    ).fetchone()
    return float(row["c"])


def _budget_exceeded(episode_id: str) -> bool:
    limit = float(get_setting("episode_cost_limit_cny") or 100)
    return episode_cost(episode_id) >= limit


# ---------- 入队 ----------

def _load_shot_model(shot_row) -> "object":
    from app.schemas import Shot
    return Shot(
        shot_no=shot_row["shot_no"], duration_s=shot_row["duration_s"], shot_size=shot_row["shot_size"],
        camera_move=shot_row["camera_move"], scene_setting=shot_row["scene_setting"],
        characters=json.loads(shot_row["characters"] or "[]"), action_desc=shot_row["action_desc"],
        first_frame_desc=(shot_row["first_frame_desc"] if "first_frame_desc" in shot_row.keys() else "") or "",
        last_frame_desc=(shot_row["last_frame_desc"] if "last_frame_desc" in shot_row.keys() else "") or "",
        source_excerpt=shot_row["source_excerpt"] or "",
        narration=shot_row["narration"], dialogues=json.loads(shot_row["dialogues"] or "[]"),
        transition=shot_row["transition"] or "硬切", continuity_from_prev=bool(shot_row["continuity_from_prev"]),
    )


KEYFRAME_HEAD = "head"
KEYFRAME_TAIL = "tail"


def _row_value(row, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    return default


def required_keyframe_kinds(shot_row) -> list[str]:
    """场景起始镜需要首图+尾图；同场景连续镜只需要尾图。"""
    if int(_row_value(shot_row, "shot_no", 0) or 0) <= 1:
        return [KEYFRAME_HEAD, KEYFRAME_TAIL]
    if not bool(_row_value(shot_row, "continuity_from_prev", 0)):
        return [KEYFRAME_HEAD, KEYFRAME_TAIL]
    return [KEYFRAME_TAIL]


def scene_generation_kinds(shot_row, requested: list[str] | None = None) -> list[str]:
    required = required_keyframe_kinds(shot_row)
    if requested is None:
        return required
    if not isinstance(requested, list):
        raise ValueError("关键帧类型必须是列表")
    invalid = [k for k in requested if k not in (KEYFRAME_HEAD, KEYFRAME_TAIL)]
    if invalid:
        raise ValueError(f"未知关键帧类型：{invalid}")
    unnecessary = [k for k in requested if k not in required]
    if unnecessary:
        raise ValueError(f"本镜不需要生成这些关键帧：{unnecessary}")
    kinds = [k for k in required if k in requested]
    if not kinds:
        raise ValueError("没有需要生成的关键帧类型")
    return kinds


def _approved_keyframe_id(shot_row, kind: str) -> str | None:
    if kind == KEYFRAME_HEAD:
        return _row_value(shot_row, "approved_head_scene_id")
    if kind == KEYFRAME_TAIL:
        return _row_value(shot_row, "approved_tail_scene_id")
    raise ValueError(f"未知关键帧类型：{kind}")


def _approved_keyframe(conn, shot_row, kind: str):
    scene_id = _approved_keyframe_id(shot_row, kind)
    if not scene_id:
        return None
    scene = conn.execute(
        "SELECT * FROM shot_scenes WHERE id=? AND shot_id=? AND kind=? AND status='succeeded'",
        (scene_id, _row_value(shot_row, "id"), kind)).fetchone()
    if not scene or not scene["image_path"] or not Path(scene["image_path"]).exists():
        return None
    return scene


def shot_keyframes_ready(shot_row) -> bool:
    conn = get_conn()
    return all(_approved_keyframe(conn, shot_row, kind) for kind in required_keyframe_kinds(shot_row))


def _first_keyframe_for_video(conn, shot_row, after_shot_id: str | None):
    if after_shot_id:
        prev = conn.execute("SELECT * FROM shots WHERE id=?", (after_shot_id,)).fetchone()
        return _approved_keyframe(conn, prev, KEYFRAME_TAIL), "prev_tail_keyframe", prev
    return _approved_keyframe(conn, shot_row, KEYFRAME_HEAD), "head_keyframe", None


def _transition_value(shot_row) -> str:
    transition = (_row_value(shot_row, "transition") or "硬切").strip()
    return transition or "硬切"


def _outgoing_transition_context(conn, shot_row) -> dict | None:
    """下一镜如果是换场镜，则它的 transition 决定本镜结尾怎么收。"""
    if not shot_row:
        return None
    next_shot = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? AND shot_no=?",
        (_row_value(shot_row, "episode_id"), int(_row_value(shot_row, "shot_no", 0) or 0) + 1),
    ).fetchone()
    if not next_shot:
        return None
    if bool(_row_value(next_shot, "continuity_from_prev", 0)):
        return None
    prev_scene = (_row_value(shot_row, "scene_setting") or "").strip()
    next_scene = (_row_value(next_shot, "scene_setting") or "").strip()
    if prev_scene == next_scene:
        return None
    transition = _transition_value(next_shot)
    if transition == "硬切":
        return None
    return {
        "transition": transition,
        "next_scene": next_scene,
        "next_first_frame_desc": (_row_value(next_shot, "first_frame_desc") or "").strip(),
        "next_shot_no": _row_value(next_shot, "shot_no"),
    }


def enqueue_shot(shot_id: str, *, prompt_override: str | None = None,
                 extra_negative: list[str] | None = None, reroll: bool = False,
                 critique: list[str] | None = None, after_shot_id: str | None = None,
                 mode_override: str | None = None) -> dict:
    """为镜头创建视频版本并入队。首尾关键帧策略：
    - 同场景接上镜：首帧 = 上一镜已过审尾图；尾帧 = 本镜已过审尾图；
    - 首镜/换场镜：首帧 = 本镜已过审首图；尾帧 = 本镜已过审尾图。
    critique：上一版 AI 评语问题，作为本次必须改正项写入 prompt。
    幂等：相同 idem_key 的成功版本直接复用（reroll 时跳过复用）。"""
    from app.compiler import compile_prompt
    from app.schemas import Bible

    conn = get_conn()
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise ValueError(f"镜头不存在：{shot_id}")
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (shot_row["episode_id"],)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    if ep["status"] not in ("confirmed", "generating", "done"):
        raise ValueError("分镜脚本未确认，不能生成视频（PRD 原则 P3：贵的环节前人工把关）")

    bible = Bible.model_validate(json.loads(project["bible_json"]))
    shot = _load_shot_model(shot_row)
    prev_for_decision = conn.execute("SELECT * FROM shots WHERE id=?", (after_shot_id,)).fetchone() if after_shot_id else None
    selector = video_modes.ShotVideoModeSelector()
    decision = selector.select_by_rules(shot, shot_row=shot_row, prev_shot=prev_for_decision)
    if mode_override in (video_modes.FIRST_LAST_FRAME_MODE, video_modes.REFERENCE_IMAGE_MODE):
        decision.mode = mode_override  # type: ignore[assignment]
        decision.reason = f"Forced mode: {mode_override}"
        decision.defaulted = True
    if not video_modes.reference_mode_enabled():
        decision.mode = video_modes.FIRST_LAST_FRAME_MODE
        decision.reason = "Reference image mode disabled."
        decision.defaulted = True
    if decision.mode == video_modes.FIRST_LAST_FRAME_MODE:
        decision.referenceImagePlan = video_modes.ReferenceImagePlan(totalCount=0, reusePreviousSceneCount=0, generateNewCount=0, types=[])

    first_scene, first_src, prev_shot = _first_keyframe_for_video(conn, shot_row, after_shot_id)
    if decision.mode == video_modes.FIRST_LAST_FRAME_MODE and shot_row["scene_status"] != "approved":
        raise ValueError("本镜关键帧尚未全部通过评审，请先完成首/尾图评审")
    if decision.mode == video_modes.FIRST_LAST_FRAME_MODE and prev_shot and prev_shot["scene_status"] != "approved":
        raise ValueError("上一镜尾图尚未通过评审，不能作为本镜首帧")
    if decision.mode == video_modes.FIRST_LAST_FRAME_MODE and not first_scene:
        if after_shot_id:
            raise ValueError("上一镜尚无通过评审的尾图，不能生成本镜视频")
        raise ValueError("本镜尚无通过评审的首图，请先生成并通过关键帧评审，再生成视频")
    last_scene = _approved_keyframe(conn, shot_row, KEYFRAME_TAIL)
    if decision.mode == video_modes.FIRST_LAST_FRAME_MODE and not last_scene:
        raise ValueError("本镜尚无通过评审的尾图，请先生成并通过关键帧评审，再生成视频")

    # 跨镜连贯：接上镜时把上一镜动作作为承接线索写入 prompt
    prev_tail_action = None
    if after_shot_id:
        pr = conn.execute("SELECT action_desc FROM shots WHERE id=?", (after_shot_id,)).fetchone()
        prev_tail_action = pr["action_desc"] if pr else None

    outgoing_transition = _outgoing_transition_context(conn, shot_row)
    incoming_transition = None
    if int(shot_row["shot_no"]) > 1 and not bool(shot_row["continuity_from_prev"]):
        incoming_transition = _transition_value(shot_row)
        if incoming_transition == "硬切":
            incoming_transition = None

    prompt_text = (prompt_override if prompt_override else
                   compile_prompt(shot, bible, extra_negative,
                                  with_refs=decision.mode == video_modes.REFERENCE_IMAGE_MODE,
                                  from_scene=(not after_shot_id) and decision.mode == video_modes.FIRST_LAST_FRAME_MODE,
                                  chained=bool(after_shot_id) and decision.mode == video_modes.FIRST_LAST_FRAME_MODE,
                                  critique=critique, prev_tail_action=prev_tail_action,
                                  with_last_frame=decision.mode == video_modes.FIRST_LAST_FRAME_MODE,
                                  incoming_transition=incoming_transition,
                                  outgoing_transition=outgoing_transition["transition"] if outgoing_transition else None,
                                  next_scene=outgoing_transition["next_scene"] if outgoing_transition else None,
                                  next_first_frame_desc=outgoing_transition["next_first_frame_desc"] if outgoing_transition else None))
    prompt_text = ensure_source_excerpt_in_prompt(prompt_text, shot)

    if decision.mode == video_modes.REFERENCE_IMAGE_MODE:
        key_material = prompt_text + f"|mode:{decision.mode}|plan:{video_modes.decision_to_dict(decision)}|after:{after_shot_id or ''}"
    else:
        key_material = prompt_text + f"|mode:{decision.mode}|first:{first_scene['id']}|last:{last_scene['id']}|after:{after_shot_id or ''}"
    if reroll:
        key = make_idem_key(key_material + f"#reroll{time.time()}")
    else:
        key = make_idem_key(key_material)
        existing = conn.execute(
            "SELECT * FROM shot_versions WHERE shot_id=? AND idem_key=? AND status='succeeded' LIMIT 1",
            (shot_id, key)).fetchone()
        if existing:
            return {"reused": True, "version_id": existing["id"]}

    version_no = (conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS m FROM shot_versions WHERE shot_id=?",
        (shot_id,)).fetchone()["m"]) + 1
    version_id = new_id("ver")
    image_meta = {
        "mode": decision.mode,
        "mode_decision": video_modes.decision_to_dict(decision),
        "after_shot_id": after_shot_id,
        "after_shot_no": prev_shot["shot_no"] if prev_shot else None,
        "incoming_transition": incoming_transition,
        "outgoing_transition": outgoing_transition,
    }
    if decision.mode == video_modes.FIRST_LAST_FRAME_MODE:
        image_meta.update({
            "first_frame_scene_id": first_scene["id"],
            "first_frame_path": first_scene["image_path"],
            "first_frame_src": first_src,
            "last_frame_scene_id": last_scene["id"],
            "last_frame_path": last_scene["image_path"],
            "last_frame_src": "tail_keyframe",
        })
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, created_at, image_inputs) "
        "VALUES(?,?,?,?,?, 'queued', ?, ?)",
        (version_id, shot_id, version_no, prompt_text, key, now(),
         json.dumps(image_meta, ensure_ascii=False)))
    job_id = new_id("job")
    conn.execute(
        "INSERT INTO jobs(id, kind, shot_id, version_id, episode_id, project_id, status, created_at, updated_at, after_shot_id) "
        "VALUES(?, 'video', ?, ?, ?, ?, 'queued', ?, ?, ?)",
        (job_id, shot_id, version_id, ep["id"], project["id"], now(), now(), after_shot_id))
    conn.execute("UPDATE episodes SET status='generating' WHERE id=? AND status='confirmed'", (ep["id"],))
    conn.commit()
    _queue.put_nowait(job_id)
    return {"reused": False, "version_id": version_id, "job_id": job_id}


# ---------- 场景关键帧：生成 K 候选 + VLM 评审 + 自动放行 ----------

def _scene_image_path(project_id: str, episode_no: int, shot_no: int, kind: str, version_no: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "shots" / str(shot_no) / "scenes"
    d.mkdir(parents=True, exist_ok=True)
    prefix = "h" if kind == KEYFRAME_HEAD else "t"
    return d / f"{prefix}{version_no}.jpg"


def enqueue_scene(shot_id: str, *, kinds: list[str] | None = None) -> dict:
    """为镜头生成场景关键帧（1~3 候选 + 评审）。创建一个 kind='scene' 任务，由 worker 执行。"""
    conn = get_conn()
    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise ValueError(f"镜头不存在：{shot_id}")
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (shot_row["episode_id"],)).fetchone()
    if ep["status"] not in ("scripted", "confirmed", "generating", "done"):
        raise ValueError("请先确认分镜脚本，再生成关键帧")
    target_kinds = scene_generation_kinds(shot_row, kinds)
    conn.execute("UPDATE shots SET scene_status='generating' WHERE id=?", (shot_id,))
    job_id = new_id("job")
    conn.execute(
        "INSERT INTO jobs(id, kind, shot_id, episode_id, project_id, status, created_at, updated_at, scene_kinds) "
        "VALUES(?, 'scene', ?, ?, ?, 'queued', ?, ?, ?)",
        (job_id, shot_id, ep["id"], ep["project_id"], now(), now(),
         json.dumps(target_kinds, ensure_ascii=False)))
    conn.commit()
    _queue.put_nowait(job_id)
    return {"job_id": job_id, "kinds": target_kinds}


async def _generate_one_scene(prompt: str, ref_inputs: list[str], dest: Path) -> None:
    """生成单张关键帧，带参考图；若网关不支持参考图（报错）则去掉参考图重试一次。"""
    try:
        item = await hiagent.generate_image(prompt, size=config.REF_IMAGE_SIZE, image_inputs=ref_inputs or None)
    except hiagent.ProviderError:
        if not ref_inputs:
            raise
        item = await hiagent.generate_image(prompt, size=config.REF_IMAGE_SIZE)
    if item.get("url"):
        await hiagent.download(item["url"], str(dest))
    elif item.get("b64_json"):
        import base64
        dest.write_bytes(base64.b64decode(item["b64_json"]))
    else:
        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")


async def _generate_keyframe_candidates(*, conn, job, shot, ep, project, bible, kind: str,
                                        char_refs: list[str], anchors: list[str],
                                        comparison_path: str | None,
                                        comparison_b64: str | None) -> tuple[str | None, float]:
    """为某个 kind 生成候选关键帧，返回最佳 scene_id 与分数。"""
    from app.compiler import compile_scene_prompt
    from app.stages import review_scene_image

    shot_model = _load_shot_model(shot)
    outgoing_transition = _outgoing_transition_context(conn, shot) if kind == KEYFRAME_TAIL else None
    base_prompt = compile_scene_prompt(
        shot_model, bible, kind=kind,
        outgoing_transition=outgoing_transition["transition"] if outgoing_transition else None,
        next_scene=outgoing_transition["next_scene"] if outgoing_transition else None,
        next_first_frame_desc=outgoing_transition["next_first_frame_desc"] if outgoing_transition else None,
    )
    # 评审只针对【本帧自己的画面描述】（首图描述/尾图描述），而不是整段 action_desc——
    # 否则首图会因为没表现动作高潮/结尾而被扣分。
    frame_desc = (shot_model.first_frame_desc if kind == KEYFRAME_HEAD else shot_model.last_frame_desc).strip() \
        or shot["action_desc"]
    if kind == KEYFRAME_TAIL and outgoing_transition:
        frame_desc += f"；尾帧带「{outgoing_transition['transition']}」转场收尾视觉"
    max_attempts = max(int(get_setting("scene_max_attempts") or 3), 1)
    threshold = float(get_setting("scene_qa_threshold") or 0.6)
    base_v = conn.execute(
        "SELECT COALESCE(MAX(version_no),0) AS m FROM shot_scenes WHERE shot_id=? AND kind=?",
        (job["shot_id"], kind)).fetchone()["m"]
    candidates = []
    prev_img_path = None
    prev_issues: list[str] = []
    for i in range(max_attempts):
        vno = base_v + i + 1
        sid = new_id("scn")
        dest = _scene_image_path(project["id"], ep["episode_no"], shot["shot_no"], kind, vno)
        # 关键：只用角色定妆照锚定“长相/发型/服饰”，【绝不】把首图/上一镜尾图当生成参考图——
        # 图生图会强力复制参考图，导致首尾帧一模一样、并照搬定妆照的站姿。姿态/构图一律以文字描述为准。
        extra = ""
        refs = list(char_refs)
        if char_refs:
            extra += "。参考图仅用于锁定人物的长相、发型与服饰，请严格按上述画面描述的姿态、动作、机位与构图作画，不要照搬参考图的站姿或构图"
        if kind == KEYFRAME_TAIL:
            extra += "。本帧是本镜【结束】定格：人物姿态/手部/表情/道具必须清楚呈现动作完成后的结果，与本镜首图明显不同，切勿画成与首图相同的画面"
        if i > 0 and prev_issues:
            # 仅返工时以“上一版同类帧”为基底定向改进（此处复制是期望行为）
            extra += "。在上一版基础上，逐条改正以下问题、其余保持不变：" + "；".join(prev_issues[:5])
            if prev_img_path:
                refs = [hiagent.data_url_from_file(prev_img_path)] + char_refs
        prompt = base_prompt + extra
        conn.execute(
            "INSERT INTO shot_scenes(id, shot_id, version_no, kind, prompt_text, status, created_at) "
            "VALUES(?,?,?,?,?, 'running', ?)", (sid, job["shot_id"], vno, kind, prompt, now()))
        conn.commit()
        try:
            await _generate_one_scene(prompt, refs, dest)
            qa = await review_scene_image(
                hiagent.encode_image_file(str(dest)), frame_desc, shot["scene_setting"],
                anchors, prev_image_b64=comparison_b64, kind=kind)
            conn.execute("UPDATE shot_scenes SET status='succeeded', image_path=?, qa_json=? WHERE id=?",
                         (str(dest), json.dumps(qa, ensure_ascii=False), sid))
            conn.commit()
            score = float(qa.get("overall", -1))
            candidates.append((sid, score, str(dest), qa))
            if score >= threshold:
                break
            prev_img_path = str(dest)
            prev_issues = list(qa.get("issues") or [])
        except Exception as exc:  # noqa: BLE001 单次失败不拖垮整轮
            conn.execute("UPDATE shot_scenes SET status='failed', error=? WHERE id=?", (str(exc)[:500], sid))
            conn.commit()

    ok = [c for c in candidates if c[1] >= 0]
    if not ok:
        return None, -1
    best_id, best_score, _, _ = max(ok, key=lambda c: c[1])
    return best_id, best_score


async def _run_scene_job(job) -> None:
    """生成本镜所需关键帧：场景起始镜生成首图+尾图，连续镜只生成尾图。"""
    from app.refs import refs_as_image_inputs
    from app.schemas import Bible
    conn = get_conn()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (job["shot_id"],)).fetchone()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (job["episode_id"],)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    try:
        bible = Bible.model_validate(json.loads(project["bible_json"]))
        threshold = float(get_setting("scene_qa_threshold") or 0.6)
        char_names = json.loads(shot["characters"] or "[]")
        char_refs = [u for u, _ in refs_as_image_inputs(bible, char_names, int(get_setting("max_ref_images") or 2))]
        anchors = [c.appearance_canonical for c in bible.characters if c.name in char_names]

        requested = None
        raw_kinds = _row_value(job, "scene_kinds")
        if raw_kinds:
            requested = json.loads(raw_kinds)
        required = scene_generation_kinds(shot, requested)
        best: dict[str, tuple[str, float]] = {}
        head_scene = None

        if KEYFRAME_HEAD in required:
            head_id, head_score = await _generate_keyframe_candidates(
                conn=conn, job=job, shot=shot, ep=ep, project=project, bible=bible, kind=KEYFRAME_HEAD,
                char_refs=char_refs, anchors=anchors, comparison_path=None, comparison_b64=None)
            if not head_id:
                raise ProviderError("首图候选生成/评审失败")
            best[KEYFRAME_HEAD] = (head_id, head_score)
            head_scene = conn.execute("SELECT * FROM shot_scenes WHERE id=?", (head_id,)).fetchone()

        comparison_path = None
        comparison_b64 = None
        if head_scene and head_scene["image_path"]:
            comparison_path = head_scene["image_path"]
            comparison_b64 = hiagent.encode_image_file(comparison_path)
        elif shot["continuity_from_prev"]:
            prev = conn.execute(
                "SELECT * FROM shots WHERE episode_id=? AND shot_no=?",
                (shot["episode_id"], shot["shot_no"] - 1)).fetchone()
            prev_tail = _approved_keyframe(conn, prev, KEYFRAME_TAIL)
            if prev_tail:
                comparison_path = prev_tail["image_path"]
                comparison_b64 = hiagent.encode_image_file(comparison_path)
        elif KEYFRAME_HEAD not in required:
            existing_head = _approved_keyframe(conn, shot, KEYFRAME_HEAD)
            if existing_head and existing_head["image_path"]:
                comparison_path = existing_head["image_path"]
                comparison_b64 = hiagent.encode_image_file(comparison_path)

        if KEYFRAME_TAIL in required:
            tail_id, tail_score = await _generate_keyframe_candidates(
                conn=conn, job=job, shot=shot, ep=ep, project=project, bible=bible, kind=KEYFRAME_TAIL,
                char_refs=char_refs, anchors=anchors, comparison_path=comparison_path, comparison_b64=comparison_b64)
            if not tail_id:
                raise ProviderError("尾图候选生成/评审失败")
            best[KEYFRAME_TAIL] = (tail_id, tail_score)

        if any(kind not in best for kind in required):
            conn.execute("UPDATE shots SET scene_status='review' WHERE id=?", (job["shot_id"],))
            conn.commit()
            _set_job(job["id"], "failed", "关键帧生成/评审失败，请重试或手动处理")
            return

        if KEYFRAME_HEAD in best:
            conn.execute("UPDATE shots SET approved_head_scene_id=? WHERE id=?", (best[KEYFRAME_HEAD][0], job["shot_id"]))
        if KEYFRAME_TAIL in best:
            conn.execute(
                "UPDATE shots SET approved_tail_scene_id=?, approved_scene_id=? WHERE id=?",
                (best[KEYFRAME_TAIL][0], best[KEYFRAME_TAIL][0], job["shot_id"]))
        refreshed = conn.execute("SELECT * FROM shots WHERE id=?", (job["shot_id"],)).fetchone()
        auto_passed = all(score >= threshold for _, score in best.values())
        scene_status = "approved" if auto_passed and shot_keyframes_ready(refreshed) else "review"
        conn.execute("UPDATE shots SET scene_status=? WHERE id=?", (scene_status, job["shot_id"]))
        conn.commit()
        _set_job(job["id"], "succeeded")
    except Exception as exc:  # noqa: BLE001
        conn.execute("UPDATE shots SET scene_status='review' WHERE id=?", (job["shot_id"],))
        conn.commit()
        _set_job(job["id"], "failed", f"关键帧生成失败：{exc}")


async def critique_version(version_id: str) -> list[str]:
    """取某视频版本的问题清单（AI 评语）：优先用已存的 QA issues；
    若该版本还没质检过，则现场抽帧跑一次 VLM 评审，并回存。供「带评语重生」避免重复犯错。"""
    conn = get_conn()
    v = conn.execute("SELECT * FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        return []
    if v["qa_json"]:
        issues = (json.loads(v["qa_json"]) or {}).get("issues") or []
        if issues:
            return list(issues)
    if not v["video_path"] or not Path(v["video_path"]).exists():
        return []
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (v["shot_id"],)).fetchone()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (shot["episode_id"],)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    try:
        from app.stages import qa_shot
        bible = json.loads(project["bible_json"])
        anchor_map = {c["name"]: c["appearance_canonical"] for c in bible["characters"]}
        anchors = [anchor_map[n] for n in json.loads(shot["characters"] or "[]") if n in anchor_map]
        frames = _extract_frames(v["video_path"])
        if not frames:
            return []
        qa = await qa_shot(frames, shot["action_desc"], shot["scene_setting"], anchors)
        _set_version(version_id, qa_json=json.dumps(qa, ensure_ascii=False))
        return list(qa.get("issues") or [])
    except Exception:  # noqa: BLE001 评语失败不阻塞重生
        return []


# ---------- 执行 ----------

def _set_job(job_id: str, status: str, error: str | None = None) -> None:
    conn = get_conn()
    conn.execute("UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?", (status, error, now(), job_id))
    conn.commit()


def _set_version(version_id: str, **fields) -> None:
    conn = get_conn()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE shot_versions SET {cols} WHERE id=?", (*fields.values(), version_id))
    conn.commit()


def _is_seedance_text_sensitive(message: str | None) -> bool:
    text = (message or "").lower()
    return (
        "inputtextsensitivecontentdetected" in text
        or "sensitive information" in text
        or "sensitive content" in text
        or "输入文本" in (message or "")
        or "敏感" in (message or "")
    )


_SEEDANCE_COPYRIGHT_MAX_RETRIES = 2


def _is_seedance_copyright_restricted(message: str | None) -> bool:
    text = (message or "").lower()
    return "copyright" in text or "版权" in (message or "")


def _ip_genericization_terms(conn, project_id: str) -> tuple[tuple[str, str], ...]:
    """把版权角色专名替换成中性代称（角色甲/乙…），降低 Seedance 输出版权误判概率。
    仅在平台已返回版权限制后的自动重提里使用。"""
    project = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project or not project["bible_json"]:
        return ()
    try:
        chars = json.loads(project["bible_json"]).get("characters", [])
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    labels = "甲乙丙丁戊己庚辛壬癸"
    names = sorted({(c.get("name") or "").strip() for c in chars if (c.get("name") or "").strip()},
                   key=len, reverse=True)  # 先长后短，避免短名先替换截断长名
    return tuple((name, f"角色{labels[i]}" if i < len(labels) else f"角色{i + 1}")
                 for i, name in enumerate(names))


def _video_image_inputs_from_meta(meta: dict) -> list[tuple[str, str]]:
    if meta.get("mode") in (video_modes.FIRST_LAST_FRAME_MODE, video_modes.REFERENCE_IMAGE_MODE):
        return video_modes.build_seedance_image_inputs(meta)
    image_inputs: list[tuple[str, str]] = []
    first_path = meta.get("first_frame_path")
    last_path = meta.get("last_frame_path")
    if not first_path:
        raise ProviderError("视频首图缺失，请重新生成关键帧后再生成视频")
    if not last_path:
        raise ProviderError("视频尾图缺失，请重新生成关键帧后再生成视频")
    try:
        image_inputs.append((hiagent.data_url_from_file(first_path), "first_frame"))
    except OSError:
        raise ProviderError("视频首图文件丢失，请重新生成关键帧后再生成视频")
    try:
        image_inputs.append((hiagent.data_url_from_file(last_path), "last_frame"))
    except OSError:
        raise ProviderError("视频尾图文件丢失，请重新生成关键帧后再生成视频")
    return image_inputs


async def _prepare_reference_mode_inputs(conn, job, version, shot, ep, meta: dict, prompt_text: str) -> tuple[dict, str]:
    if meta.get("mode") != video_modes.REFERENCE_IMAGE_MODE:
        return meta, prompt_text
    if meta.get("reference_images"):
        return meta, prompt_text
    from app.schemas import Bible

    project = conn.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    shot_model = _load_shot_model(shot)
    prev_shot = conn.execute("SELECT * FROM shots WHERE id=?", (meta.get("after_shot_id"),)).fetchone() if meta.get("after_shot_id") else None
    # 复用入队时已确定的模式决策，不在生成时再跑一次 LLM 选择：既省每镜一次文本调用，
    # 又避免模式在入队与执行之间无谓翻转（决策应在入队时一次定死）。
    decision = video_modes.dict_to_decision(meta.get("mode_decision") or {})
    if decision.mode == video_modes.FIRST_LAST_FRAME_MODE:
        first_scene, first_src, _ = _first_keyframe_for_video(conn, shot, meta.get("after_shot_id"))
        last_scene = _approved_keyframe(conn, shot, KEYFRAME_TAIL)
        if first_scene and last_scene:
            from app.compiler import compile_prompt

            meta["mode"] = video_modes.FIRST_LAST_FRAME_MODE
            meta["mode_decision"] = video_modes.decision_to_dict(decision)
            meta["fallback_reason"] = "LLM selector switched to FIRST_LAST_FRAME_MODE before Seedance call."
            meta.update({
                "first_frame_scene_id": first_scene["id"],
                "first_frame_path": first_scene["image_path"],
                "first_frame_src": first_src,
                "last_frame_scene_id": last_scene["id"],
                "last_frame_path": last_scene["image_path"],
                "last_frame_src": "tail_keyframe",
            })
            outgoing = meta.get("outgoing_transition") or {}
            prompt_text = ensure_source_excerpt_in_prompt(
                compile_prompt(
                    shot_model, bible,
                    from_scene=not bool(meta.get("after_shot_id")),
                    chained=bool(meta.get("after_shot_id")),
                    with_last_frame=True,
                    incoming_transition=meta.get("incoming_transition"),
                    outgoing_transition=outgoing.get("transition") if isinstance(outgoing, dict) else None,
                    next_scene=outgoing.get("next_scene") if isinstance(outgoing, dict) else None,
                    next_first_frame_desc=outgoing.get("next_first_frame_desc") if isinstance(outgoing, dict) else None,
                ),
                shot_model,
            )
            _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False))
            return meta, prompt_text
        # 入队时定的是首尾帧模式，但执行时本镜已无可用首/尾关键帧（极少见，例如关键帧被删）。
        # 不能硬失败——降级到参考图模式，由参考图自行生成视频。
        if decision.mode != video_modes.REFERENCE_IMAGE_MODE or decision.referenceImagePlan.totalCount <= 0:
            decision.mode = video_modes.REFERENCE_IMAGE_MODE
            if decision.referenceImagePlan.totalCount <= 0:
                decision.referenceImagePlan = video_modes.ReferenceImagePlan()
            decision.needGenerateNewReferences = decision.referenceImagePlan.generateNewCount > 0
        meta["mode"] = video_modes.REFERENCE_IMAGE_MODE
        meta["fallback_reason"] = (
            "First/last frame mode had no approved keyframes at execution time; "
            "fell back to reference image mode."
        )

    assets = await video_modes.build_reference_assets(
        conn=conn, project_id=job["project_id"], episode_no=ep["episode_no"], episode_id=job["episode_id"],
        shot_id=job["shot_id"], shot=shot_model, bible=bible, decision=decision, prev_shot=prev_shot)
    if not assets:
        from app.compiler import compile_prompt

        meta["reference_failure_logs"] = (meta.get("reference_failure_logs") or []) + [{
            "mode": video_modes.REFERENCE_IMAGE_MODE,
            "reason": "No quality-approved reference images.",
            "prompt": prompt_text[:500],
        }]
        meta["mode"] = video_modes.FIRST_LAST_FRAME_MODE
        meta["fallback_reason"] = "Reference image preparation produced no approved assets."
        first_scene, first_src, _ = _first_keyframe_for_video(conn, shot, meta.get("after_shot_id"))
        last_scene = _approved_keyframe(conn, shot, KEYFRAME_TAIL)
        if not first_scene or not last_scene:
            raise ProviderError("Reference image mode failed and first/last fallback keyframes are not ready.")
        meta.update({
            "first_frame_scene_id": first_scene["id"],
            "first_frame_path": first_scene["image_path"],
            "first_frame_src": first_src,
            "last_frame_scene_id": last_scene["id"],
            "last_frame_path": last_scene["image_path"],
            "last_frame_src": "tail_keyframe",
        })
        outgoing = meta.get("outgoing_transition") or {}
        prompt_text = ensure_source_excerpt_in_prompt(
            compile_prompt(
                shot_model, bible,
                from_scene=not bool(meta.get("after_shot_id")),
                chained=bool(meta.get("after_shot_id")),
                with_last_frame=True,
                incoming_transition=meta.get("incoming_transition"),
                outgoing_transition=outgoing.get("transition") if isinstance(outgoing, dict) else None,
                next_scene=outgoing.get("next_scene") if isinstance(outgoing, dict) else None,
                next_first_frame_desc=outgoing.get("next_first_frame_desc") if isinstance(outgoing, dict) else None,
            ),
            shot_model,
        )
    else:
        meta["mode"] = video_modes.REFERENCE_IMAGE_MODE
        meta["mode_decision"] = video_modes.decision_to_dict(decision)
        meta["reference_images"] = [a.public_dict() for a in assets]
        meta.pop("first_frame_path", None)
        meta.pop("last_frame_path", None)
        meta.pop("first_frame_scene_id", None)
        meta.pop("last_frame_scene_id", None)
        prompt_text = video_modes.append_reference_prompt_notes(prompt_text, assets)
    _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False), prompt_text=prompt_text)
    return meta, prompt_text


async def _run_job(job_id: str) -> None:
    conn = get_conn()
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or job["status"] not in ("queued", "running"):
        return
    if job["kind"] == "scene":
        _set_job(job_id, "running")
        await _run_scene_job(job)
        return
    version = conn.execute("SELECT * FROM shot_versions WHERE id=?", (job["version_id"],)).fetchone()
    shot = conn.execute("SELECT * FROM shots WHERE id=?", (job["shot_id"],)).fetchone()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (job["episode_id"],)).fetchone()

    # 首尾关键帧：视频不再等待上一镜成片，也不从视频抽尾帧。
    meta = json.loads(version["image_inputs"] or "{}")
    after_shot_id = meta.get("after_shot_id")

    if _budget_exceeded(job["episode_id"]):
        _set_job(job_id, "paused_budget", f"本集成本已达上限 ¥{get_setting('episode_cost_limit_cny')}，队列暂停。可在设置中调高后重试")
        _set_version(version["id"], status="paused_budget")
        return

    _set_job(job_id, "running")
    started = time.time()
    try:
        task_id = version["provider_task_id"]
        result = None
        _set_version(version["id"], status="running")
        prompt_text = ensure_source_excerpt_in_prompt(version["prompt_text"], _load_shot_model(shot))
        if prompt_text != version["prompt_text"]:
            _set_version(version["id"], prompt_text=prompt_text)
        meta, prompt_text = await _prepare_reference_mode_inputs(conn, job, version, shot, ep, meta, prompt_text)
        safety_retry_used = bool(meta.get("seedance_safety_retry"))
        copyright_retries = int(meta.get("seedance_copyright_retries") or 0)
        image_inputs: list[tuple[str, str]] | None = None

        while True:
            if not task_id:  # 重启恢复时可能已有 task_id，直接续轮询
                if image_inputs is None:
                    # first_frame + last_frame 均来自已过审关键图；缺任一张即失败，不做艺术兜底替换。
                    image_inputs = _video_image_inputs_from_meta(meta)
                    if meta.get("mode") == video_modes.REFERENCE_IMAGE_MODE:
                        meta["reference_image_used"] = bool(image_inputs)
                        meta["first_frame_used"] = False
                        meta["last_frame_used"] = False
                    else:
                        meta["first_frame_used"] = bool(image_inputs)
                        meta["last_frame_used"] = any(role == "last_frame" for _, role in image_inputs)
                    _set_version(version["id"], image_inputs=json.dumps(meta, ensure_ascii=False))
                try:
                    task_id = await hiagent.create_video_task(prompt_text, image_urls=image_inputs)
                except ProviderError as exc:
                    if _is_seedance_text_sensitive(str(exc)) and not safety_retry_used:
                        prompt_text = sanitize_seedance_prompt(prompt_text, aggressive=True)
                        safety_retry_used = True
                        meta["seedance_safety_retry"] = True
                        meta["seedance_safety_reason"] = str(exc)[:300]
                        _set_version(version["id"], prompt_text=prompt_text, provider_task_id=None,
                                     image_inputs=json.dumps(meta, ensure_ascii=False))
                        continue
                    if _is_seedance_copyright_restricted(str(exc)) and copyright_retries < _SEEDANCE_COPYRIGHT_MAX_RETRIES:
                        copyright_retries += 1
                        if copyright_retries == 1:
                            prompt_text = sanitize_seedance_prompt(
                                prompt_text, aggressive=True,
                                extra_terms=_ip_genericization_terms(conn, job["project_id"]))
                        meta["seedance_copyright_retries"] = copyright_retries
                        meta["seedance_copyright_reason"] = str(exc)[:300]
                        _set_version(version["id"], prompt_text=prompt_text, provider_task_id=None,
                                     image_inputs=json.dumps(meta, ensure_ascii=False))
                        continue
                    raise
                _set_version(version["id"], provider_task_id=task_id)

            deadline = time.time() + config.VIDEO_POLL_BUDGET
            result = None
            while time.time() < deadline:
                result = await hiagent.poll_video_task(task_id)
                if result["status"] in ("succeeded", "failed"):
                    break
                await asyncio.sleep(config.VIDEO_POLL_INTERVAL)
            if result is None or result["status"] not in ("succeeded", "failed"):
                raise ProviderError(f"轮询超出 {config.VIDEO_POLL_BUDGET // 60} 分钟预算，任务 {task_id} 仍未完成；可稍后对该镜头重试")
            if result["status"] == "failed":
                error_text = result["error"][:400]
                if _is_seedance_text_sensitive(error_text) and not safety_retry_used:
                    prompt_text = sanitize_seedance_prompt(prompt_text, aggressive=True)
                    safety_retry_used = True
                    task_id = None
                    meta["seedance_safety_retry"] = True
                    meta["seedance_safety_reason"] = error_text
                    _set_version(version["id"], prompt_text=prompt_text, provider_task_id=None,
                                 image_inputs=json.dumps(meta, ensure_ascii=False))
                    continue
                if _is_seedance_copyright_restricted(error_text) and copyright_retries < _SEEDANCE_COPYRIGHT_MAX_RETRIES:
                    copyright_retries += 1
                    if copyright_retries == 1:  # 首次重提：去掉版权专名 + 激进改写，降低输出与原 IP 相似度
                        prompt_text = sanitize_seedance_prompt(
                            prompt_text, aggressive=True,
                            extra_terms=_ip_genericization_terms(conn, job["project_id"]))
                    task_id = None  # 再次重提靠重新生成的随机性（同一镜其它版本可成功即说明判定是概率性的）
                    meta["seedance_copyright_retries"] = copyright_retries
                    meta["seedance_copyright_reason"] = error_text
                    _set_version(version["id"], prompt_text=prompt_text, provider_task_id=None,
                                 image_inputs=json.dumps(meta, ensure_ascii=False))
                    continue
                raise ProviderError(f"Seedance 任务失败：{error_text}")
            break

        dest = _video_path(job["project_id"], ep["episode_no"], shot["shot_no"], version["version_no"])
        await hiagent.download(result["video_url"], str(dest))
        latency = round(time.time() - started, 1)
        cost = shot_cost_cny(shot["duration_s"])
        _set_version(version["id"], status="succeeded", video_path=str(dest),
                     last_frame_url=result["last_frame_url"], cost_cny=cost, latency_s=latency)
        _set_job(job_id, "succeeded")
        # 无已采用版本时自动采用本次成功版本
        conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=? AND adopted_version_id IS NULL",
                     (version["id"], job["shot_id"]))
        conn.commit()
        # 评审墙产生了新片段，旧的整集合成视频即过期 → 删除，避免成片台展示陈旧成品
        _invalidate_final_video(job["project_id"], ep["episode_no"])
        await _maybe_auto_qa(job, version["id"], str(dest))
    except (ProviderError, Exception) as exc:  # noqa: BLE001 失败要响：原样透出
        message = str(exc)[:500]
        _set_version(version["id"], status="failed", error=message)
        _set_job(job_id, "failed", message)


def _promote_if_better_qa(job, version_id: str, qa: dict) -> None:
    """自动模式下，若本版质检分高于当前采用版，则改采用本版。
    否则首版成功即占住 adopted_version_id，QA 触发的重生版永远不会被采用——重生纯属花钱不见效。
    仅在分数严格更高（或当前采用版没有有效质检分）时切换，并使旧成片失效。"""
    conn = get_conn()
    v = conn.execute("SELECT status FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v or v["status"] != "succeeded":
        return
    new_overall = qa.get("overall", -1)
    if new_overall is None or new_overall < 0:
        return
    shot = conn.execute("SELECT adopted_version_id FROM shots WHERE id=?", (job["shot_id"],)).fetchone()
    adopted = shot["adopted_version_id"] if shot else None
    if adopted == version_id:
        return
    if not adopted:
        conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=? AND adopted_version_id IS NULL",
                     (version_id, job["shot_id"]))
        conn.commit()
        return
    av = conn.execute("SELECT qa_json FROM shot_versions WHERE id=?", (adopted,)).fetchone()
    try:
        cur_overall = (json.loads(av["qa_json"]) or {}).get("overall", -1) if av and av["qa_json"] else -1
    except (TypeError, ValueError, json.JSONDecodeError):
        cur_overall = -1
    if new_overall > cur_overall:
        conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, job["shot_id"]))
        conn.commit()
        ep = conn.execute("SELECT episode_no FROM episodes WHERE id=?", (job["episode_id"],)).fetchone()
        if ep:
            _invalidate_final_video(job["project_id"], ep["episode_no"])


async def _maybe_auto_qa(job, version_id: str, video_path: str) -> None:
    """自动质检 + 一次自动重抽（QA 失败不阻塞流程，只标记未质检）。"""
    if get_setting("auto_qa") != "true" or not shutil.which("ffmpeg"):
        return
    conn = get_conn()
    try:
        frames = _extract_frames(video_path)
        shot = conn.execute("SELECT * FROM shots WHERE id=?", (job["shot_id"],)).fetchone()
        project = conn.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
        bible = json.loads(project["bible_json"])
        anchor_map = {c["name"]: c["appearance_canonical"] for c in bible["characters"]}
        anchors = [anchor_map[n] for n in json.loads(shot["characters"] or "[]") if n in anchor_map]
        from app.stages import qa_shot
        qa = await qa_shot(frames, shot["action_desc"], shot["scene_setting"], anchors)
        _set_version(version_id, qa_json=json.dumps(qa, ensure_ascii=False))
        # 重生版若质检分更高，立即提升为采用版（避免更差的首版长期占位）
        _promote_if_better_qa(job, version_id, qa)
        threshold = float(get_setting("auto_retake_threshold") or 0.6)
        version = conn.execute("SELECT * FROM shot_versions WHERE id=?", (version_id,)).fetchone()
        meta = json.loads(version["image_inputs"] or "{}") if version else {}
        if 0 <= qa.get("overall", -1) < threshold and meta.get("mode") == video_modes.REFERENCE_IMAGE_MODE:
            logs = meta.get("reference_failure_logs") or []
            logs.append({
                "mode": video_modes.REFERENCE_IMAGE_MODE,
                "reason": "Video QA failed after reference image mode.",
                "reference_images": meta.get("reference_images") or [],
                "prompt": version["prompt_text"][:500],
                "qa": qa,
            })
            meta["reference_failure_logs"] = logs
            rows = conn.execute("SELECT image_inputs, qa_json FROM shot_versions WHERE shot_id=?", (job["shot_id"],)).fetchall()
            failures = 0
            for row in rows:
                row_meta = json.loads(row["image_inputs"] or "{}")
                if row_meta.get("mode") != video_modes.REFERENCE_IMAGE_MODE:
                    continue
                row_qa = json.loads(row["qa_json"] or "{}")
                if row_qa.get("overall", 1) < threshold:
                    failures += 1
            force_first_last = failures >= video_modes.fallback_failure_threshold()
            action_issue = any(x in " ".join(qa.get("issues") or []) for x in ["落点", "结尾", "动作", "end", "landing"])
            if force_first_last or action_issue:
                meta["fallback_reason"] = "Reference image mode failed repeatedly or action endpoint mismatched; fallback to first/last frame mode."
                _set_version(version_id, image_inputs=json.dumps(meta, ensure_ascii=False))
                enqueue_shot(job["shot_id"], extra_negative=qa.get("issues", [])[:3],
                             after_shot_id=job["after_shot_id"],
                             mode_override=video_modes.FIRST_LAST_FRAME_MODE)
            else:
                meta["retry_reason"] = "Reference image mode character/quality QA failed; retry with reselected references."
                _set_version(version_id, image_inputs=json.dumps(meta, ensure_ascii=False))
                enqueue_shot(job["shot_id"], extra_negative=qa.get("issues", [])[:3],
                             reroll=True, after_shot_id=job["after_shot_id"],
                             mode_override=video_modes.REFERENCE_IMAGE_MODE)
            return
        if 0 <= qa.get("overall", -1) < threshold and version["version_no"] == 1 and qa.get("issues"):
            enqueue_shot(job["shot_id"], extra_negative=qa["issues"][:3],
                         after_shot_id=job["after_shot_id"])
    except Exception as exc:  # noqa: BLE001 QA 异常只记录，不影响已落盘的视频
        _set_version(version_id, qa_json=json.dumps({"overall": -1, "issues": [f"质检未完成：{exc}"]}, ensure_ascii=False))
        log_provider_call("vlm_qa", config.MODEL_VLM, "QA_ERROR", None, 0, error=str(exc))


def _extract_frames(video_path: str) -> list[str]:
    """ffmpeg 抽 首/中/尾 3 帧，返回 base64 列表。"""
    frames = []
    with tempfile.TemporaryDirectory() as td:
        for i, pos in enumerate(("0", "50%", "99%")):
            out = Path(td) / f"f{i}.jpg"
            vf = "select=eq(n\\,0)" if pos == "0" else None
            cmd = ["ffmpeg", "-y", "-loglevel", "error"]
            if pos == "0":
                cmd += ["-i", video_path, "-vf", "select=eq(n\\,0)", "-vframes", "1"]
            else:
                # 用 ffprobe 拿时长再定位
                dur = float(subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path],
                    capture_output=True, text=True, check=True).stdout.strip() or 5)
                ts = dur * (0.5 if pos == "50%" else 0.97)
                cmd += ["-ss", f"{ts:.2f}", "-i", video_path, "-vframes", "1"]
            cmd += ["-q:v", "4", str(out)]
            subprocess.run(cmd, check=True, capture_output=True)
            frames.append(hiagent.encode_image_file(str(out)))
    return frames


# ---------- worker 生命周期 ----------

async def _worker_loop(name: str) -> None:
    while True:
        job_id = await _queue.get()
        try:
            await _run_job(job_id)
        except Exception as exc:  # noqa: BLE001 worker 永不死亡，但错误必须落库
            _set_job(job_id, "failed", f"worker 异常：{exc}")
        finally:
            _queue.task_done()


def recover_and_start(loop_concurrency: int | None = None) -> None:
    """启动时恢复队列（PRD §4.5 验收：中途杀进程重启后队列状态可恢复）。"""
    conn = get_conn()
    rows = conn.execute("SELECT id FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at").fetchall()
    for r in rows:
        _queue.put_nowait(r["id"])
    n = loop_concurrency or int(get_setting("video_concurrency") or 2)
    for i in range(n):
        _workers.append(asyncio.get_running_loop().create_task(_worker_loop(f"w{i}")))


def ensure_workers(n: int) -> None:
    """把常驻 worker 池扩容到至少 n 个（只增不减）。一键全自动会按 auto_concurrency 调大，
    让大量关键帧/视频任务并行消费同一队列；空闲 worker 只是 await 队列，不占资源。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    while len(_workers) < max(n, 0):
        _workers.append(loop.create_task(_worker_loop(f"w{len(_workers)}")))


def worker_count() -> int:
    return len(_workers)


async def stop() -> None:
    """优雅停机：取消常驻 worker 循环。否则 uvicorn --reload/退出时会卡在
    'Waiting for connections to close'——常驻 while-True 任务不退出，停机就挂起。"""
    for t in _workers:
        t.cancel()
    for t in _workers:
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _workers.clear()


def retry_paused(episode_id: str) -> int:
    """成本上限调高后，恢复因预算暂停的任务。"""
    conn = get_conn()
    rows = conn.execute("SELECT id FROM jobs WHERE episode_id=? AND status='paused_budget'", (episode_id,)).fetchall()
    for r in rows:
        conn.execute("UPDATE jobs SET status='queued', error=NULL, updated_at=? WHERE id=?", (now(), r["id"]))
        _queue.put_nowait(r["id"])
    conn.commit()
    return len(rows)


# ---------- 成片台：汇总状态 / 拼接 / 导出 ----------

def episode_mix_status(episode_id: str) -> dict:
    """返回：每镜是否已有成片（采用版），以及整体状态。"""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        return {"ready": False, "shots_total": 0, "shots_ready": 0, "shots": []}
    shots = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall())
    ready = 0
    out = []
    for s in shots:
        vid = None
        if s["adopted_version_id"]:
            v = conn.execute(
                "SELECT * FROM shot_versions WHERE id=? AND status='succeeded'",
                (s["adopted_version_id"],)).fetchone()
            if v and v["video_path"]:
                from app.config import PROJECTS_DIR
                rel_path = Path(v["video_path"]).relative_to(PROJECTS_DIR).as_posix()
                vid = f"/media/{rel_path}"
                ready += 1
        out.append({"shot_id": s["id"], "shot_no": s["shot_no"],
                    "duration_s": s["duration_s"], "video_url": vid,
                    "has_adopted": bool(vid)})
    return {
        "episode_id": ep["id"],
        "title": ep["title"],
        "episode_no": ep["episode_no"],
        "shots_total": len(shots),
        "shots_ready": ready,
        "ready": len(shots) > 0 and ready == len(shots),
        "final_video_url": _existing_final_url(ep),
        "shots": out,
    }


def _existing_final_url(ep_row) -> str | None:
    from app.config import PROJECTS_DIR
    final_path = _final_video_path(ep_row["project_id"], ep_row["episode_no"])
    if final_path.exists():
        rel_path = final_path.relative_to(PROJECTS_DIR).as_posix()
        return f"/media/{rel_path}"
    return None


def _final_video_path(project_id: str, episode_no: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "final"
    d.mkdir(parents=True, exist_ok=True)
    return d / "episode.mp4"


def _delete_version_files(video_path: str | None) -> None:
    """删除版本视频及旧链路遗留的缓存尾帧。"""
    if not video_path:
        return
    p = Path(video_path)
    for f in (p, Path(str(p.with_suffix("")) + "_last.jpg")):
        try:
            f.unlink()
        except OSError:
            pass


def _purge_shots(conn, shots: list[dict]) -> tuple[int, set[str]]:
    """删除给定镜头的全部版本、关键帧、任务与采用标记。
    返回 (删除版本数, 受影响剧集 id 集合)。"""
    versions_removed = 0
    affected_eps: set[str] = set()
    for s in shots:
        affected_eps.add(s["episode_id"])
        versions = conn.execute(
            "SELECT id, video_path FROM shot_versions WHERE shot_id=?", (s["id"],)).fetchall()
        for v in versions:
            _delete_version_files(v["video_path"])
        scenes = conn.execute("SELECT image_path FROM shot_scenes WHERE shot_id=?", (s["id"],)).fetchall()
        for sc in scenes:
            if sc["image_path"]:
                try:
                    Path(sc["image_path"]).unlink()
                except OSError:
                    pass
        conn.execute("DELETE FROM shot_versions WHERE shot_id=?", (s["id"],))
        conn.execute("DELETE FROM shot_scenes WHERE shot_id=?", (s["id"],))
        conn.execute("DELETE FROM jobs WHERE shot_id=?", (s["id"],))
        conn.execute(
            "UPDATE shots SET adopted_version_id=NULL, approved_scene_id=NULL, "
            "approved_head_scene_id=NULL, approved_tail_scene_id=NULL, scene_status='none' WHERE id=?",
            (s["id"],))
        versions_removed += len(versions)
    return versions_removed, affected_eps


def _rollback_episodes(conn, ep_ids: set[str]) -> None:
    for ep_id in ep_ids:
        ep = conn.execute(
            "SELECT project_id, episode_no FROM episodes WHERE id=?", (ep_id,)).fetchone()
        if ep:
            _invalidate_final_video(ep["project_id"], ep["episode_no"])
        conn.execute("UPDATE episodes SET status='confirmed' WHERE id=? AND status IN ('generating','done')", (ep_id,))


def purge_character_video_artifacts(project_id: str, character_names: list[str]) -> dict:
    """角色定妆照重做后，清理所有用到该角色的镜头已生成产物：
    评审墙关键帧、各版本视频、相关任务、整集成品，并把对应剧集回退到“已确认”，
    强制后续基于新定妆照重新生成，避免新旧画风/形象混用。"""
    targets = {n for n in character_names if n}
    if not targets:
        return {"shots": 0, "versions": 0, "episodes": 0}
    conn = get_conn()
    shots = rows_to_dicts(conn.execute(
        """SELECT s.id, s.episode_id, s.characters
           FROM shots s JOIN episodes e ON e.id = s.episode_id
           WHERE e.project_id = ?""", (project_id,)).fetchall())
    affected_shots = [s for s in shots if set(json.loads(s["characters"] or "[]")) & targets]
    versions_removed, affected_eps = _purge_shots(conn, affected_shots)
    _rollback_episodes(conn, affected_eps)
    conn.commit()
    return {"shots": len(affected_shots), "versions": versions_removed, "episodes": len(affected_eps)}


def delete_project_episodes(project_id: str) -> int:
    """重新分集时整体清空本项目所有剧集及其衍生数据（镜头/版本/视频/任务/成片目录）。
    旧逻辑只删 status='planned' 的剧集，导致已进入分镜/确认的旧集残留、与新集 episode_no 撞号，
    前端就出现“同一集号有两三条、剧情重复”。重新分集应是干净替换。"""
    conn = get_conn()
    eps = conn.execute("SELECT id, episode_no FROM episodes WHERE project_id=?", (project_id,)).fetchall()
    shots = rows_to_dicts(conn.execute(
        "SELECT s.id, s.episode_id FROM shots s JOIN episodes e ON e.id=s.episode_id WHERE e.project_id=?",
        (project_id,)).fetchall())
    _purge_shots(conn, shots)  # 删版本文件/尾帧、jobs、清采用标记
    conn.execute("DELETE FROM shots WHERE episode_id IN (SELECT id FROM episodes WHERE project_id=?)", (project_id,))
    conn.execute("DELETE FROM episodes WHERE project_id=?", (project_id,))
    conn.commit()
    ep_dir = config.PROJECTS_DIR / project_id / "episodes"
    if ep_dir.exists():
        shutil.rmtree(ep_dir, ignore_errors=True)
    return len(eps)


def delete_episode_shots(episode_id: str) -> int:
    """清空单集分镜及其衍生产物。用于剧本重生/编辑后让下游重新展开。"""
    conn = get_conn()
    ep = conn.execute("SELECT project_id, episode_no FROM episodes WHERE id=?", (episode_id,)).fetchone()
    shots = rows_to_dicts(conn.execute(
        "SELECT id, episode_id FROM shots WHERE episode_id=?", (episode_id,)).fetchall())
    _purge_shots(conn, shots)
    conn.execute("DELETE FROM shots WHERE episode_id=?", (episode_id,))
    conn.execute("DELETE FROM jobs WHERE episode_id=?", (episode_id,))
    conn.execute(
        "UPDATE episodes SET status='planned', script_error=NULL WHERE id=? AND status NOT IN ('planned','scripting','script_failed')",
        (episode_id,))
    conn.commit()
    if ep:
        _invalidate_final_video(ep["project_id"], ep["episode_no"])
    return len(shots)


def purge_project_video_artifacts(project_id: str) -> dict:
    """画风切换后全项目作废：旧画风的定妆照、旧关键帧与旧视频
    是比文字 prompt 更强的画风信号，残留任何一环都会把新画风拉回旧画风，必须整体清理。"""
    conn = get_conn()
    shots = rows_to_dicts(conn.execute(
        """SELECT s.id, s.episode_id FROM shots s JOIN episodes e ON e.id = s.episode_id
           WHERE e.project_id = ?""", (project_id,)).fetchall())
    versions_removed, affected_eps = _purge_shots(conn, shots)
    _rollback_episodes(conn, affected_eps)
    conn.commit()
    return {"shots": len(shots), "versions": versions_removed, "episodes": len(affected_eps)}


def delete_video_version(version_id: str) -> str | None:
    """删除单个视频版本（含视频/尾帧文件、相关任务）；若是采用版则清空采用并使该集成品失效。
    返回所属 shot_id。"""
    conn = get_conn()
    v = conn.execute("SELECT * FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        return None
    shot_id = v["shot_id"]
    _delete_version_files(v["video_path"])
    conn.execute("DELETE FROM shot_versions WHERE id=?", (version_id,))
    conn.execute("DELETE FROM jobs WHERE version_id=?", (version_id,))
    conn.execute("UPDATE shots SET adopted_version_id=NULL WHERE id=? AND adopted_version_id=?", (shot_id, version_id))
    shot = conn.execute("SELECT episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if shot:
        ep = conn.execute("SELECT project_id, episode_no FROM episodes WHERE id=?", (shot["episode_id"],)).fetchone()
        if ep:
            _invalidate_final_video(ep["project_id"], ep["episode_no"])
    conn.commit()
    return shot_id


def purge_shot_videos(shot_id: str) -> int:
    """删除某镜的全部视频版本（含视频/尾帧文件、相关任务），清空采用标记，并使该集成品失效。
    用于：该镜关键帧被删空后，旧成片已无首尾帧依据，应一并删除。返回删除的版本数。"""
    conn = get_conn()
    shot = conn.execute("SELECT episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot:
        return 0
    versions = conn.execute("SELECT id, video_path FROM shot_versions WHERE shot_id=?", (shot_id,)).fetchall()
    for v in versions:
        _delete_version_files(v["video_path"])
    conn.execute("DELETE FROM shot_versions WHERE shot_id=?", (shot_id,))
    conn.execute("DELETE FROM jobs WHERE shot_id=? AND kind='video'", (shot_id,))
    conn.execute("UPDATE shots SET adopted_version_id=NULL WHERE id=?", (shot_id,))
    ep = conn.execute("SELECT project_id, episode_no FROM episodes WHERE id=?", (shot["episode_id"],)).fetchone()
    if ep:
        _invalidate_final_video(ep["project_id"], ep["episode_no"])
    conn.commit()
    return len(versions)


def _invalidate_final_video(project_id: str, episode_no: int) -> None:
    """删除某集已合成的整集成品（如存在）。在评审墙产生新片段后调用，
    使成片台回到“需重新合成”的状态，而非展示与当前片段不一致的旧成品。"""
    final_path = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "final" / "episode.mp4"
    try:
        if final_path.exists():
            final_path.unlink()
    except OSError:
        pass


def _adopted_video_paths(episode_id: str) -> list[tuple[int, str]]:
    """按镜头顺序返回 (shot_no, video_path)，仅含已有成片的镜头。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT s.shot_no, v.video_path
           FROM shots s
           JOIN shot_versions v ON v.id = s.adopted_version_id
           WHERE s.episode_id=? AND v.status='succeeded' AND v.video_path IS NOT NULL
           ORDER BY s.shot_no""",
        (episode_id,)).fetchall()
    return [(r["shot_no"], r["video_path"]) for r in rows]


def concatenate_episode(episode_id: str) -> dict:
    """把本集所有已采用的镜头顺序拼接成一个 MP4。
    返回 {video_url, shots, total_duration_s}。若系统未装 ffmpeg 则返回占位说明。
    """
    from pathlib import Path as _P
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError("剧集不存在")
    pieces = _adopted_video_paths(episode_id)
    if not pieces:
        raise ValueError("本集没有任何已采用的视频片段，先生成/采用后再试")

    final_path = _final_video_path(ep["project_id"], ep["episode_no"])
    if not shutil.which("ffmpeg"):
        # 缺 ffmpeg 的保底：回传首个片段 URL，前端提示用户安装 ffmpeg
        first = next(p for p in pieces if p[1])
        from app.config import PROJECTS_DIR
        rel_path = Path(first[1]).relative_to(PROJECTS_DIR).as_posix()
        return {
            "video_url": f"/media/{rel_path}",
            "shots": len(pieces),
            "total_duration_s": 10 * len(pieces),
            "ffmpeg_missing": True,
            "note": "服务端缺少 ffmpeg，已临时回退为首个片段的直链；请安装 ffmpeg 后重新合成",
        }

    # 用 concat demuxer 优先无重编码直粘（画质无损）；但 -c copy 要求各片段编码参数
    # （像素格式/timebase/SAR/profile）完全一致，否则会失败或花屏。一旦失败，回退重编码兜底。
    with tempfile.TemporaryDirectory() as td:
        listfile = _P(td) / "list.txt"
        lines = []
        for _, vpath in pieces:
            # concat demuxer 要求绝对路径并转义单引号
            safe = vpath.replace("'", "'\\''")
            lines.append(f"file '{safe}'")
        listfile.write_text("\n".join(lines), encoding="utf-8")
        silent_video = _P(td) / "concat.mp4"
        concat_in = ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "concat", "-safe", "0", "-i", str(listfile)]
        try:
            subprocess.run(
                concat_in + ["-c", "copy", "-movflags", "+faststart", str(silent_video)],
                check=True, capture_output=True)
        except subprocess.CalledProcessError:
            # 片段编码参数不一致导致 -c copy 失败 → 重编码兜底（画质损失极小，但保证能拼成整集）
            subprocess.run(
                concat_in + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(silent_video)],
                check=True, capture_output=True)
        # 配音混音：开启音频功能时把整集配音轨混入成片（Seedance 视频本身无声）。
        # 没有可用配音/未装 ffmpeg 时 build 返回 None → 退回无声成片（合理跳过，非静默吞错）。
        from app import audio as audio_mod
        audio_track = (audio_mod.build_episode_audio_track(episode_id, _P(td) / "track.wav")
                       if audio_mod.is_enabled() else None)
        if audio_track:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-i", str(silent_video), "-i", str(audio_track),
                 "-c:v", "copy", "-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0",
                 "-shortest", "-movflags", "+faststart", str(final_path)],
                check=True, capture_output=True)
        else:
            shutil.copyfile(str(silent_video), str(final_path))

    total_dur = 0
    try:
        for _, vpath in pieces:
            raw = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", vpath], capture_output=True, text=True, check=True
            ).stdout.strip()
            total_dur += float(raw) if raw else 0
    except (subprocess.CalledProcessError, ValueError):
        total_dur = 10 * len(pieces)

    from app.config import PROJECTS_DIR
    rel_path = final_path.relative_to(PROJECTS_DIR).as_posix()
    return {
        "video_url": f"/media/{rel_path}",
        "shots": len(pieces),
        "total_duration_s": round(total_dur, 1),
        "ffmpeg_missing": False,
    }
