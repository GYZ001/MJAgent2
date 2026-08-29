from __future__ import annotations

import math
import hashlib

try:
    _queue
except NameError:  # pragma: no cover - used when importing this module directly
    from app.media_exec.common import *


_ACTIVE_VIDEO_JOB_STATUSES = ("queued", "running", "waiting_provider", "waiting_retry")
_CONCAT_PROBE_TIMEOUT_S = 30.0
_CONCAT_DURATION_TOLERANCE_RATIO = 0.10
_CONCAT_DURATION_TOLERANCE_MIN_S = 0.75
_CONCAT_OPERATION_LEASE_S = 2 * 60 * 60
_CONCAT_COMMAND = "delivery.concatenate"


class ConcatOperationConflict(ValueError):
    """A concat idempotency key is already bound to another request."""


class ConcatOperationInProgress(ValueError):
    """A live owner still holds the concat operation lease."""


def _ensure_concat_operation_receipts(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS concat_operation_receipts(
               operation_key TEXT PRIMARY KEY,
               command TEXT NOT NULL,
               request_fingerprint TEXT NOT NULL,
               episode_id TEXT NOT NULL,
               status TEXT NOT NULL,
               result_json TEXT NOT NULL DEFAULT '{}',
               final_path TEXT NOT NULL DEFAULT '',
               final_sha256 TEXT NOT NULL DEFAULT '',
               report_path TEXT NOT NULL DEFAULT '',
               report_sha256 TEXT NOT NULL DEFAULT '',
               report_content TEXT NOT NULL DEFAULT '',
               stage_path TEXT NOT NULL DEFAULT '',
               stage_sha256 TEXT NOT NULL DEFAULT '',
               promotion_phase TEXT NOT NULL DEFAULT 'claimed',
               release_authority_json TEXT NOT NULL DEFAULT '{}',
               video_manifest_json TEXT NOT NULL DEFAULT '{}',
               claim_token TEXT NOT NULL DEFAULT '',
               lease_expires_at REAL NOT NULL DEFAULT 0,
               created_at REAL NOT NULL,
               updated_at REAL NOT NULL
           )"""
    )
    columns = {
        str(row[1]) for row in conn.execute(
            "PRAGMA table_info(concat_operation_receipts)"
        )
    }
    for name, ddl in (
        ("report_content", "TEXT NOT NULL DEFAULT ''"),
        ("stage_path", "TEXT NOT NULL DEFAULT ''"),
        ("stage_sha256", "TEXT NOT NULL DEFAULT ''"),
        ("promotion_phase", "TEXT NOT NULL DEFAULT 'claimed'"),
        ("release_authority_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("video_manifest_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if name not in columns:
            conn.execute(
                f"ALTER TABLE concat_operation_receipts ADD COLUMN {name} {ddl}"
            )


def _concat_operation_key(idempotency_key: str) -> str:
    normalized = str(idempotency_key or "").strip()
    if not normalized:
        raise ConcatOperationConflict("合片命令缺少稳定幂等键")
    return f"{_CONCAT_COMMAND}:{normalized}"


def _load_completed_concat_result(row) -> dict[str, Any]:
    try:
        result = json.loads(str(row["result_json"] or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("合片 receipt 结果损坏") from exc
    if not isinstance(result, dict):
        raise RuntimeError("合片 receipt 结果不是对象")
    for path_field, hash_field, label in (
        ("final_path", "final_sha256", "成片"),
        ("report_path", "report_sha256", "剪辑报告"),
    ):
        path = Path(str(row[path_field] or ""))
        expected = str(row[hash_field] or "")
        if not path.is_file() or not expected or _media_sha256(path) != expected:
            raise ConcatOperationConflict(
                f"合片 receipt 绑定的{label}已丢失或漂移，拒绝伪造幂等重放"
            )
    return result


def claim_concat_operation(
    *,
    idempotency_key: str,
    request_fingerprint: str,
    episode_id: str,
    release_authority: dict[str, Any],
    video_delivery_manifest: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Claim a concat operation, or return its exact durably published result."""
    operation_key = _concat_operation_key(idempotency_key)
    conn = get_conn()
    _ensure_concat_operation_receipts(conn)
    if conn.in_transaction:
        conn.commit()
    stamp = now()
    owner = new_id("concatop")
    release_json = json.dumps(
        release_authority, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    manifest_json = json.dumps(
        video_delivery_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM concat_operation_receipts WHERE operation_key=?",
            (operation_key,),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO concat_operation_receipts(
                       operation_key,command,request_fingerprint,episode_id,status,
                       result_json,release_authority_json,video_manifest_json,
                       claim_token,lease_expires_at,created_at,updated_at
                   ) VALUES(?,?,?,?,'running','{}',?,?,?,?,?,?)""",
                (
                    operation_key,
                    _CONCAT_COMMAND,
                    request_fingerprint,
                    episode_id,
                    release_json,
                    manifest_json,
                    owner,
                    stamp + _CONCAT_OPERATION_LEASE_S,
                    stamp,
                    stamp,
                ),
            )
            conn.commit()
            return owner, None
        if (
            str(row["command"]) != _CONCAT_COMMAND
            or str(row["request_fingerprint"]) != request_fingerprint
            or str(row["episode_id"]) != episode_id
        ):
            raise ConcatOperationConflict(
                "相同 idempotency_key 已绑定不同的合片请求"
            )
        if str(row["status"]) == "succeeded":
            conn.commit()
            return None, _load_completed_concat_result(row)
        if (
            str(row["release_authority_json"] or "{}") != release_json
            or str(row["video_manifest_json"] or "{}") != manifest_json
        ):
            raise ConcatOperationConflict(
                "合片幂等键已冻结旧的分镜发布权威或已采纳视频清单"
            )
        if float(row["lease_expires_at"] or 0) > stamp:
            raise ConcatOperationInProgress("相同合片操作正在执行")
        updated = conn.execute(
            """UPDATE concat_operation_receipts
                  SET claim_token=?,lease_expires_at=?,updated_at=?
                WHERE operation_key=? AND status='running' AND lease_expires_at<=?""",
            (
                owner,
                stamp + _CONCAT_OPERATION_LEASE_S,
                stamp,
                operation_key,
                stamp,
            ),
        )
        if updated.rowcount != 1:
            raise ConcatOperationInProgress("合片 receipt 接管 CAS 冲突")
        conn.commit()
        phase = str(row["promotion_phase"] or "claimed")
        if phase != "claimed":
            try:
                recovered = _resume_concat_promotion(
                    conn,
                    operation_key=operation_key,
                    request_fingerprint=request_fingerprint,
                    episode_id=episode_id,
                    claim_token=owner,
                    release_authority=release_authority,
                    video_delivery_manifest=video_delivery_manifest,
                )
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                conn.execute(
                    """UPDATE concat_operation_receipts SET lease_expires_at=0,updated_at=?
                         WHERE operation_key=? AND claim_token=? AND status='running'""",
                    (now(), operation_key, owner),
                )
                conn.commit()
                raise
            if recovered is not None:
                return None, recovered
        return owner, None
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def release_concat_operation(
    *, idempotency_key: str, request_fingerprint: str, claim_token: str,
) -> None:
    """Expire a failed owner while preserving its frozen source snapshot."""
    conn = get_conn()
    _ensure_concat_operation_receipts(conn)
    conn.execute(
        """UPDATE concat_operation_receipts
              SET lease_expires_at=0,updated_at=?
            WHERE operation_key=? AND command=? AND request_fingerprint=?
              AND claim_token=? AND status='running'""",
        (
            now(),
            _concat_operation_key(idempotency_key),
            _CONCAT_COMMAND,
            request_fingerprint,
            claim_token,
        ),
    )
    conn.commit()


def _media_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _concat_promotion_checkpoint(_phase: str) -> None:
    """No-op checkpoint used by crash-recovery integration tests."""


def _content_versioned_final_url(final_path: Path, content_hash: str) -> str:
    return build_media_url(final_path, version=content_hash.removeprefix("sha256:"))


def _assert_concat_sources_current(
    conn,
    *,
    episode_id: str,
    release_authority: dict[str, Any],
    video_delivery_manifest: dict[str, Any],
) -> None:
    from app.downstream_authority import (
        current_partial_adopted_video_delivery_manifest,
        verify_current_storyboard_release_authority,
    )

    if verify_current_storyboard_release_authority(
        episode_id, conn=conn,
    ) != release_authority:
        raise ConcatOperationConflict("合片发布前分镜权威发生漂移")
    # 合片冻结的是部分交付清单（跳过缺镜/失效镜，不整体失败），发布前的漂移
    # 复核必须用同一口径重新计算，否则一个此前就被合法跳过的镜头在这里会被
    # 严格版本判成"缺少已采纳权威"，把已经跳过一次的镜头再次错误地当成漂移。
    if current_partial_adopted_video_delivery_manifest(
        episode_id, conn=conn,
    ) != video_delivery_manifest:
        raise ConcatOperationConflict("合片发布前已采纳视频发生漂移")


def _resume_concat_promotion(
    conn,
    *,
    operation_key: str,
    request_fingerprint: str,
    episode_id: str,
    claim_token: str,
    release_authority: dict[str, Any],
    video_delivery_manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """Finish a receipt-owned filesystem promotion without rendering again."""
    from app.atomic_io import atomic_write_text

    row = conn.execute(
        "SELECT * FROM concat_operation_receipts WHERE operation_key=?",
        (operation_key,),
    ).fetchone()
    if row is None or str(row["claim_token"]) != claim_token:
        raise ConcatOperationConflict("合片恢复 owner 已被围栏")
    stage_path = Path(str(row["stage_path"] or ""))
    final_path = Path(str(row["final_path"] or ""))
    report_path = Path(str(row["report_path"] or ""))
    stage_sha = str(row["stage_sha256"] or "")
    report_sha = str(row["report_sha256"] or "")
    report_content = str(row["report_content"] or "")
    phase = str(row["promotion_phase"] or "claimed")
    try:
        result = json.loads(str(row["result_json"] or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("合片恢复 result 损坏") from exc
    if not isinstance(result, dict):
        raise RuntimeError("合片恢复 result 不是对象")

    if phase == "prepared":
        if not stage_path.is_file() or _media_sha256(stage_path) != stage_sha:
            # No final path has been exposed yet. Remove only this receipt's
            # explicit owner stage and safely rerender the frozen source set.
            stage_path.unlink(missing_ok=True)
            conn.execute(
                """UPDATE concat_operation_receipts
                      SET promotion_phase='claimed',stage_path='',stage_sha256='',
                          final_path='',final_sha256='',report_path='',report_sha256='',
                          report_content='',result_json='{}',updated_at=?
                    WHERE operation_key=? AND claim_token=? AND status='running'""",
                (now(), operation_key, claim_token),
            )
            conn.commit()
            return None
        conn.execute(
            """UPDATE concat_operation_receipts
                  SET promotion_phase='staged',updated_at=?
                WHERE operation_key=? AND claim_token=? AND status='running'""",
            (now(), operation_key, claim_token),
        )
        conn.commit()
        phase = "staged"

    _assert_concat_sources_current(
        conn,
        episode_id=episode_id,
        release_authority=release_authority,
        video_delivery_manifest=video_delivery_manifest,
    )
    if phase == "staged":
        atomic_copy(stage_path, final_path)
        if _media_sha256(final_path) != stage_sha:
            raise RuntimeError("合片 final 推广后哈希不一致")
        _concat_promotion_checkpoint("after_final_copy")
        conn.execute(
            """UPDATE concat_operation_receipts
                  SET promotion_phase='final_promoted',updated_at=?
                WHERE operation_key=? AND claim_token=? AND status='running'""",
            (now(), operation_key, claim_token),
        )
        conn.commit()
        phase = "final_promoted"

    if phase == "final_promoted":
        if not final_path.is_file() or _media_sha256(final_path) != stage_sha:
            atomic_copy(stage_path, final_path)
        atomic_write_text(report_path, report_content)
        if _media_sha256(report_path) != report_sha:
            raise RuntimeError("合片 report 推广后哈希不一致")
        _concat_promotion_checkpoint("after_report_write")
        conn.execute(
            """UPDATE concat_operation_receipts
                  SET promotion_phase='report_promoted',updated_at=?
                WHERE operation_key=? AND claim_token=? AND status='running'""",
            (now(), operation_key, claim_token),
        )
        conn.commit()
        phase = "report_promoted"

    if phase != "report_promoted":
        raise RuntimeError(f"未知合片推广阶段：{phase}")
    if not final_path.is_file() or _media_sha256(final_path) != stage_sha:
        atomic_copy(stage_path, final_path)
    if not report_path.is_file() or _media_sha256(report_path) != report_sha:
        atomic_write_text(report_path, report_content)

    owner = new_id("concatpub")
    if conn.in_transaction:
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _assert_concat_sources_current(
            conn,
            episode_id=episode_id,
            release_authority=release_authority,
            video_delivery_manifest=video_delivery_manifest,
        )
        current = conn.execute(
            """SELECT claim_token,status,promotion_phase
                 FROM concat_operation_receipts WHERE operation_key=?""",
            (operation_key,),
        ).fetchone()
        if (
            current is None
            or str(current["claim_token"]) != claim_token
            or str(current["status"]) != "running"
            or str(current["promotion_phase"]) != "report_promoted"
        ):
            raise ConcatOperationConflict("合片 finalize owner/phase CAS 冲突")
        if _media_sha256(final_path) != stage_sha or _media_sha256(report_path) != report_sha:
            raise ConcatOperationConflict("合片 finalize 文件哈希漂移")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS episode_video_publish_leases(
                   episode_id TEXT PRIMARY KEY,owner TEXT NOT NULL,
                   video_manifest_hash TEXT NOT NULL,status TEXT NOT NULL,updated_at REAL NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO episode_video_publish_leases(
                   episode_id,owner,video_manifest_hash,status,updated_at
               ) VALUES(?,?,?,'published',?)
               ON CONFLICT(episode_id) DO UPDATE SET owner=excluded.owner,
                   video_manifest_hash=excluded.video_manifest_hash,
                   status='published',updated_at=excluded.updated_at""",
            (episode_id, owner, video_delivery_manifest["manifest_hash"], now()),
        )
        receipt = conn.execute(
            """UPDATE concat_operation_receipts
                  SET status='succeeded',promotion_phase='promoted',lease_expires_at=0,
                      updated_at=?
                WHERE operation_key=? AND command=? AND request_fingerprint=?
                  AND episode_id=? AND claim_token=? AND status='running'
                  AND promotion_phase='report_promoted'""",
            (
                now(), operation_key, _CONCAT_COMMAND, request_fingerprint,
                episode_id, claim_token,
            ),
        )
        if receipt.rowcount != 1:
            raise ConcatOperationConflict("合片 receipt finalize CAS 冲突")
        _concat_promotion_checkpoint("before_finalize_commit")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    final_path.with_suffix(".stale").unlink(missing_ok=True)
    stage_path.unlink(missing_ok=True)
    return result


def _publish_concat_output(
    conn,
    *,
    episode_id: str,
    candidate_path: Path,
    final_path: Path,
    report: dict[str, Any],
    result: dict[str, Any],
    release_authority: dict[str, Any],
    video_delivery_manifest: dict[str, Any],
    operation_idempotency_key: str | None = None,
    operation_request_fingerprint: str | None = None,
    operation_claim_token: str | None = None,
) -> dict[str, Any]:
    """Publish direct calls atomically; durable commands use staged recovery."""
    from app.atomic_io import atomic_write_text

    if operation_idempotency_key is not None:
        if not operation_request_fingerprint or not operation_claim_token:
            raise ConcatOperationConflict("合片发布缺少完整的 receipt owner 绑定")
        operation_key = _concat_operation_key(operation_idempotency_key)
        final_sha256 = _media_sha256(candidate_path)
        report["final_video_sha256"] = final_sha256
        report_content = json.dumps(report, ensure_ascii=False, indent=2)
        report_sha256 = hashlib.sha256(report_content.encode("utf-8")).hexdigest()
        result["video_url"] = _content_versioned_final_url(final_path, final_sha256)
        result["final_video_sha256"] = final_sha256
        result["edit_report_sha256"] = report_sha256
        stage_path = final_path.with_name(
            f".{final_path.name}.{operation_claim_token}.stage"
        )
        report_path = _edit_report_path(final_path)
        if conn.in_transaction:
            conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            _assert_concat_sources_current(
                conn,
                episode_id=episode_id,
                release_authority=release_authority,
                video_delivery_manifest=video_delivery_manifest,
            )
            prepared = conn.execute(
                """UPDATE concat_operation_receipts
                      SET result_json=?,stage_path=?,stage_sha256=?,final_path=?,
                          final_sha256=?,report_path=?,report_sha256=?,report_content=?,
                          promotion_phase='prepared',lease_expires_at=?,updated_at=?
                    WHERE operation_key=? AND command=? AND request_fingerprint=?
                      AND episode_id=? AND claim_token=? AND status='running'""",
                (
                    json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
                    str(stage_path),
                    final_sha256,
                    str(final_path),
                    final_sha256,
                    str(report_path),
                    report_sha256,
                    report_content,
                    now() + _CONCAT_OPERATION_LEASE_S,
                    now(),
                    operation_key,
                    _CONCAT_COMMAND,
                    operation_request_fingerprint,
                    episode_id,
                    operation_claim_token,
                ),
            )
            if prepared.rowcount != 1:
                raise ConcatOperationConflict("合片 prepare owner 已被恢复流程围栏")
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        atomic_copy(candidate_path, stage_path)
        if _media_sha256(stage_path) != final_sha256:
            raise RuntimeError("合片 stage 哈希不一致")
        conn.execute(
            """UPDATE concat_operation_receipts
                  SET promotion_phase='staged',updated_at=?
                WHERE operation_key=? AND claim_token=? AND status='running'
                  AND promotion_phase='prepared'""",
            (now(), operation_key, operation_claim_token),
        )
        conn.commit()
        return _resume_concat_promotion(
            conn,
            operation_key=operation_key,
            request_fingerprint=operation_request_fingerprint,
            episode_id=episode_id,
            claim_token=operation_claim_token,
            release_authority=release_authority,
            video_delivery_manifest=video_delivery_manifest,
        ) or result

    from app.downstream_authority import (
        current_partial_adopted_video_delivery_manifest,
        verify_current_storyboard_release_authority,
    )
    if conn.in_transaction:
        conn.commit()
    owner = new_id("concatpub")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS episode_video_publish_leases(
                   episode_id TEXT PRIMARY KEY,owner TEXT NOT NULL,
                   video_manifest_hash TEXT NOT NULL,status TEXT NOT NULL,updated_at REAL NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO episode_video_publish_leases(
                   episode_id,owner,video_manifest_hash,status,updated_at
               ) VALUES(?,?,?,'publishing',?)
               ON CONFLICT(episode_id) DO UPDATE SET owner=excluded.owner,
                   video_manifest_hash=excluded.video_manifest_hash,
                   status='publishing',updated_at=excluded.updated_at""",
            (episode_id, owner, video_delivery_manifest["manifest_hash"], now()),
        )
        if verify_current_storyboard_release_authority(
            episode_id, conn=conn,
        ) != release_authority:
            raise ValueError("合片发布前分镜权威发生漂移")
        # 合片冻结的是部分交付清单（跳过缺镜/失效镜），发布前漂移复核要用同一
        # 口径重算，否则本来就合法跳过的镜头会被严格版本误判成漂移。
        if current_partial_adopted_video_delivery_manifest(
            episode_id, conn=conn,
        ) != video_delivery_manifest:
            raise ValueError("合片发布前已采纳视频发生漂移")
        report["final_video_sha256"] = _media_sha256(candidate_path)
        atomic_copy(candidate_path, final_path)
        report_path = _edit_report_path(final_path)
        atomic_write_text(
            report_path,
            json.dumps(report, ensure_ascii=False, indent=2),
        )
        final_sha256 = _media_sha256(final_path)
        report_sha256 = _media_sha256(report_path)
        result["video_url"] = _versioned_final_url(final_path)
        result["final_video_sha256"] = final_sha256
        result["edit_report_sha256"] = report_sha256
        final_path.with_suffix(".stale").unlink(missing_ok=True)
        updated = conn.execute(
            """UPDATE episode_video_publish_leases SET status='published',updated_at=?
                 WHERE episode_id=? AND owner=? AND status='publishing'""",
            (now(), episode_id, owner),
        )
        if updated.rowcount != 1:
            raise ValueError("合片 publish owner CAS 冲突")
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _probe_concat_media(path: str | Path) -> dict[str, Any]:
    media_path = Path(path)
    if not media_path.is_file() or media_path.stat().st_size <= 0:
        raise ValueError("文件不存在或为空")
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=format_name,duration:stream=codec_type,duration",
                "-of", "json", str(media_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=_CONCAT_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"ffprobe 超过 {int(_CONCAT_PROBE_TIMEOUT_S)} 秒"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[-500:]
        raise ValueError("ffprobe 无法读取容器" + (f"：{detail}" if detail else "")) from exc
    except OSError as exc:
        raise ValueError(f"ffprobe 无法执行：{exc}") from exc
    try:
        payload = json.loads(completed.stdout or "{}")
        fmt = payload.get("format") or {}
        format_name = str(fmt.get("format_name") or "").strip().lower()
        duration_s = float(fmt.get("duration"))
        streams = payload.get("streams") or []
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("ffprobe 返回了无效的容器元数据") from exc
    if "mp4" not in format_name.split(","):
        raise ValueError(f"容器不是 MP4（format_name={format_name or 'unknown'}）")
    video_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    if not video_streams:
        raise ValueError("容器中没有视频流")
    # 容器 duration 取音视频流较长者：源片段音轨普遍比视频轨长几十毫秒，一旦
    # 采样率还混用，concat demuxer 用首段音频 timebase 解释后续包会把这个
    # 差值放大到秒级（EP3 即是如此）。视频流自身的 duration 不受音频影响，
    # 是拼接时长门唯一可信的权威基准。
    try:
        video_duration_s = float(video_streams[0].get("duration"))
    except (TypeError, ValueError) as exc:
        raise ValueError("ffprobe 未返回视频流时长") from exc
    if not math.isfinite(video_duration_s) or video_duration_s <= 0:
        raise ValueError(f"视频流时长无效（duration={video_duration_s!r}）")
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError(f"容器时长无效（duration={duration_s!r}）")
    has_audio = any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio"
        for stream in streams
    )
    return {
        "duration_s": duration_s,
        "video_duration_s": video_duration_s,
        "format_name": format_name,
        "has_audio": has_audio,
    }


def _validate_concat_output(
    path: str | Path,
    *,
    expected_duration_s: float,
    decode_timeout_s: float,
) -> float:
    try:
        probe = _probe_concat_media(path)
    except ValueError as exc:
        raise ValueError(
            f"合片产物容器/时长校验失败，上一版成片仍保留：{exc}"
        ) from exc
    if not math.isfinite(expected_duration_s) or expected_duration_s <= 0:
        raise ValueError("无法确定合片预期时长，上一版成片仍保留")
    # 用视频流时长做实测基准：容器 duration 取音视频流较长者，一旦音频未被
    # 完全对齐（残留亚帧级 apad/atrim 舍入）就会把音频的漂移重新记回时长门。
    actual_duration_s = float(probe["video_duration_s"])
    tolerance_s = max(
        _CONCAT_DURATION_TOLERANCE_MIN_S,
        expected_duration_s * _CONCAT_DURATION_TOLERANCE_RATIO,
    )
    if abs(actual_duration_s - expected_duration_s) > tolerance_s:
        raise ValueError(
            "合片产物时长异常，上一版成片仍保留："
            f"实测 {actual_duration_s:.3f}s，预期 {expected_duration_s:.3f}s，"
            f"允许误差 {tolerance_s:.3f}s"
        )
    try:
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-xerror",
                "-i", str(path), "-map", "0:v:0", "-f", "null", "-",
            ],
            check=True,
            capture_output=True,
            timeout=decode_timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"合片产物完整解码超过 {int(decode_timeout_s)} 秒，上一版成片仍保留"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()[-500:]
        raise ValueError(
            "合片产物完整解码失败，上一版成片仍保留"
            + (f"：{detail}" if detail else "")
        ) from exc
    except OSError as exc:
        raise ValueError(f"合片产物无法执行完整解码，上一版成片仍保留：{exc}") from exc
    return actual_duration_s


def _active_generation_shot_nos(conn, episode_id: str) -> list[int]:
    placeholders = ",".join("?" for _ in _ACTIVE_VIDEO_JOB_STATUSES)
    rows = conn.execute(
        f"""SELECT DISTINCT s.shot_no
              FROM jobs j JOIN shots s ON s.id=j.shot_id
             WHERE s.episode_id=? AND j.status IN ({placeholders})
             ORDER BY s.shot_no""",
        (episode_id, *_ACTIVE_VIDEO_JOB_STATUSES),
    ).fetchall()
    return [int(row["shot_no"]) for row in rows]


def _is_delivery_fallback(row) -> bool:
    if row is None:
        return False
    try:
        meta = json.loads(row["image_inputs"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(isinstance(meta, dict) and meta.get("delivery_fallback"))


def _playable_model_candidate(conn, shot_id: str):
    rows = conn.execute(
        """SELECT * FROM shot_versions
           WHERE shot_id=? AND status='succeeded' AND video_path IS NOT NULL
           ORDER BY version_no DESC""",
        (shot_id,),
    ).fetchall()
    return next(
        (
            row for row in rows
            if not _is_delivery_fallback(row)
            and row["video_path"]
            and Path(row["video_path"]).is_file()
        ),
        None,
    )


def _shot_has_valid_adopted_video(conn, adopted_version_id: str | None) -> bool:
    if not adopted_version_id:
        return False
    row = conn.execute(
        "SELECT * FROM shot_versions WHERE id=? AND status='succeeded'",
        (adopted_version_id,),
    ).fetchone()
    return bool(
        row and not _is_delivery_fallback(row)
        and row["video_path"] and Path(row["video_path"]).is_file()
    )


def _auto_adopt_playable_candidates_before_mix(episode_id: str) -> dict[str, Any]:
    """合成前把"有真实成功候选但未采纳"的镜头自动采纳为当前最新候选。

    用户原话："只要它读到视频生成完了就可以合成啊"——方案 B：合成时自动采纳
    每镜最新的成功版本。选版口径与 ``_playable_model_candidate`` 完全一致
    （status='succeeded' + video_path 落盘 + 非 delivery_fallback，按
    version_no 取最新），不另立标准。采纳动作本身必须走真实的人工采纳同一条
    核心逻辑（``app.api._adopt_version_core``，即 POST /shots/{id}/adopt 背后
    的实现）——技术门禁校验、evidence Artifact、gate_decisions 与
    review_action_audit 审计记录都照常落地，只是 reason 里写明这是成片合成时
    自动代采，不是直接 UPDATE shots 绕过既有校验与回执。

    每一镜的采纳是 ``_adopt_version_core`` 自己提交的独立事务；某一镜失败
    （技术门禁不过、review wall 拦截等）只把该镜记入跳过，不影响其余镜头
    已经成功提交的采纳，也不让整份合成失败——跳过路径见调用方后续的
    ``missing_model_shot_nos``/``skip_reasons`` 计算，口径不变。
    """
    from app import api
    from fastapi import HTTPException

    conn = get_conn()
    shot_rows = conn.execute(
        "SELECT id,shot_no,adopted_version_id FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    adopted_shot_nos: list[int] = []
    auto_adopt_failures: dict[str, str] = {}
    for shot in shot_rows:
        shot_id = str(shot["id"])
        shot_no = int(shot["shot_no"])
        if _shot_has_valid_adopted_video(conn, shot["adopted_version_id"]):
            continue
        candidate = _playable_model_candidate(conn, shot_id)
        if candidate is None:
            continue
        try:
            api._adopt_version_core(
                shot_id,
                {
                    "version_id": candidate["id"],
                    "reason": "成片合成时自动采纳该镜最新的成功技术校验候选（此前未人工采纳）",
                    "idempotency_key": f"auto-adopt-mix:{episode_id}:{shot_id}:{candidate['id']}",
                },
            )
        except (HTTPException, ValueError) as exc:
            # 回滚必须排在任何日志/记录之前：_adopt_version_core 中途失败时
            # conn 上可能还留着这一镜未提交的部分写入（例如 shots/shot_versions
            # 已 UPDATE 但 reconcile_adopted_revision 才抛错），不清掉会连累
            # 下一镜的采纳事务。
            if conn.in_transaction:
                conn.rollback()
            auto_adopt_failures[str(shot_no)] = f"自动采纳失败：{exc}"
            continue
        adopted_shot_nos.append(shot_no)
    return {
        "auto_adopted_shot_nos": adopted_shot_nos,
        "auto_adopt_failures": auto_adopt_failures,
    }


def episode_mix_status(episode_id: str) -> dict:
    """返回当前已有真实视频的可合成状态。"""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        return {"ready": False, "shots_total": 0, "shots_ready": 0, "shots": []}
    shots = rows_to_dicts(conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,)).fetchall())
    out = []
    for s in shots:
        vid = None
        v = None
        if s["adopted_version_id"]:
            v = conn.execute(
                "SELECT * FROM shot_versions WHERE id=? AND status='succeeded'",
                (s["adopted_version_id"],)).fetchone()
            if (
                v and not _is_delivery_fallback(v)
                and v["video_path"] and Path(v["video_path"]).is_file()
            ):
                vid = build_media_url(v["video_path"])
        playback_rate = float(v["playback_rate"] or 1.0) if vid and v else 1.0
        model_candidate = _playable_model_candidate(conn, s["id"])
        # 已采纳真实视频时用实测视频流时长而非分镜合约的名义 duration_s，
        # 使这里展示的预估口径与合成后 total_duration_s/edit_report.timeline
        # 的权威来源（视频流实测时长）保持一致，不再各算各的。探测失败（尚未
        # 真正落盘可读的视频）时退回名义估算，这里只是预览，不是交付门禁。
        effective_duration_s = round(float(s["duration_s"] or 0) / playback_rate, 2)
        if vid and v and v["video_path"]:
            try:
                measured_probe = _probe_concat_media(v["video_path"])
            except ValueError:
                pass
            else:
                effective_duration_s = round(
                    float(measured_probe["video_duration_s"]) / playback_rate, 2
                )
        out.append({"shot_id": s["id"], "shot_no": s["shot_no"],
                    "duration_s": s["duration_s"], "video_url": vid,
                    "has_adopted": bool(vid),
                    "has_model_candidate": bool(model_candidate),
                    "playback_rate": playback_rate,
                    "effective_duration_s": effective_duration_s})
    available = sum(1 for item in out if item["has_model_candidate"])
    skipped_shot_nos = [item["shot_no"] for item in out if not item["has_model_candidate"]]
    active_shot_nos = _active_generation_shot_nos(conn, episode_id)
    final_path = _final_video_path(ep["project_id"], ep["episode_no"])
    final_edit_report = _read_edit_report(final_path)
    final_timeline = (
        final_edit_report.get("timeline")
        if isinstance(final_edit_report, dict) else None
    )
    return {
        "episode_id": ep["id"],
        "title": ep["title"],
        "episode_no": ep["episode_no"],
        "shots_total": len(shots),
        "shots_ready": available,
        # 部分合成是主流程：任意一镜真实视频已落盘即可合成，
        # 其他缺镜/生成中镜头只做透明跳过，不生成图片占位。
        "ready": available > 0,
        "generation_active": bool(active_shot_nos),
        "active_shot_nos": active_shot_nos,
        "all_ready": len(shots) > 0 and available == len(shots),
        "shots_skipped": len(skipped_shot_nos),
        "skipped_shot_nos": skipped_shot_nos,
        "final_video_url": _existing_final_url(ep),
        "final_video_stale": _final_video_is_stale(ep),
        "final_is_partial": bool(
            isinstance(final_timeline, dict) and final_timeline.get("partial")
        ),
        "final_edit_report": final_edit_report,
        "shots": out,
    }


def _existing_final_url(ep_row) -> str | None:
    final_path = _final_video_path(ep_row["project_id"], ep_row["episode_no"])
    if final_path.exists():
        return _versioned_final_url(final_path)
    return None


def _versioned_final_url(final_path: Path) -> str:
    """返回随成品文件变化的 URL，避免重新合成后浏览器继续播放旧缓存。"""
    stat = final_path.stat()
    revision = f"{stat.st_mtime_ns}-{stat.st_size}"
    return build_media_url(final_path, version=revision)


def _final_video_is_stale(ep_row) -> bool:
    final_path = _final_video_path(ep_row["project_id"], ep_row["episode_no"])
    return final_path.is_file() and final_path.with_suffix(".stale").is_file()


def _final_video_path(project_id: str, episode_no: int) -> Path:
    d = config.PROJECTS_DIR / project_id / "episodes" / str(episode_no) / "final"
    d.mkdir(parents=True, exist_ok=True)
    return d / "episode.mp4"


def _edit_report_path(final_path: Path) -> Path:
    return final_path.with_name("episode.edit-report.json")


def _read_edit_report(final_path: Path) -> dict | None:
    path = _edit_report_path(final_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _final_edit_mode() -> str:
    import os

    mode = (os.getenv("MANJU_FINAL_EDIT_MODE") or "auto").strip().lower()
    if mode in {"always", "auto", "off"}:
        return mode
    if mode in {"1", "true", "yes", "on"}:
        return "always"
    if mode in {"0", "false", "no", "draft", "fast"}:
        return "off"
    return "auto"


def _final_edit_decision(
    conn,
    episode_id: str,
    piece_specs: list[tuple[int, str, float]],
    skipped_shot_nos: list[int],
) -> tuple[bool, str]:
    """Return whether the expensive final-edit pass is worth running now."""
    mode = _final_edit_mode()
    if mode == "always":
        return True, "forced_by_env"
    if mode == "off":
        return False, "disabled_by_env"
    if skipped_shot_nos:
        return False, "partial_timeline_fast_preview"

    from app.continuity import required_text_strategy
    from app.final_edit import shot_from_row, transition_spec

    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    shot_by_no = {int(row["shot_no"]): shot_from_row(row) for row in rows}
    ordered_shots = [
        shot_by_no[shot_no]
        for shot_no, _path, _rate in piece_specs
        if shot_no in shot_by_no
    ]

    for shot in ordered_shots:
        required = shot.required_text
        exact = (required.exact_text or "").strip() if required else ""
        if exact and required_text_strategy(shot) == "deterministic_insert":
            return True, "deterministic_text"

    for previous, current in zip(ordered_shots, ordered_shots[1:]):
        if current.shot_no == previous.shot_no + 1 and transition_spec(current.transition).edit_type != "cut":
            return True, "enhanced_transition"

    return False, "simple_timeline_fast_concat"


def concatenate_episode(
    episode_id: str,
    *,
    operation_idempotency_key: str | None = None,
    operation_request_fingerprint: str | None = None,
    operation_claim_token: str | None = None,
    operation_release_authority: dict[str, Any] | None = None,
    operation_video_delivery_manifest: dict[str, Any] | None = None,
) -> dict:
    """把当前已有的真实模型视频按镜号拼接成 MP4。

    只接受真实模型视频。内容 QA 低分不拦截，但静态图片、轻运动卡和静音片段
    不具备成片资格。缺镜或生成中镜头直接跳过；任何时候只要已有一镜
    真实视频就允许生成当前阶段成片。
    """
    from pathlib import Path as _P
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError("剧集不存在")
    if operation_idempotency_key is None:
        # 直接调用路径（测试/无幂等操作的兼容入口）：release_authority 与
        # video_delivery_manifest 都在下面就地现算，自动采纳必须先做，样本
        # 才会把这一轮新采纳的镜头算进去。命令总线路径
        # （capabilities/handlers/delivery.py::concatenate）在冻结这两份快照
        # 之前已经调用过同一个函数——这里不重复调用，避免采纳发生在冻结之后
        # 造成合片自己制造的"发布前已采纳视频发生漂移"。
        _auto_adopt_playable_candidates_before_mix(episode_id)
    from app.downstream_authority import verify_current_storyboard_release_authority

    release_authority = (
        operation_release_authority
        if operation_idempotency_key is not None
        else verify_current_storyboard_release_authority(episode_id, conn=conn)
    )
    if not isinstance(release_authority, dict):
        raise ConcatOperationConflict("合片操作缺少冻结的分镜发布权威")
    if not shutil.which("ffmpeg"):
        raise ValueError(
            "服务端未找到视频合成组件 ffmpeg；请安装 ffmpeg 或修正服务启动 PATH 后重试，"
            "本次未生成成片"
        )
    if not shutil.which("ffprobe"):
        raise ValueError(
            "服务端未找到视频校验组件 ffprobe；无法在替换前校验成片，"
            "本次未生成成片"
        )

    shot_rows = conn.execute(
        "SELECT id,shot_no FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    shot_id_by_no = {int(row["shot_no"]): row["id"] for row in shot_rows}

    from app.downstream_authority import current_partial_adopted_video_delivery_manifest

    # video_delivery_manifest 只服务于合片操作的幂等/CAS 漂移检测（"发布时
    # 用的还是不是取样时那批已采纳视频"），不是入选候选的判据——入选判据见
    # 下面 pieces 的注释。用容错版本（single-shot 失效只跳过那一镜、不让整
    # 份清单计算失败），否则同样的悬空采纳会在这里再次把整次合成打死。
    video_delivery_manifest = (
        operation_video_delivery_manifest
        if operation_idempotency_key is not None
        else current_partial_adopted_video_delivery_manifest(episode_id, conn=conn)
    )
    if not isinstance(video_delivery_manifest, dict):
        raise ConcatOperationConflict("合片操作缺少冻结的已采纳视频清单")
    _assert_concat_sources_current(
        conn,
        episode_id=episode_id,
        release_authority=release_authority,
        video_delivery_manifest=video_delivery_manifest,
    )
    skip_reasons: dict[str, str] = dict(video_delivery_manifest.get("skip_reasons") or {})
    # missing_model_shot_nos：这些镜头连一个成功产出、真实落盘的模型视频候选
    # 都没有（从没生成、生成中、或生成失败）。这是"部分合成是主流程"的判据
    # 本体——任意一镜真实视频已落盘即可合成，其余透明跳过，不拖垮整份成片；
    # 与 episode_mix_status() 的 `ready = available > 0` 同一条判据，不是
    # 另立标准。
    missing_model_shot_nos = sorted(
        shot_no for shot_no, shot_id in shot_id_by_no.items()
        if _playable_model_candidate(conn, shot_id) is None
    )
    pieces = _adopted_video_paths(episode_id)
    if not pieces:
        raise ValueError(
            "本集当前还没有任何可播放的真实模型视频，上一版成片仍保留；"
            "不会使用静态图片或静音片段冒充成片。请先在生成台完成至少一镜的"
            "视频生成并等待其落盘后重试"
        )

    from app.video_playback import normalize_playback_rate

    rate_rows = conn.execute(
        """SELECT s.shot_no, v.playback_rate
           FROM shots s JOIN shot_versions v ON v.id=s.adopted_version_id
           WHERE s.episode_id=?""",
        (episode_id,),
    ).fetchall()
    rate_by_shot = {
        int(row["shot_no"]): normalize_playback_rate(row["playback_rate"])
        for row in rate_rows
    }
    piece_specs_candidates = [
        (int(shot_no), path, rate_by_shot.get(int(shot_no), 1.0))
        for shot_no, path in pieces
    ]
    # 逐候选做容器/时长可探测性预检：技术校验的正确作用是决定哪些镜头够格
    # 入选候选集，不是让整份合成失败——探测失败的候选被排除出候选集并记入
    # 跳过原因，其余合法候选继续参与拼接。这里探测一次后把结果传给下游的
    # 归一化/拼接阶段复用，不重复调用 ffprobe。
    piece_specs: list[tuple[int, str, float]] = []
    probe_by_shot: dict[int, dict[str, Any]] = {}
    for shot_no, vpath, rate in piece_specs_candidates:
        try:
            probe_by_shot[shot_no] = _probe_concat_media(vpath)
        except ValueError as exc:
            skip_reasons[str(shot_no)] = f"源片段容器/时长校验失败：{exc}"
            continue
        piece_specs.append((shot_no, vpath, rate))
    piece_shot_nos = [shot_no for shot_no, _path, _rate in piece_specs]
    all_shot_nos = [
        int(row["shot_no"])
        for row in conn.execute(
            "SELECT shot_no FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
        ).fetchall()
    ]
    skipped_shot_nos = [shot_no for shot_no in all_shot_nos if shot_no not in set(piece_shot_nos)]
    # skip_reasons 只保留这次真正被跳过的镜头，并给没有更具体原因（既没有
    # manifest 权威校验失败、也没有容器探测失败——单纯是从没生成/仍在生成中）
    # 的镜头补一条默认说明，不让前端拿到一个对不上号或缺失原因的字典。
    skip_reasons = {
        str(shot_no): skip_reasons.get(str(shot_no), "尚无已采纳且落盘可播放的真实视频")
        for shot_no in skipped_shot_nos
    }
    if not piece_specs:
        raise ValueError(
            "本集当前没有任何镜头的源片段能通过容器/时长校验，上一版成片仍保留；"
            "不会使用静态图片或静音片段冒充成片。可稍后重试，或在生成台重新生成"
            "对应镜头"
        )
    # 拿不到实测时长时，只累加本次真正参与拼接的镜头时长。
    duration_by_shot = {
        int(row["shot_no"]): float(row["duration_s"] or 0)
        for row in conn.execute(
            "SELECT shot_no,duration_s FROM shots WHERE episode_id=?", (episode_id,),
        ).fetchall()
    }
    est_total_dur = sum(
        duration_by_shot.get(shot_no, 0.0) / rate
        for shot_no, _path, rate in piece_specs
    )
    concat_timeout_s = min(1800.0, max(120.0, est_total_dur * 10.0 + 60.0))

    final_path = _final_video_path(ep["project_id"], ep["episode_no"])
    started_at = time.perf_counter()
    common_result = {
        "shots": len(piece_specs),
        "ffmpeg_missing": False,
        "shots_total": len(all_shot_nos),
        "shots_skipped": len(skipped_shot_nos),
        "skipped_shot_nos": skipped_shot_nos,
        "missing_model_shot_nos": missing_model_shot_nos,
        "skip_reasons": skip_reasons,
        "included_shot_nos": piece_shot_nos,
        "partial": bool(skipped_shot_nos),
        "final_video_stale": False,
        "fallback_shots_created": 0,
        "fallback_shots_reused": 0,
        "playback_rates": {str(no): rate for no, _path, rate in piece_specs},
        "storyboard_release_authority": release_authority,
        "video_delivery_manifest": video_delivery_manifest,
    }

    # final-edit 是质量增强层，不是交付门禁。任何字体/滤镜/转场失败都回退到
    # 下方的传统硬拼，上一版成片仍在原子替换成功前保持可用。
    final_edit_failure: str | None = None
    final_edit_enabled, final_edit_reason = _final_edit_decision(
        conn,
        episode_id,
        piece_specs,
        skipped_shot_nos,
    )
    final_edit_elapsed_s: float | None = None
    if final_edit_enabled:
        final_edit_started_at = time.perf_counter()
        try:
            from app.final_edit import render_episode_final_edit

            with tempfile.TemporaryDirectory() as edit_td:
                edit_dir = _P(edit_td)
                edited_video = edit_dir / "final-edit.mp4"
                edit_report = render_episode_final_edit(
                    conn,
                    episode_id,
                    piece_specs,
                    edited_video,
                    edit_dir,
                )
                edit_report["timeline"] = {
                    "partial": bool(skipped_shot_nos),
                    "shots_total": len(all_shot_nos),
                    "included_shot_nos": piece_shot_nos,
                    "skipped_shot_nos": skipped_shot_nos,
                    "missing_model_shot_nos": missing_model_shot_nos,
                    "skip_reasons": skip_reasons,
                }
                edit_report["mode"] = "final_edit"
                edit_report["video_delivery_manifest"] = video_delivery_manifest
                edit_report["video_delivery_manifest_hash"] = video_delivery_manifest[
                    "manifest_hash"
                ]
                edit_report["decision_reason"] = final_edit_reason
                edit_report["elapsed_s"] = round(time.perf_counter() - final_edit_started_at, 3)
                validated_duration_s = _validate_concat_output(
                    edited_video,
                    expected_duration_s=float(edit_report["total_duration_s"]),
                    decode_timeout_s=concat_timeout_s,
                )
                result = {
                    "total_duration_s": round(validated_duration_s, 1),
                    **common_result,
                    "elapsed_s": round(time.perf_counter() - started_at, 3),
                    "final_edit": edit_report,
                }
                _publish_concat_output(
                    conn,
                    episode_id=episode_id,
                    candidate_path=edited_video,
                    final_path=final_path,
                    report=edit_report,
                    result=result,
                    release_authority=release_authority,
                    video_delivery_manifest=video_delivery_manifest,
                    operation_idempotency_key=operation_idempotency_key,
                    operation_request_fingerprint=operation_request_fingerprint,
                    operation_claim_token=operation_claim_token,
                )
            return result
        except Exception as exc:  # noqa: BLE001 - 质量增强失败必须继续完整交付
            final_edit_elapsed_s = time.perf_counter() - final_edit_started_at
            final_edit_failure = f"{type(exc).__name__}: {exc}"[:1000]

    # probe_by_shot 已在候选预检阶段对每个入选片段探测过一次（未通过的候选
    # 已被排除并记入 skip_reasons，不会走到这里）；这里直接复用，不重复调用
    # ffprobe，也不会再因为单个片段的问题让整份合成失败。
    measured_total_dur = 0.0
    for shot_no, vpath, rate in piece_specs:
        source_probe = probe_by_shot[shot_no]
        # 预期时长以视频流为准：容器 duration 取音视频流较长者，源片段音轨普遍
        # 比视频轨长几十毫秒，拿它当预期基准会把音频的漂移错记成时长膨胀。
        measured_total_dur += float(source_probe["video_duration_s"]) / rate

    from app.final_edit import FINAL_AUDIO_RATE, audio_normalize_filter

    # 用 concat demuxer 优先无重编码直粘（画质无损）；但 -c copy 要求各片段编码参数
    # （像素格式/timebase/SAR/profile）完全一致，否则会失败或花屏。一旦失败，回退重编码兜底。
    #
    # 拼接前逐镜先把音频归一：模型产出的源片段音轨是真实录音（不是无声占位），
    # 采样率并不保证一致，逐镜时长也和视频流有几十毫秒的量级偏差。不归一直接
    # -c copy 直粘，concat demuxer 会用首段音频的 timebase 解释后续所有音频包，
    # 采样率一旦混用即把时长拉伸到秒级（超出时长门容差）；即使采样率一致，
    # 逐镜偏差也会累积成音画不同步。draft 与 final_edit 共用同一套音频归一
    # 逻辑（app.final_edit.audio_normalize_filter），不允许只有一条路径正确。
    with tempfile.TemporaryDirectory() as td:
        listfile = _P(td) / "list.txt"
        lines = []
        prepared_specs: list[tuple[int, str, float]] = []
        for shot_no, vpath, rate in piece_specs:
            source_probe = probe_by_shot[shot_no]
            video_duration_s = float(source_probe["video_duration_s"])
            has_audio = bool(source_probe["has_audio"])
            effective_duration = max(0.05, video_duration_s / rate)
            speed_change = abs(rate - 1.0) > 0.0001
            prepared_path = _P(td) / f"shot-{shot_no}-norm.mp4"
            inputs = ["-i", vpath]
            audio_label = "0:a"
            if not has_audio:
                inputs += [
                    "-f", "lavfi", "-t", f"{effective_duration:.6f}",
                    "-i", f"anullsrc=channel_layout=stereo:sample_rate={FINAL_AUDIO_RATE}",
                ]
                audio_label = "1:a"
            video_args = (
                ["-vf", f"setpts=PTS/{rate:.6f}", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "18", "-pix_fmt", "yuv420p"]
                if speed_change else ["-c:v", "copy"]
            )
            audio_filter = audio_normalize_filter(
                atempo_rate=rate if (speed_change and has_audio) else None,
                duration_s=effective_duration,
            )
            prepare_cmd = [
                "ffmpeg", "-y", "-loglevel", "error", *inputs,
                "-map", "0:v:0", "-map", audio_label,
                *video_args,
                "-af", audio_filter,
                "-c:a", "aac", "-ar", str(FINAL_AUDIO_RATE),
                "-movflags", "+faststart", str(prepared_path),
            ]
            try:
                subprocess.run(
                    prepare_cmd, check=True, capture_output=True, timeout=concat_timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                raise ValueError(
                    f"镜 {shot_no} 的音视频归一处理超时；上一版成片仍保留，可稍后重试"
                ) from exc
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or b"").decode("utf-8", "replace")[-500:]
                raise ValueError(
                    f"镜 {shot_no} 的音视频归一处理失败；上一版成片仍保留"
                    + (f"：{detail}" if detail else "")
                ) from exc
            if not prepared_path.is_file() or prepared_path.stat().st_size <= 0:
                raise ValueError(
                    f"镜 {shot_no} 的音视频归一处理未产出有效片段；上一版成片仍保留"
                )
            prepared_specs.append((shot_no, str(prepared_path), rate))
            # concat demuxer 要求绝对路径并转义单引号
            safe = str(prepared_path).replace("'", "'\\''")
            lines.append(f"file '{safe}'")
        listfile.write_text("\n".join(lines), encoding="utf-8")
        concat_output = _P(td) / "concat.mp4"
        concat_in = ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "concat", "-safe", "0", "-i", str(listfile)]
        try:
            subprocess.run(
                concat_in + ["-c", "copy", "-movflags", "+faststart", str(concat_output)],
                check=True, capture_output=True, timeout=concat_timeout_s)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # 片段编码参数不一致导致 -c copy 失败 → 重编码兜底（画质损失极小，但保证能拼成整集）。
            # 拼接输入已经是上面逐镜归一过的片段，这里显式声明相同的音频目标规格，
            # 重编码回退与 -c copy 快速路径共享同一套音频归一结果，不会走了兜底就漏归一。
            try:
                subprocess.run(
                    concat_in + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", str(FINAL_AUDIO_RATE),
                                 "-movflags", "+faststart", str(concat_output)],
                    check=True, capture_output=True, timeout=concat_timeout_s)
            except subprocess.TimeoutExpired as exc:
                raise ValueError(
                    f"整集合成超过 {int(concat_timeout_s)} 秒，已停止本次任务；上一版成片仍保留，可稍后重试"
                ) from exc
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or b"").decode("utf-8", "replace")[-500:]
                raise ValueError(
                    "整集合成失败，上一版成片仍保留，可检查片段后重试"
                    + (f"：{detail}" if detail else "")
                ) from exc
        if not concat_output.is_file() or concat_output.stat().st_size <= 0:
            raise ValueError("ffmpeg 未产出有效成片，上一版成片仍保留，可检查片段后重试")
        total_dur = _validate_concat_output(
            concat_output,
            expected_duration_s=measured_total_dur,
            decode_timeout_s=concat_timeout_s,
        )
        publish_candidate = final_path.with_name(
            f".{final_path.name}.{new_id('candidate')}.tmp"
        )
        atomic_copy(concat_output, publish_candidate)

    fallback_edit_report = {
        "ok": False,
        "mode": "draft_concat",
        "fallback": "draft_concat",
        "error": final_edit_failure,
        "skipped_final_edit": not final_edit_enabled,
        "decision_reason": final_edit_reason,
        "final_edit_elapsed_s": round(final_edit_elapsed_s, 3) if final_edit_elapsed_s is not None else None,
        "elapsed_s": round(time.perf_counter() - started_at, 3),
        "runtime_blocking": False,
        "timeline": {
            "partial": bool(skipped_shot_nos),
            "shots_total": len(all_shot_nos),
            "included_shot_nos": piece_shot_nos,
            "skipped_shot_nos": skipped_shot_nos,
            "missing_model_shot_nos": missing_model_shot_nos,
            "skip_reasons": skip_reasons,
        },
        "video_delivery_manifest": video_delivery_manifest,
        "video_delivery_manifest_hash": video_delivery_manifest["manifest_hash"],
    }
    result = {
        "total_duration_s": round(total_dur, 1),
        **common_result,
        "elapsed_s": round(time.perf_counter() - started_at, 3),
        "final_edit": fallback_edit_report,
    }
    try:
        _publish_concat_output(
            conn,
            episode_id=episode_id,
            candidate_path=publish_candidate,
            final_path=final_path,
            report=fallback_edit_report,
            result=result,
            release_authority=release_authority,
            video_delivery_manifest=video_delivery_manifest,
            operation_idempotency_key=operation_idempotency_key,
            operation_request_fingerprint=operation_request_fingerprint,
            operation_claim_token=operation_claim_token,
        )
    finally:
        publish_candidate.unlink(missing_ok=True)
    return result

__all__ = [name for name in globals() if not name.startswith("__")]
