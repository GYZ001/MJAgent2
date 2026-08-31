"""人物谱/场景库「完全手动新增/替换」共用的上传管道（2026-08-31 用户拍板：
「图像描述都让用户自己填写上传」）。

manual_character.py（角色手动新增/替换定妆照）与 manual_scene.py（场景手动新增/
替换场景图）共用同一套图片校验与长度差值报错，不各写一份——两者只是挂的表
（character_portraits / scene_references）不同，上传管道本身无差异。

两条硬性告知（CLAUDE.md「User-Facing Behavior」）：手动上传的图片不受项目统一
画风约束、替换后下游已生成的分镜/视频不会自动重做——本模块把这两句写成常量，
两个调用方（新增/替换）与前端共享同一份措辞，不允许各写一遍导致不一致。
"""
from __future__ import annotations

from fastapi import HTTPException, UploadFile

MAX_MANUAL_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB，参考 app.ingest.MAX_NOVEL_UPLOAD_BYTES 同一量级取值方式

MANUAL_UPLOAD_PROMPT_MARKER = "[用户手动上传]无生成提示词，画面由用户自行提供"

MANUAL_STYLE_WARNING = "该图片由用户手动上传，不受项目统一画风约束，可能与其它镜头画风不一致。"

DOWNSTREAM_STALE_NOTICE = (
    "替换只对之后新产出的分镜/视频生效；此前已生成的分镜与视频仍使用替换前的图片，"
    "不会自动重做，如需更新请手动前往对应集数重新生成。"
)

# JPEG: FF D8 FF；PNG: 89 50 4E 47 0D 0A 1A 0A；WEBP: 'RIFF'....'WEBP'（第 0-3 与
# 第 8-11 字节两段签名）——与 app.evidence.media.validate_image_file 的判据同构，
# 额外多认 WEBP（那边只服务已落盘的生成产物，历史上不产出 WEBP；这里服务用户任意
# 来源的相机/截图导出，WEBP 很常见，不认会把合法上传误拒）。
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _looks_like_supported_image(raw: bytes) -> bool:
    if raw.startswith(_JPEG_SIGNATURE) or raw.startswith(_PNG_SIGNATURE):
        return True
    return raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"


async def read_manual_image_upload(file: UploadFile) -> bytes:
    """读取并校验用户手动上传的定妆照/场景图；不认识的类型或超限文件直接拒收。

    按类型/大小/签名三道校验（大小用「多读 1 字节」判断，避免把整个超限文件读进
    内存才发现超限——与 app.domain.projects.create._read_novel_upload 同一手法）。
    """
    raw = await file.read(MAX_MANUAL_IMAGE_BYTES + 1)
    if len(raw) > MAX_MANUAL_IMAGE_BYTES:
        limit_mb = MAX_MANUAL_IMAGE_BYTES // (1024 * 1024)
        raise HTTPException(413, f"图片超过 {limit_mb} MB，请压缩后重试")
    if not raw:
        raise HTTPException(422, "文件为空，请选择一张真实图片")
    if not _looks_like_supported_image(raw):
        raise HTTPException(415, "仅支持 JPEG / PNG / WEBP 格式的图片")
    return raw


def length_gap_message(field_label: str, value: str, min_len: int, max_len: int) -> str | None:
    """长度越界时精确说明差多少字，不许笼统报错（CLAUDE.md「Prompts」）。
    合规返回 None。"""
    length = len(value)
    if length < min_len:
        return f"{field_label}现 {length} 字，至少还需 {min_len - length} 字才够 {min_len}~{max_len} 字下限"
    if length > max_len:
        return f"{field_label}现 {length} 字，超出上限 {length - max_len} 字，要求 {min_len}~{max_len} 字"
    return None
