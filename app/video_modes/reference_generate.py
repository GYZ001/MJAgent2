"""单张参考图生成调用：seed 回退、尾帧提取与批量 prompt 写入。"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess

from pathlib import Path
from typing import Any

from app import config, hiagent
from app.atomic_io import atomic_write_bytes
from app.db import new_id
from app.hiagent import ProviderError
from app.schemas import Bible, EpisodeScreenplay, Shot

from .asset_lookup import _asset_from_path, _safe_ref_name, reference_image_path
from .keyframe_contract import _keyframe_character_anchors
from .mode_selection import ReferenceImageAsset
from .reference_prompt import reference_generation_prompt



_SEED_USAGE_NOTE = (
    " Reference images lock identity, outfit, style, and environment only—not pose, framing, camera, or physical "
    "height. Each character image is one separate named identity; never merge, swap, omit, or duplicate identities. "
    "Ignore crop-size differences and follow the mandatory action/geometry contract."
)


async def _generate_image_with_seed_fallback(prompt: str, seed_inputs: list[str] | None, *,
                                             call_meta: dict | None = None) -> dict[str, Any]:
    """Generate with the complete seed contract; never drop identity inputs."""
    return await hiagent.generate_image(
        prompt,
        size=config.REF_IMAGE_SIZE,
        image_inputs=seed_inputs or None,
        call_meta=call_meta,
    )



async def _generate_one_reference(*, project_id: str, episode_no: int, shot: Shot, bible: Bible,
                                  ref_type: str, index: int, content_override: str | None = None,
                                  seed_inputs: list[str] | None = None,
                                  extra_instruction: str | None = None,
                                  skip_inline_qa: bool = False,
                                  screenplay: EpisodeScreenplay | None = None) -> ReferenceImageAsset:
    from app.continuity import effective_characters_visible

    prompt = reference_generation_prompt(
        shot,
        bible,
        ref_type,
        index,
        content_override=content_override,
        screenplay=screenplay,
        identity_seeded=bool(seed_inputs),
    )
    if seed_inputs:
        prompt += _SEED_USAGE_NOTE
    if extra_instruction:
        instruction = extra_instruction.strip()
        if seed_inputs:
            seed_names = list(dict.fromkeys([
                *_keyframe_character_anchors(
                    shot,
                    bible,
                    screenplay=screenplay,
                ),
                *[
                    str(character.name).strip()
                    for character in bible.characters
                    if str(character.name).strip()
                ],
            ]))
            aliases = {
                name: f"subject {position}"
                for position, name in enumerate(seed_names, start=1)
            }
            for name in sorted(aliases, key=len, reverse=True):
                instruction = instruction.replace(name, aliases[name])
        prompt += " " + instruction
    # 每次生成使用独立文件名：合同升级/并发重抽不得覆盖历史版本已引用的字节。
    base_dest = reference_image_path(project_id, episode_no, shot.shot_no, ref_type, index)
    artifact_token = _safe_ref_name(new_id("img"))
    dest = base_dest.with_name(f"{base_dest.stem}_{artifact_token}{base_dest.suffix}")
    item = await _generate_image_with_seed_fallback(
        prompt,
        seed_inputs,
        call_meta={
            "asset_kind": "reference_image",
            "episode_no": episode_no,
            "shot_no": shot.shot_no,
            "reference_type": ref_type,
            "reference_index": index,
            "artifact_token": artifact_token,
        })
    if item.get("url"):
        await hiagent.download(item["url"], str(dest))
    elif item.get("b64_json"):
        atomic_write_bytes(dest, base64.b64decode(item["b64_json"]))
    else:
        raise ProviderError(f"Reference image response missing url/b64_json: {list(item.keys())}")
    # VLM 图片质检已下线：技术产物（文件）落盘即可用，不再跑单图或批量评分。
    # ``skip_inline_qa`` 形参不再改变行为，保留仅为调用方兼容。
    del skip_inline_qa
    qa = {"overall": 1.0, "issues": []}
    asset = _asset_from_path(
        path=str(dest),
        ref_type=ref_type,
        source="seedream_generated",
        quality_score=1.0,
        qa=qa,
        related_character_ids=(
            effective_characters_visible(shot) if ref_type in {"character", "plot_key_frame"} else []
        ),
    )
    asset.selectedForSeedance = True
    asset.rejectReason = None
    return asset


def _extract_last_frame(video_path: str, dest: Path) -> bool:
    """用 ffmpeg 抽取视频最后一帧到 dest。成功返回 True。"""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return False
    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, check=True).stdout.strip() or 0)
        if dur <= 0:
            return False
        ts = max(0.0, dur - 0.1)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ts:.2f}", "-i", video_path,
             "-vframes", "1", "-q:v", "3", str(dest)],
            check=True, capture_output=True)
        return dest.exists()
    except (subprocess.SubprocessError, ValueError, OSError):
        return False


def previous_tail_source_contract(conn: Any, prev_shot: Any) -> dict[str, Any] | None:
    """冻结上一镜实际采用成片，用于检测重抽/重新采用后的尾帧过期。"""
    if prev_shot is None:
        return None

    def _g(key: str) -> Any:
        if hasattr(prev_shot, "keys"):
            return prev_shot[key] if key in prev_shot.keys() else None
        return prev_shot.get(key)

    prev_id = _g("id")
    adopted = _g("adopted_version_id")
    if not adopted:
        return None
    v = conn.execute(
        "SELECT video_path FROM shot_versions WHERE id=? AND status='succeeded'", (adopted,),
    ).fetchone()
    if not v or not v["video_path"]:
        return None
    video_path = Path(v["video_path"])
    if not video_path.is_file():
        return None
    try:
        stat = video_path.stat()
    except OSError:
        return None
    return {
        "shot_id": prev_id,
        "adopted_version_id": adopted,
        "video_path": str(video_path.resolve()),
        "video_size": stat.st_size,
        "video_mtime_ns": stat.st_mtime_ns,
    }


def previous_tail_reference_asset(conn: Any, prev_shot: Any, *, dest_dir: Path) -> ReferenceImageAsset | None:
    """从上一镜实际采用成片抽尾帧，作为连续镜的参考图锚点。"""
    source_contract = previous_tail_source_contract(conn, prev_shot)
    if source_contract:
        signature = hashlib.sha256(
            json.dumps(source_contract, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"00_previous_tail_{signature}.jpg"
        if (dest.is_file() and dest.stat().st_size > 0) or _extract_last_frame(
            source_contract["video_path"], dest,
        ):
            asset = _asset_from_path(
                path=str(dest), ref_type="previous_shot_frame", source="previous_shot",
                shot_id=source_contract["shot_id"], quality_score=1.0,
                qa={"overall": 1.0, "issues": ["forced_continuity"]},
            )
            asset.dependency_manifest = {"continuity_source": source_contract}
            return asset
    return None


def _portrait_seed_inputs(bible: Bible, character_names: list[str], *, project_id: str | None,
                          episode_no: int | None, limit: int = 2) -> list[str]:
    """出场角色定妆照的 data URL，作为新参考图的 i2i 种子（锁长相/发型/服饰，姿态仍走文字）。
    用 refs.refs_as_image_inputs 走「按集分段定妆照」选版，与喂给 Seedance 的人物锚点同源。"""
    from app.refs import refs_as_image_inputs
    return [url for url, _ in refs_as_image_inputs(
        bible, list(character_names), max(limit, 0), project_id=project_id, episode_no=episode_no)]
