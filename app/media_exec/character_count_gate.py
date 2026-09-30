"""视频画面人数与身份闸门：候选视频落盘后抽帧问 VLM「画面里有几个人、分别是谁」。

L5（碰 hiagent 与 db）。完全照搬 ``app.media_exec.subtitle_gate`` 的已验证范式：
窄二值/可核验问题 + 代码核验，不做评分——评分制视觉质检 2026-08-29 已因
「评分可靠性为 0」整体下线（见 ``app.media_exec.run_job_steps.run_auto_qa`` 的
docstring）。

背景（2026-09-30 生产实证，proj_ca86b15ab7d7 第 1 集第 15 段）：分镜 prompt_text
末尾写着「画面中只有@温念、@顾屿 共2人」，采用视频里顾屿却同时出现两次（前景
背影一个、后方又一个）。视频生成后除技术校验（文件/容器/时长）与字幕闸门外，
没有任何环节核对人数/身份，问题只能在人工看片时才被发现。

判据从数据推导，不猜：本段「允许人数」= ``resources.characters`` 里
``visibility=visible`` 的条目数 + ``resources.flashback_figures`` 条目数（闪回
画面里的孩童是合法的）；若段内存在 ``subject_kind`` 为群演/crowd 的条目（人数
本就无限定），人数上限判为不可判定，只留痕、不拦。VLM 只做两件可核验的提名：
逐帧列出画面里每一个独立人形的简短特征，并把它对应到已登记角色列表里的哪一位
（或「无法对应」）；代码据此核验人数超额（``headcount_exceeded``）与同一具名
角色在同一帧重复出现（``character_duplicated``）。两条都要求连续
``MIN_CONSECUTIVE_FRAMES`` 帧命中才成立——单帧因遮挡/转场被误识别成两个人是
已知的 VLM 噪声模式，误拦一次就是一次视频额度。

结论写进 ``shot_versions.qa_json`` 的 ``character_count_gate`` 键，由
``app.evidence.character_count`` 在候选登记时并进技术校验，走既有的
``technical_resubmit_limit`` 自动重提，与字幕闸门同一条路，不另造重试机制。

取舍：抽帧、VLM 调用、解析失败一律放行（``checked=False``），与 subtitle_gate
「未判定不拦」同一取舍；本段没有登记任何可见角色时（无从比对）同样放行、只留痕。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any

from app import hiagent
from app.db import get_conn, get_setting
from app.media_exec import subtitle_gate

_LOGGER = logging.getLogger(__name__)

SETTING_KEY = "video_character_count_gate_enabled"
GATE_KEY = "character_count_gate"

#: 无限定人数的主体类型（见 app.schemas.segment_identity.SegmentCharacter.subject_kind
#: 的取值说明：「独立无名人物用 extra；复数人群用 crowd」）。``extra`` 是单独一个
#: 无名人物，人数是确定的 1，照常计入允许人数；只有 ``crowd``（复数人群，本项目
#: 产品话术里的「群演」）人数本身不确定，段内出现它时本段人数上限才结构上不可判定。
_UNLIMITED_SUBJECT_KINDS = frozenset({"crowd"})

#: 单帧因遮挡、转场或构图误把一个人识别成两个人是已知的 VLM 噪声模式；只有连续
#: 两个抽样帧都命中同一条问题才可信，与 subtitle_gate「未判定不拦」同一容错取舍
#: 的数量版——字幕闸门是二值问题没有「连续性」维度，这里判据是计数，必须显式设阈。
MIN_CONSECUTIVE_FRAMES = 2


def enabled() -> bool:
    value = (get_setting(SETTING_KEY) or "false").strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError(f"非法运行时设置 {SETTING_KEY}；请在监制房修正")
    return value == "true"


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    return default


# ---------------------------------------------------------------------------
# 本段允许人数与已登记角色外观清单
# ---------------------------------------------------------------------------

def _segment_for_shot(shot_id: str) -> dict[str, Any]:
    row = get_conn().execute("SELECT shot_contract_json FROM shots WHERE id=?", (shot_id,)).fetchone()
    if row is None:
        return {}
    try:
        contract = json.loads(row["shot_contract_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return contract.get("storyboard_pack_segment") or {} if isinstance(contract, dict) else {}


def _character_appearance(conn: Any, character: dict[str, Any], prompt_text: str) -> str:
    """这个角色的外观要点：优先人物卡定妆照的外观锚点（最权威、与画面绑定），
    其次段落自报的 ``description``（群演常见写法），最后从 ``prompt_text`` 里
    模型自己写的「@名字 的外观：……。」提取。查不到任何一种时如实留空，不编造。
    """
    portrait_id = character.get("portrait_id")
    if portrait_id:
        row = conn.execute(
            "SELECT appearance FROM character_portraits WHERE id=?", (portrait_id,),
        ).fetchone()
        appearance = str((row["appearance"] if row else "") or "").strip()
        if appearance:
            return appearance
    description = str(character.get("description") or "").strip()
    if description:
        return description
    name = str(character.get("display_name") or "").strip()
    if name and prompt_text:
        match = re.search(rf"@{re.escape(name)}\s*的外观：([^。]+)。", prompt_text)
        if match:
            return match.group(1).strip()
    return ""


def character_roster_for_shot(shot_id: str, prompt_text: str) -> dict[str, Any]:
    """本段允许人数与已登记可见角色的外观清单。

    ``allowed_headcount`` 为 ``None`` 表示段内含无限定人数的群演/crowd 条目，
    人数上限结构上不可判定；``roster`` 为空表示本段没有登记任何可见角色。
    """
    conn = get_conn()
    segment = _segment_for_shot(shot_id)
    resources = segment.get("resources") or {}
    characters = [c for c in (resources.get("characters") or []) if isinstance(c, dict)]
    flashback = [f for f in (resources.get("flashback_figures") or []) if isinstance(f, dict)]
    unlimited = any(c.get("subject_kind") in _UNLIMITED_SUBJECT_KINDS for c in characters)
    visible = [
        c for c in characters
        if c.get("visibility") == "visible"
        and str(c.get("display_name") or "").strip()
        # 群演/crowd 的 display_name 是「共享 label」，多个不同背景人形被 VLM
        # 正确匹配到同一个 label 是预期行为，不是重复；排除在具名名单外，
        # 让这些人形只能落到「无法对应」，不参与 character_duplicated 判定。
        and c.get("subject_kind") not in _UNLIMITED_SUBJECT_KINDS
    ]
    roster = [
        {"name": str(c.get("display_name")).strip(), "appearance": _character_appearance(conn, c, prompt_text)}
        for c in visible
    ]
    return {
        "roster": roster,
        "allowed_headcount": None if unlimited else len(visible) + len(flashback),
        "unlimited_reason": (
            "本段登记了无限定人数的群演/crowd 条目，人数上限不可判定" if unlimited else ""
        ),
    }


# ---------------------------------------------------------------------------
# VLM 提问
# ---------------------------------------------------------------------------

def _roster_line(roster: list[dict[str, str]]) -> str:
    return "、".join(
        f"{item['name']}（{item['appearance']}）" if item["appearance"] else item["name"]
        for item in roster
    )


def build_messages(frames: list[bytes], roster: list[dict[str, str]]) -> list[dict[str, Any]]:
    prompt = (
        f"下面每张图是一段短视频的抽帧。本段登记在场的角色共 {len(roster)} 位：{_roster_line(roster)}。"
        "逐张完成两件事：(a) 列出画面里每一个能看清轮廓的独立人形（背影、只露出部分身体也算），"
        "用一句话给出这个人形的大致年龄段、性别、发型与上衣颜色；(b) 依据外观描述判断这个人形对应"
        "上面角色列表里的哪一位，确实找不到匹配的就填「无法对应」。只返回一个 JSON 对象："
        '{"frames":[{"index":1,"figures":[{"figure_no":1,"desc":"看到的特征","matched_name":"角色名或无法对应"}]}]}，'
        "frames 按输入顺序逐张给出，画面里没有可辨认人形的帧 figures 给空数组。"
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for frame in frames:
        data = base64.b64encode(frame).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}})
    return [
        {"role": "system", "content": "Return exactly one valid JSON object. No Markdown, no prose."},
        {"role": "user", "content": content},
    ]


# ---------------------------------------------------------------------------
# 模型原文 → 逐帧人形清单（不做人数/重复判定，判定见 evaluate_headcount_and_duplication）
# ---------------------------------------------------------------------------

def _normalize_frame(item: dict[str, Any], ordinal: int) -> dict[str, Any]:
    figures: list[dict[str, Any]] = []
    figures_raw = item.get("figures")
    if isinstance(figures_raw, list):
        for i, fig in enumerate(figures_raw, start=1):
            if not isinstance(fig, dict):
                continue
            figure_no = fig.get("figure_no")
            figures.append({
                "figure_no": figure_no if isinstance(figure_no, int) else i,
                "desc": str(fig.get("desc") or ""),
                "matched_name": str(fig.get("matched_name") or "").strip(),
            })
    index = item.get("index")
    return {"index": index if isinstance(index, int) else ordinal, "figures": figures}


def _frames_by_structure(body: str) -> list[dict[str, Any]] | None:
    """整体 JSON 合法时按结构读；不合法返回 None 交给片段级兜底。"""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    frames = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(frames, list):
        raise ValueError("模型返回缺少 frames 列表")
    return [_normalize_frame(item, ordinal) for ordinal, item in enumerate(frames, start=1) if isinstance(item, dict)]


def _frame_fragments(body: str) -> list[str]:
    """定位 ``"frames": [ ... ]`` 数组，按花括号配平切出每个顶层帧片段。

    帧对象内部嵌套 ``figures`` 数组（含花括号），不能像 subtitle_gate 那样用
    ``\\{[^{}]*\\}`` 的扁平正则逐个摘取——那个正则假设帧对象是平的。这里手写一个
    最小配平扫描器，但**不追踪字符串边界**：subtitle_gate 的真实故障样本是键名
    混入控制字符，导致引号数量变成奇数——如果按字符串边界配平，一个损坏的帧会
    让引号奇偶错位，进而拖累后面所有本来完好的帧一起读不出来。只数花括号/方括号
    本身更皮实：代价是 ``desc`` 文本里出现字面花括号会打乱计数，可接受（VLM 给的
    是「20多岁女性，长发」这类描述，不会写花括号）；单个帧因此切出的片段即使不是
    合法 JSON，也只影响它自己——``json.loads`` 会在 ``_frames_by_fragments`` 里
    对这一个片段失败，不影响其它帧。
    """
    marker = re.search(r'"frames"\s*:\s*\[', body)
    if not marker:
        return []
    fragments: list[str] = []
    depth = 0
    array_depth = 1
    obj_start: int | None = None
    for i in range(marker.end(), len(body)):
        ch = body[i]
        if ch == "[":
            array_depth += 1
        elif ch == "]":
            array_depth -= 1
            if array_depth == 0 and depth == 0:
                break
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and obj_start is not None:
                fragments.append(body[obj_start:i + 1])
                obj_start = None
    return fragments


def _frames_by_fragments(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for ordinal, fragment in enumerate(_frame_fragments(body), start=1):
        try:
            item = json.loads(fragment)
        except ValueError:
            continue
        if isinstance(item, dict):
            frames.append(_normalize_frame(item, ordinal))
    return frames


def parse_verdict(raw: str, frames_checked: int) -> dict[str, Any]:
    """模型原文 → 逐帧人形清单；读不出任何一帧就抛 ``ValueError``（调用方按
    「未判定」放行并留痕，与 subtitle_gate 同一取舍）。读不出的单帧不编值。
    """
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    body = text[start:end + 1]
    frames = _frames_by_structure(body)
    if frames is None:
        frames = _frames_by_fragments(body)
    if not frames:
        raise ValueError("模型没有返回任何可读的帧数据")
    return {"checked": True, "frames_checked": frames_checked, "frames_reported": len(frames), "frames": frames}


# ---------------------------------------------------------------------------
# 逐帧人形清单 → 人数超额 / 角色重复判定
# ---------------------------------------------------------------------------

def _frame_headcount(frame: dict[str, Any]) -> int:
    return len(frame["figures"])


def _frame_duplicated_names(frame: dict[str, Any], roster_names: set[str]) -> set[str]:
    counts: dict[str, int] = {}
    for figure in frame["figures"]:
        name = figure["matched_name"]
        if name in roster_names:
            counts[name] = counts.get(name, 0) + 1
    return {name for name, count in counts.items() if count >= 2}


def _consecutive_run(indices: list[int]) -> list[int]:
    """``indices`` 已升序；返回第一段步长为 1、长度达到 ``MIN_CONSECUTIVE_FRAMES``
    的连续序列，没有就返回空列表——单帧命中不可信，见该常量注释。
    """
    run: list[int] = []
    for idx in indices:
        run = [*run, idx] if run and idx == run[-1] + 1 else [idx]
        if len(run) >= MIN_CONSECUTIVE_FRAMES:
            return run
    return []


def _frame_evidence(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": frame["index"],
        "seconds": round((frame["index"] - 1) * subtitle_gate.FRAME_INTERVAL_S, 1),
        "headcount": _frame_headcount(frame),
        "figures": frame["figures"],
    }


def evaluate_headcount_and_duplication(
    parsed: dict[str, Any], *, allowed_headcount: int | None, roster_names: list[str],
) -> dict[str, Any]:
    """从逐帧人形清单判定人数超额/角色重复。两条都要求连续
    ``MIN_CONSECUTIVE_FRAMES`` 帧命中才成立；``allowed_headcount`` 为 ``None``
    时人数超额不参与判定（只由调用方把不可判定原因写进最终 verdict）。
    """
    frames = sorted(parsed["frames"], key=lambda f: f["index"])
    result: dict[str, Any] = {
        "headcount_exceeded": False, "headcount_evidence": [],
        "character_duplicated": False, "duplicated_characters": [],
    }
    if allowed_headcount is not None:
        over = sorted(f["index"] for f in frames if _frame_headcount(f) > allowed_headcount)
        run = _consecutive_run(over)
        if run:
            result["headcount_exceeded"] = True
            result["headcount_evidence"] = [_frame_evidence(f) for f in frames if f["index"] in run]
    names = set(roster_names)
    for name in roster_names:
        hits = sorted(f["index"] for f in frames if name in _frame_duplicated_names(f, names))
        run = _consecutive_run(hits)
        if run:
            result["character_duplicated"] = True
            result["duplicated_characters"].append({
                "name": name,
                "frames": [_frame_evidence(f) for f in frames if f["index"] in run],
            })
    return result


# ---------------------------------------------------------------------------
# 编排：抽帧 → VLM → 落库
# ---------------------------------------------------------------------------

async def detect_character_count(
    video_path: str, roster: list[dict[str, str]], *, call_meta: dict[str, Any],
) -> dict[str, Any]:
    frames = await asyncio.to_thread(subtitle_gate.sample_frames, video_path)
    if not frames:
        raise ValueError("抽不出任何帧")
    raw = await hiagent.chat(
        build_messages(frames, roster), temperature=0, max_tokens=2000,
        call_meta={"kind": "vlm_character_count_gate", **call_meta},
        response_format={"type": "json_object"},
    )
    return parse_verdict(raw, len(frames))


def write_verdict(version_id: str, verdict: dict[str, Any]) -> None:
    """把结论并进该版本的 qa_json（保留其它键）。独立提交：这是诊断类写入，不借调用方事务。"""
    conn = get_conn()
    row = conn.execute("SELECT qa_json FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    try:
        existing = json.loads((row["qa_json"] if row else None) or "{}")
    except ValueError:
        existing = {}
    merged = {**(existing if isinstance(existing, dict) else {}), GATE_KEY: verdict}
    conn.execute("UPDATE shot_versions SET qa_json=? WHERE id=?", (json.dumps(merged, ensure_ascii=False), version_id))
    conn.commit()


def _unchecked_verdict(label: str, version_id: str, exc: Exception) -> dict[str, Any]:
    verdict = {"checked": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    _LOGGER.warning("[VIDEO_CHARACTER_COUNT_GATE][未判定] 版本 %s %s：%s", version_id, label, verdict["error"])
    return verdict


def _judge_parsed_verdict(
    version_id: str, parsed: dict[str, Any], roster_info: dict[str, Any], roster_names: list[str],
) -> dict[str, Any]:
    """帧证据完整时才允许判定「无问题」：截断的部分帧里已经命中的连续证据仍然
    成立（同 subtitle_gate「命中即成立」的取舍），只有干净结论（没找到问题）才
    要求帧数覆盖到送检数量，否则「没找到」可能只是模型没读全，不该当成「通过」。
    """
    judged = evaluate_headcount_and_duplication(
        parsed, allowed_headcount=roster_info["allowed_headcount"], roster_names=roster_names,
    )
    hit = judged["headcount_exceeded"] or judged["character_duplicated"]
    if not hit and parsed["frames_reported"] < parsed["frames_checked"]:
        exc = ValueError(f"模型只报告了 {parsed['frames_reported']}/{parsed['frames_checked']} 帧，证据不完整不判定为通过")
        return _unchecked_verdict("证据不完整", version_id, exc)
    verdict = {
        **parsed, **judged,
        "allowed_headcount": roster_info["allowed_headcount"],
        "headcount_undetermined_reason": roster_info["unlimited_reason"],
        "roster_names": roster_names,
    }
    if hit:
        _LOGGER.warning(
            "[VIDEO_CHARACTER_COUNT_GATE][拦截] 版本 %s：人数超额=%s 重复角色=%s",
            version_id, verdict["headcount_exceeded"],
            [d["name"] for d in verdict["duplicated_characters"]],
        )
    else:
        _LOGGER.info("[VIDEO_CHARACTER_COUNT_GATE][通过] 版本 %s 检查 %s 帧", version_id, verdict["frames_checked"])
    return verdict


async def evaluate_version(job: Any, version: Any, dest: str) -> dict[str, Any] | None:
    """worker 在候选登记前调用；返回写入的结论，闸门关闭时返回 None。永不抛出——
    角色名单推导会读 DB（可能撞忙锁或脏数据），与 VLM 调用一样必须兜住。
    """
    if not enabled():
        return None
    version_id = str(version["id"])
    try:
        shot_id = str(job["shot_id"])
        prompt_text = str(_row_value(version, "prompt_text") or "")
        roster_info = character_roster_for_shot(shot_id, prompt_text)
        call_meta = {"project_id": job["project_id"], "shot_id": shot_id, "version_id": version_id}
    except Exception as exc:  # noqa: BLE001 未判定放行，见 _unchecked_verdict
        verdict = _unchecked_verdict("角色名单推导失败", version_id, exc)
        write_verdict(version_id, verdict)
        return verdict
    if not roster_info["roster"]:
        verdict = {"checked": False, "reason": "本段没有登记在场的可见角色，跳过画面人数与身份核验"}
        write_verdict(version_id, verdict)
        return verdict
    roster_names = [item["name"] for item in roster_info["roster"]]
    try:
        parsed = await detect_character_count(dest, roster_info["roster"], call_meta=call_meta)
    except Exception as exc:  # noqa: BLE001 未判定放行，见 _unchecked_verdict
        verdict = _unchecked_verdict("VLM 调用失败", version_id, exc)
    else:
        verdict = _judge_parsed_verdict(version_id, parsed, roster_info, roster_names)
    write_verdict(version_id, verdict)
    return verdict
