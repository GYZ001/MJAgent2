"""说话人 -> 参考音频清单解析（角色固定音色 U3：视频请求接入，见
docs/角色固定音色_声音生成接口调研与实施方案_2026-09-23.md §5.3）。

L4（包前缀 ``"app.voice" = 4`` 覆盖，未单独声明）：只经调用方传入的 ``conn``
读 ``character_portraits``/``character_voices``，不发起任何外部调用。

:func:`resolve_segment_reference_audios` 被两处调用，且必须传入同一套判据
（能力上限、每段人数上限、是否有可见参考）：
``app.media_exec.enqueue_prompt``（入队时算幂等键指纹）与
``app.media_exec.input_reference_audio``（提交前冻结进版本 meta）。两次调用
时机不同（入队 vs 真正冻结），结果理论上可能因这段时间内声音改绑而漂移——
与 ``current_reference_manifest``/``resolve_shot_asset_dependencies`` 对参考
图两处调用的漂移是同一类已知限制，图片那边靠 ``manifest_revisions_match``
显式核对，音频本轮不做对应的漂移校验（P0 范围见 U3 派单）。

真正挡住"已采纳镜头被重新烧掉"的不是这两处指纹算得准不准，而是更上游
——``only_incomplete``（``app.domain.video_ops.generate._generate_episode_core``
第 217-232 行）与 Supervisor 覆盖台账——在镜头进入本模块之前就已经把"已
采纳且不过期"的镜头整段过滤掉，见 ``tests/test_voice_segment_refs.py`` 对
该过滤 SQL 的独立验证。本模块只回答"如果现在要为这段选声音参考，选出来
是什么"，不负责"什么时候才该重新选"。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from app.db import get_setting
from app.voice import store as voice_store

SETTING_KEY_ENABLED = "video_reference_audio_enabled"
SETTING_KEY_MAX_SPEAKERS = "video_reference_audio_max_speakers"
DEFAULT_MAX_SPEAKERS = 3
_BIBLE_PREFIX = "bible:"


def reference_audio_enabled() -> bool:
    """默认开启（用户 2026-09-24：参考音频「必须是默认就走」）；只有设置里显式写了
    0/false/off/no 才关闭。人物卡没有声音或声音文件缺失的角色在解析时逐个跳过，
    不影响出片。"""
    raw = str(get_setting(SETTING_KEY_ENABLED) or "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def configured_max_speakers() -> int:
    """每段最多传入几个角色的声音；合法区间 1-3，读不到或非法值按默认 3。"""
    raw = str(get_setting(SETTING_KEY_MAX_SPEAKERS) or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_SPEAKERS
    except ValueError:
        return DEFAULT_MAX_SPEAKERS
    return max(1, min(3, value))


def _character_name(identity_id: str) -> str:
    return identity_id[len(_BIBLE_PREFIX):]


def _bible_speaker_ranking(segment: dict[str, Any]) -> list[tuple[str, str]]:
    """本段 ``bible:`` 说话人，按台词句数降序、同数按首次出场排序。

    含画外音/内心独白：两者的 ``speaker_identity_id`` 仍是 ``bible:{名}``，
    判据只看前缀天然覆盖；不含旁白（固定字面量"旁白"）与 ``entity:``（未
    具名主体自己的前缀），两者都不以 ``bible:`` 开头，自然被排除。
    """
    counts: dict[str, int] = {}
    first_index: dict[str, int] = {}
    for index, line in enumerate(segment.get("dialogue") or []):
        if not isinstance(line, dict):
            continue
        identity_id = str(line.get("speaker_identity_id") or "")
        if not identity_id.startswith(_BIBLE_PREFIX):
            continue
        counts[identity_id] = counts.get(identity_id, 0) + 1
        first_index.setdefault(identity_id, index)
    ordered = sorted(
        counts, key=lambda identity_id: (-counts[identity_id], first_index[identity_id]),
    )
    return [(identity_id, _character_name(identity_id)) for identity_id in ordered]


def _portrait_id_for_identity(segment: dict[str, Any], identity_id: str) -> str | None:
    for entry in (segment.get("resources") or {}).get("characters") or []:
        if isinstance(entry, dict) and str(entry.get("identity_id") or "") == identity_id:
            portrait_id = entry.get("portrait_id")
            return str(portrait_id) if portrait_id else None
    return None


def _portrait_anchor_key(conn: sqlite3.Connection, project_id: str, portrait_id: str) -> str:
    """本段快照的 ``portrait_id`` 此刻的年龄段锚点；行已被删除、或 ``anchor_key``
    列尚未懒迁移到当前数据库，都按默认年龄段处理（空串）——"是否非默认年龄段"
    这条规则只在确实查得到非空锚点时才生效，查不到不等于"一定是非默认"。
    """
    try:
        row = conn.execute(
            "SELECT anchor_key FROM character_portraits WHERE id=? AND project_id=?",
            (portrait_id, project_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    if row is None:
        return ""
    return str(row["anchor_key"] or "")


def resolve_segment_reference_audios(
    *,
    conn: sqlite3.Connection,
    project_id: str,
    segment: dict[str, Any],
    has_visual_reference: bool,
    max_speakers: int,
    supports_reference_audio: bool,
    max_reference_audios: int,
    max_reference_audio_total_s: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """本段说话角色 -> 参考音频清单（refs）与未传原因（skips，中文）。

    refs 每项 ``{"index","character_name","anchor_key","voice_id","clip_path",
    "clip_sha256","clip_duration_s"}``；skips 每项 ``{"character_name","reason"}``。
    两个返回值任何情况下都是列表（不返回 None），调用方可直接原样冻结或计入
    指纹。规则见模块文档引用的方案 §5.3 与 U3 派单第 2 条。
    """
    ranked = _bible_speaker_ranking(segment)
    if not ranked:
        return [], []
    if not supports_reference_audio:
        return [], [
            {"character_name": name, "reason": "当前视频模型未接入参考音频"}
            for _identity_id, name in ranked
        ]
    if not has_visual_reference:
        return [], [
            {"character_name": name, "reason": "本段没有参考图，视频模型不接受只传声音"}
            for _identity_id, name in ranked
        ]
    cap = max(0, min(int(max_speakers), int(max_reference_audios)))
    skips: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    for identity_id, name in ranked:
        portrait_id = _portrait_id_for_identity(segment, identity_id)
        anchor_key = _portrait_anchor_key(conn, project_id, portrait_id) if portrait_id else ""
        voice = voice_store.current_for(conn, project_id, name, anchor_key)
        if voice is None:
            reason = "该年龄段未绑定声音" if anchor_key else "未配置声音"
            skips.append({"character_name": name, "reason": reason})
            continue
        clip_path = str(voice["clip_path"] or "")
        if not clip_path or not Path(clip_path).is_file():
            skips.append({"character_name": name, "reason": "声音文件缺失，请在人物谱重新生成"})
            continue
        # 名额只算真正传入的声音：没配声音的说话人不占位，否则排在后面、配了声音的
        # 角色会被误判「超出上限」。
        if len(accepted) >= cap:
            skips.append({"character_name": name, "reason": f"超出每段 {cap} 个上限"})
            continue
        accepted.append({
            "character_name": name, "anchor_key": anchor_key,
            "voice_id": str(voice["id"]), "clip_path": str(voice["clip_path"] or ""),
            "clip_sha256": str(voice["clip_sha256"] or ""),
            "clip_duration_s": voice["clip_duration_s"],
        })
    total = 0.0
    cutoff = len(accepted)
    for position, item in enumerate(accepted):
        duration = float(item["clip_duration_s"] or 0.0)
        if total + duration > max_reference_audio_total_s:
            cutoff = position
            break
        total += duration
    for item in accepted[cutoff:]:
        skips.append({"character_name": item["character_name"], "reason": "超出总时长上限"})
    refs = [{"index": index, **item} for index, item in enumerate(accepted[:cutoff], start=1)]
    return refs, skips


def fingerprint_reference_audios(refs: list[dict[str, Any]]) -> str:
    """稳定指纹：只取影响供应商请求字节的字段，按 ``refs`` 已排定的顺序直接
    拼接（不排序——顺序本身决定 ``@音频N`` 的编号，也是请求内容的一部分）。
    """
    if not refs:
        return ""
    material = "|".join(
        f"{item.get('character_name')}:{item.get('anchor_key')}:"
        f"{item.get('voice_id')}:{item.get('clip_sha256')}"
        for item in refs
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
