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

    @staticmethod
    def _resolve_owning_project_id(conn, source_revision_id: str) -> str | None:
        """从 ``source_revision_id``（``shot_versions.id``——见
        ``app.media_exec.input_video_mode`` 里 ``adopted_id`` 的既有用法）走
        shot/version → episode → project 这条既有解析链，得到它实际归属的项目。

        任何一环查不到都返回 ``None``——调用方必须把 ``None`` 当「拒绝发布」处理
        （fail closed），不能把「查不到」当「不需要校验」放行：这正是本函数存在
        的理由，即让 ``local_path``/``source_url`` 声称的发布对象与
        ``source_revision_id`` 声称的归属互相印证，而不是各说各话。
        """
        row = conn.execute(
            """SELECT e.project_id AS project_id
               FROM shot_versions v
               JOIN shots s ON s.id = v.shot_id
               JOIN episodes e ON e.id = s.episode_id
               WHERE v.id = ?""",
            (source_revision_id,),
        ).fetchone()
        return str(row["project_id"]) if row and row["project_id"] else None

    @staticmethod
    def _require_path_within_owning_project(path: Path, project_id: str) -> Path:
        """路径穿越防护 + 项目归属校验的合一判据：``path``（已 ``Path.resolve()``）
        必须落在 ``source_revision_id`` 解析出的这一个项目目录内。任何 ``../``
        穿越、指向另一个项目目录、或指向项目目录之外任意文件的输入，都会在下面
        的 ``relative_to`` 上抛 ``ValueError`` 被拒绝——fail closed，不做「不在
        已知项目列表就放行」这种黑名单式判断。

        两个候选根都试一遍：``provider_media_public_base_url`` 可能对应一个与
        ``app.config.PROJECTS_DIR`` 目录结构镜像但绝对路径不同的对象存储根
        （``projects_dir`` 设置），命中哪一个就用哪一个计算相对路径，不假设两者
        必然同时存在或统一到同一个绝对路径下——沿用本函数改造前就有的双根兼容
        行为，只是把「只要在某个根下就放行」收紧成「必须在该 source_revision_id
        所属项目的子目录下才放行」。
        """
        # 延迟 import：app.config.PROJECTS_DIR 在测试里常被
        # monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path) 按用例覆盖，模块
        # 级 import 会绑定到导入时的旧值，看不到之后的桩替换。
        from app.config import PROJECTS_DIR

        candidate_roots: list[Path] = []
        configured = get_setting("projects_dir")
        if configured:
            candidate_roots.append(Path(configured).resolve())
        candidate_roots.append(PROJECTS_DIR.resolve())
        for root in candidate_roots:
            project_root = (root / project_id).resolve()
            try:
                path.relative_to(project_root)
            except ValueError:
                continue
            return path.relative_to(root)
        raise ValueError(
            "本地媒体不在 source_revision_id 所属项目目录内，禁止跨项目或路径穿越发布"
        )

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
        owning_project_id = self._resolve_owning_project_id(db, source_revision_id)
        if not owning_project_id:
            raise ValueError(
                "source_revision_id 无法解析出所属项目（shot/version → episode → "
                "project 链路查不到匹配行），拒绝发布"
            )
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
            if not public_base:
                raise ValueError(
                    "本地参考视频尚未配置供应商可访问的对象存储或 provider_media_public_base_url"
                )
            relative = self._require_path_within_owning_project(path, owning_project_id)
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
