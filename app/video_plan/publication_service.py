"""Provider-media publication: fetch/validate a provider-hosted media URL (or
a local worker path), store it durably, and record a signed public URL.

Moved verbatim out of the pre-split ``app/video_plan.py`` (see
``app/video_plan/__init__.py`` for the package-split rationale).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from app.db import get_conn, get_setting, new_id, now

from .primitives import _json


class ProviderMediaPublicationService:
    """Publish project media through an explicitly configured, provider-readable URL."""

    @staticmethod
    def _assert_web_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("参考视频必须发布为可访问的 http(s) Web URL")
        host = parsed.hostname.lower()
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
            raise ValueError("参考视频 URL 不能指向本机或局域网主机")
        addresses: set[str] = set()
        try:
            addresses.add(str(ipaddress.ip_address(host)))
        except ValueError:
            try:
                addresses.update(
                    item[4][0]
                    for item in socket.getaddrinfo(
                        host, parsed.port or (443 if parsed.scheme == "https" else 80),
                        type=socket.SOCK_STREAM,
                    )
                )
            except socket.gaierror as exc:
                raise ValueError(f"参考视频 URL 主机无法解析：{host}") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if (
                ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            ):
                raise ValueError("参考视频 URL 不能指向私网、链路本地或保留地址")

    @staticmethod
    async def _check_accessible(url: str) -> None:
        timeout = httpx.Timeout(connect=10, read=20, write=10, pool=10)
        current = url
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _hop in range(6):
                ProviderMediaPublicationService._assert_web_url(current)
                response = await client.get(
                    current, headers={"Range": "bytes=0-1"},
                )
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("参考视频重定向缺少 Location")
                    current = urljoin(current, location)
                    continue
                if response.status_code not in {200, 206}:
                    raise ValueError(
                        f"参考视频 URL 不可读取（HTTP {response.status_code}）"
                    )
                return
        raise ValueError("参考视频 URL 重定向次数过多")

    @staticmethod
    async def _remote_metadata(url: str) -> dict[str, Any]:
        try:
            limit = int(get_setting("provider_media_max_download_bytes") or 512 * 1024 * 1024)
        except (TypeError, ValueError):
            limit = 512 * 1024 * 1024
        digest = hashlib.sha256()
        size = 0
        mime = "application/octet-stream"
        timeout = httpx.Timeout(connect=10, read=120, write=10, pool=10)
        current = url
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _hop in range(6):
                ProviderMediaPublicationService._assert_web_url(current)
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("参考视频重定向缺少 Location")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise ValueError(
                            f"参考视频内容不可完整读取（HTTP {response.status_code}）"
                        )
                    mime = response.headers.get("content-type", mime).split(";", 1)[0]
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise ValueError("参考视频超过媒体发布服务允许的大小")
                        digest.update(chunk)
                    return {
                        "sha256": digest.hexdigest(),
                        "size_bytes": size,
                        "mime": mime,
                    }
        raise ValueError("参考视频 URL 重定向次数过多")

    @staticmethod
    def _media_metadata(path: Path) -> dict[str, Any]:
        raw = path.read_bytes()
        metadata: dict[str, Any] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        }
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries",
                    "format=duration:stream=width,height,codec_name",
                    "-of", "json", str(path),
                ],
                capture_output=True, text=True, timeout=30, check=True,
            )
            probe = json.loads(result.stdout or "{}")
            metadata["duration_s"] = float((probe.get("format") or {}).get("duration") or 0)
            video = next(
                (item for item in probe.get("streams") or [] if item.get("width")),
                {},
            )
            metadata["width"] = video.get("width")
            metadata["height"] = video.get("height")
            metadata["codec"] = video.get("codec_name")
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
        return metadata

    async def publish(
        self,
        *,
        source_revision_id: str,
        source_url: str | None = None,
        local_path: str | None = None,
        expires_at: float | None = None,
        conn=None,
    ) -> dict[str, Any]:
        db = conn or get_conn()
        if not str(source_revision_id or "").strip():
            raise ValueError("媒体发布必须绑定非空 source_revision_id")
        metadata: dict[str, Any] = {}
        if source_url:
            url = source_url.strip()
            await self._check_accessible(url)
            metadata = await self._remote_metadata(url)
            sha = metadata["sha256"]
            mime = metadata["mime"]
        elif local_path:
            path = Path(local_path).resolve()
            if not path.is_file():
                raise ValueError("待发布媒体文件不存在")
            metadata = self._media_metadata(path)
            public_base = (get_setting("provider_media_public_base_url") or "").strip().rstrip("/")
            projects_root = Path(get_setting("projects_dir") or "").resolve() if get_setting("projects_dir") else None
            if not public_base:
                raise ValueError(
                    "本地参考视频尚未配置供应商可访问的对象存储或 provider_media_public_base_url"
                )
            if projects_root and path.is_relative_to(projects_root):
                relative = path.relative_to(projects_root)
            else:
                from app.config import PROJECTS_DIR
                try:
                    relative = path.relative_to(PROJECTS_DIR.resolve())
                except ValueError as exc:
                    raise ValueError("本地媒体不在项目媒体目录，禁止匿名外传") from exc
            url = f"{public_base}/{quote(relative.as_posix(), safe='/')}"
            await self._check_accessible(url)
            sha = metadata["sha256"]
            mime = metadata["mime"]
        else:
            raise ValueError("source_url 与 local_path 至少提供一项")
        publication_id = new_id("pmp")
        expiry = float(expires_at or now() + 6 * 3600)
        if expiry <= now() + 1800:
            raise ValueError("媒体 URL 有效期不足，必须覆盖排队和生成窗口")
        db.execute(
            """INSERT INTO provider_media_publications(
                   id,source_revision_id,source_url,local_path,published_url,
                   sha256,mime,duration_s,width,height,url_expires_at,status,
                   metadata_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                publication_id, source_revision_id, source_url, local_path, url,
                sha, mime, metadata.get("duration_s"), metadata.get("width"),
                metadata.get("height"), expiry, "ready", _json(metadata), now(), now(),
            ),
        )
        if conn is None:
            db.commit()
        return {
            "id": publication_id,
            "source_revision_id": source_revision_id,
            "published_url": url,
            "sha256": sha,
            "mime": mime,
            "url_expires_at": expiry,
            **metadata,
        }
