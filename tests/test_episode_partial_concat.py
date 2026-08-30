import json
import sqlite3
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from app import api, artifacts, db, worker
from tests.conftest import patch_worker_everywhere, patch_api_everywhere
from app.video_playback import normalize_playback_rate


@pytest.fixture(autouse=True)
def _published_authority_for_concat_mechanics(monkeypatch):
    from app import downstream_authority

    monkeypatch.setattr(
        downstream_authority,
        "verify_current_storyboard_release_authority",
        lambda episode_id, conn=None: {
            "published_storyboard_artifact_id": f"storyboard:{episode_id}",
            "release_qualification_hash": "release-current",
        },
    )

    def video_manifest(episode_id, conn=None):
        rows = (conn or worker.get_conn()).execute(
            """SELECT s.id,s.shot_no,s.adopted_version_id,v.playback_rate,v.video_path
                 FROM shots s LEFT JOIN shot_versions v ON v.id=s.adopted_version_id
                WHERE s.episode_id=? ORDER BY s.shot_no""",
            (episode_id,),
        ).fetchall()
        items = [
            {
                "shot_id": row["id"],
                "shot_no": row["shot_no"],
                "adopted_version_id": row["adopted_version_id"],
                "playback_rate": float(row["playback_rate"] or 1),
                "video_path": row["video_path"],
            }
            for row in rows
        ]
        return {"manifest_hash": json.dumps(items, sort_keys=True), "items": items}

    monkeypatch.setattr(
        downstream_authority,
        "current_adopted_video_delivery_manifest",
        video_manifest,
    )
    # concatenate_episode 现在为幂等/CAS 漂移检测计算的是容错版本清单（单镜
    # 权威失效只跳过那一镜，不整份失败），入选候选的判据仍是
    # _adopted_video_paths；这里给它接上同一套"机制测试"用的直通 mock，不
    # 让真实实现里的 Artifact/evaluations 强校验干扰这些聚焦拼接机制的用例。
    monkeypatch.setattr(
        downstream_authority,
        "current_partial_adopted_video_delivery_manifest",
        video_manifest,
    )


def _probe_result(duration_s: float, *, has_audio: bool = True) -> SimpleNamespace:
    streams = [{"codec_type": "video", "duration": str(duration_s)}]
    if has_audio:
        # 真实模型产出的源片段一律带真实音轨（不是无声占位），默认建模成有音频，
        # 这样测出的行为覆盖 draft_concat 的常规路径而不是已废弃的"静音视频"假设。
        streams.append({"codec_type": "audio", "duration": str(duration_s), "sample_rate": "44100"})
    return SimpleNamespace(
        stdout=json.dumps({
            "format": {
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration": str(duration_s),
            },
            "streams": streams,
        }),
        stderr="",
    )


def _database(shot_nos: tuple[int, ...] = (1, 2, 3), *, db_path: Path | None = None) -> sqlite3.Connection:
    # db_path 给需要"用第二条独立连接读盘上数据"的用例用——:memory: 私有于
    # 单个连接对象，验证不了真提交是否落盘；传入真实文件路径时可以另开一条
    # sqlite3.connect() 独立核对，而不是用写入的同一条连接自证。
    conn = sqlite3.connect(str(db_path) if db_path is not None else ":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) "
        "VALUES('e','p',1,'E','confirmed',0)"
    )
    for shot_no in shot_nos:
        conn.execute(
            "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES(?,?,?,?)",
            (f"s{shot_no}", "e", shot_no, 5 + shot_no),
        )
    conn.commit()
    return conn


def _seed_current_delivery(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO artifacts(
               id,type,scope_type,scope_id,version,status,trust_level,
               content_json,content_hash,contract_version,created_at
           ) VALUES('delivery-art','delivery_package','episode','e',1,'approved','T5',
                    '{}','hash','delivery-1.0.0',0)"""
    )
    conn.execute(
        """INSERT INTO delivery_packages(
               id,episode_id,artifact_id,status,package_path,manifest_json,
               quality_report_json,known_issues,created_at
           ) VALUES('delivery-pkg','e','delivery-art','approved','/tmp/delivery-pkg',
                    '{}','{}','',0)"""
    )
    conn.execute(
        """UPDATE episodes
              SET delivery_artifact_id='delivery-art',delivery_status='approved'
            WHERE id='e'"""
    )


def _version(conn: sqlite3.Connection, *, shot_no: int, path: Path, adopted: bool) -> None:
    version_id = f"v{shot_no}"
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
           ) VALUES(?,?,?,?,?,'succeeded',?,0)""",
        (version_id, f"s{shot_no}", 1, "prompt", f"key-{shot_no}", str(path)),
    )
    if adopted:
        conn.execute(
            "UPDATE shots SET adopted_version_id=? WHERE id=?",
            (version_id, f"s{shot_no}"),
        )


def test_partial_episode_is_ready_when_any_real_video_exists_but_ffmpeg_is_still_required(
    tmp_path, monkeypatch,
) -> None:
    conn = _database()
    project_root = tmp_path / "projects"
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)
    adopted_path = shot_dir / "shot-1.mp4"
    unadopted_path = shot_dir / "shot-2.mp4"
    missing_path = shot_dir / "shot-3-missing.mp4"
    adopted_path.write_bytes(b"adopted")
    unadopted_path.write_bytes(b"unadopted")
    _version(conn, shot_no=1, path=adopted_path, adopted=True)
    _version(conn, shot_no=2, path=unadopted_path, adopted=False)
    _version(conn, shot_no=3, path=missing_path, adopted=True)
    conn.commit()

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: None)

    status = worker.episode_mix_status("e")

    assert status["ready"] is True
    assert status["all_ready"] is False
    assert status["shots_ready"] == 2
    assert status["shots_skipped"] == 1
    assert status["skipped_shot_nos"] == [3]
    assert [item["shot_no"] for item in status["shots"] if item["has_adopted"]] == [1]

    try:
        worker.concatenate_episode("e")
    except ValueError as exc:
        assert "未找到视频合成组件 ffmpeg" in str(exc)
        assert "本次未生成成片" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("缺少 ffmpeg 时不得把首个片段冒充最终成片")


def test_concat_produces_partial_output_while_other_shots_are_still_generating(
    tmp_path, monkeypatch,
) -> None:
    """部分合成是主流程：其余镜头仍在生成中不得拖垮已落盘镜头的合成。

    用户明确要求拆掉"任意一镜没有已采纳有效视频就整份失败"的拦截
    （CON-409 · 镜 3 缺少已采纳的有效视频权威一类的报错）。这里镜 2 仍在
    provider 侧生成中，没有任何已采纳视频；合成必须只用镜 1 产出部分成片，
    把镜 2 记入 skipped_shot_nos，而不是整次失败。
    """
    from app.evidence import media as media_evidence
    from app import final_edit

    conn = _database((1, 2))
    project_root = tmp_path / "projects"
    completed_path = project_root / "p" / "episodes" / "1" / "shots" / "shot-1.mp4"
    completed_path.parent.mkdir(parents=True)
    completed_path.write_bytes(b"completed-real-video")
    _version(conn, shot_no=1, path=completed_path, adopted=True)
    conn.execute("UPDATE shots SET transition='闪白' WHERE id='s2'")
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v_running','s2',1,'prompt','running-key','running',0)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at
           ) VALUES('j_running','video','s2','v_running','e','p','waiting_provider',0,0)"""
    )
    conn.commit()
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    monkeypatch.setattr(
        final_edit,
        "render_episode_final_edit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("partial preview must stay fast")),
    )

    def successful_run(command, **_kwargs):
        if command[0] == "ffprobe":
            return _probe_result(5.0)
        if command[-1] == "-":
            return SimpleNamespace(stdout=b"", stderr=b"")
        Path(command[-1]).write_bytes(b"partial-final")
        return SimpleNamespace(stdout="", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", successful_run)

    status = worker.episode_mix_status("e")

    assert status["ready"] is True
    assert status["shots_ready"] == 1
    assert status["generation_active"] is True
    assert status["active_shot_nos"] == [2]

    result = worker.concatenate_episode("e")

    assert result["partial"] is True
    assert result["included_shot_nos"] == [1]
    assert result["skipped_shot_nos"] == [2]
    assert result["missing_model_shot_nos"] == [2]
    assert result["skip_reasons"]["2"]
    # final_edit 的"部分时间线走快速预览"决策路径没有被跳过检测短路掉；
    # render_episode_final_edit 若被误调用会在 monkeypatch 里直接抛断言错误。
    assert result["final_edit"]["skipped_final_edit"] is True
    assert conn.execute("SELECT COUNT(*) FROM shot_versions").fetchone()[0] == 2


def test_full_episode_with_real_transition_still_uses_final_edit(tmp_path, monkeypatch) -> None:
    from app import final_edit
    from app.evidence import media as media_evidence

    conn = _database((1, 2))
    project_root = tmp_path / "projects"
    for shot_no in (1, 2):
        path = project_root / "p" / "episodes" / "1" / "shots" / f"shot-{shot_no}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"real-video-{shot_no}".encode())
        _version(conn, shot_no=shot_no, path=path, adopted=True)
    conn.execute("UPDATE shots SET transition='闪白' WHERE id='s2'")
    conn.commit()

    calls: list[list[tuple[int, str, float]]] = []

    def fake_render_episode_final_edit(_conn, _episode_id, piece_specs, destination, _work_dir):
        calls.append(piece_specs)
        Path(destination).write_bytes(b"edited-final")
        return {
            "ok": True,
            "prepared_shots": len(piece_specs),
            "total_duration_s": 10.0,
            "transitions": [{"edit_type": "dip_white"}],
        }

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(final_edit, "render_episode_final_edit", fake_render_episode_final_edit)

    def successful_validation(command, **_kwargs):
        if command[0] == "ffprobe":
            return _probe_result(10.0)
        if command[-1] == "-":
            return SimpleNamespace(stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(worker.subprocess, "run", successful_validation)

    result = worker.concatenate_episode("e")

    assert calls == [[(1, str(project_root / "p" / "episodes" / "1" / "shots" / "shot-1.mp4"), 1.0),
                      (2, str(project_root / "p" / "episodes" / "1" / "shots" / "shot-2.mp4"), 1.0)]]
    assert result["partial"] is False
    assert result["final_edit"]["mode"] == "final_edit"
    assert result["final_edit"]["decision_reason"] == "enhanced_transition"
    assert (project_root / "p" / "episodes" / "1" / "final" / "episode.mp4").read_bytes() == b"edited-final"


def test_episode_without_adoption_auto_adopts_playable_candidates_before_mix(tmp_path, monkeypatch) -> None:
    """方案 B（用户已拍板）：合成时自动采纳每镜最新的成功版本。

    历史注记——这条用例曾经叫
    ``test_episode_without_adoption_never_force_adopts_candidates_before_mix``，
    钉住的是相反的行为（合成绝不自动采纳候选，一镜都没采纳就直接拒绝合成）。
    那条断言对应的是 2026-08-11 a4f38df 引入的"当前分镜仍有镜头未采纳…禁止
    生成部分成片"整份失败闸门；本文件同一提交把它进一步锁死成"绝不能在这个
    过程里悄悄帮用户把候选采纳掉"。追史发现这不是一个经过权衡后刻意保留的
    约束：2026-08-03 的实施检查点（PRD/漫剧成片质量、镜头连续性与文字画面
    整改PRD.md §4.1）已经把"未采纳但真实可播放的模型候选先自动择优"写成
    "已落地"的设计事实，对应实现正是 0f9b83c 里
    ``select_best_video_candidate(force_best=True)`` 那段——a4f38df 是当天
    夜里的自动保存提交，删掉了这段代码又没有回改 PRD，也没有任何提交信息
    解释为什么要收紧。frontend/src/pages/CinemaPage.tsx 的按钮提示与确认弹窗
    文案（"未采纳但已有可播放模型候选的分镜会先自动择优"）自那以后从未被
    改过，说明前端一直在对用户承诺这条行为，只是后端没兑现——即用户在本轮
    报告的"12 镜全部有视频、只有 4 镜被采纳"正是这个回归的真实后果。
    真实数据实测（第三集 ep_f0f0b4d4abef）显示同一缺口：12 镜全部生成成功，
    只有 4 镜被采纳，用户原话"只要它读到视频生成完了就可以合成啊"。本用例
    把断言反过来钉住新行为：技术校验通过、非 delivery_fallback、文件落盘的
    候选必须在合成前自动采纳，且必须走真实的 ``_adopt_version_core``（而不是
    绕过审计的裸 UPDATE）——技术门禁、evidence Artifact 与审计记录都要留痕。
    """
    from app.evidence import media as media_evidence
    from app.evidence import repository

    conn = _database()
    project_root = tmp_path / "projects"
    ftyp_bytes = b"\x00\x00\x00\x18ftypmp42" + b"x" * 64
    for shot_no in (1, 2, 3):
        path = project_root / "p" / "episodes" / "1" / "shots" / f"shot-{shot_no}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(ftyp_bytes)
        _version(conn, shot_no=shot_no, path=path, adopted=False)
        conn.execute(
            "UPDATE shot_versions SET technical_validation_json=?,qa_json=? WHERE id=?",
            ('{"passed":true}', '{"overall":0.2,"contract_facts":[]}', f"v{shot_no}"),
        )
    conn.commit()
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    # 上游剧本/分镜评审墙资格与本用例无关（专注合成时自动采纳的机制），比照
    # 本文件其它用例对 downstream_authority 的直通 mock 原则同样直通。
    patch_api_everywhere(monkeypatch, "_review_assert_shot_positive", lambda *a, **k: {})
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def successful_run(command, **_kwargs):
        if command[0] == "ffprobe":
            duration_s = 15.0 if Path(command[-1]).name == "concat.mp4" else 5.0
            return _probe_result(duration_s)
        if command[-1] == "-":
            return SimpleNamespace(stdout=b"", stderr=b"")
        Path(command[-1]).write_bytes(b"generated-video")
        return SimpleNamespace(stdout="", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", successful_run)

    status = worker.episode_mix_status("e")
    assert status["ready"] is True
    assert status["shots_ready"] == 3

    # 三镜都只有未采纳候选（has_model_candidate 为真但 has_adopted 为假）；
    # 方案 B：合成必须把三镜都自动采纳为各自的最新成功候选，全部入选，不再
    # 因为"没有已采纳视频"整体拒绝。
    result = worker.concatenate_episode("e")

    assert result["shots"] == 3
    assert result["skipped_shot_nos"] == []
    assert conn.execute(
        "SELECT COUNT(*) FROM shots WHERE episode_id='e' AND adopted_version_id IS NOT NULL"
    ).fetchone()[0] == 3
    # 采纳必须走真实的 _adopt_version_core，留下可追溯的审计记录，而不是绕过
    # 审计的裸 UPDATE。
    audit_reasons = [
        row[0] for row in conn.execute(
            "SELECT reason FROM review_action_audit WHERE action='video_version.adopt'"
        ).fetchall()
    ]
    assert len(audit_reasons) == 3
    assert all("成片合成时自动采纳" in reason for reason in audit_reasons)


def test_concat_refuses_only_when_no_real_video_exists_and_never_creates_image_fallback(
    tmp_path, monkeypatch,
) -> None:
    conn = _database((1,))
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    status = worker.episode_mix_status("e")
    assert status["ready"] is False

    try:
        worker.concatenate_episode("e")
    except ValueError as exc:
        assert "没有任何可播放的真实模型视频" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("缺少真实模型视频时不得创建图片兜底")

    assert conn.execute("SELECT COUNT(*) FROM shot_versions").fetchone()[0] == 0
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()[0] is None


def test_legacy_image_fallback_cannot_outrank_or_grade_over_real_video(
    tmp_path, monkeypatch,
) -> None:
    from app.evidence import media as media_evidence

    conn = _database((1,))
    project_root = tmp_path / "projects"
    real_path = project_root / "p" / "episodes" / "1" / "shots" / "real-v1.mp4"
    fallback_path = project_root / "p" / "episodes" / "1" / "shots" / "fallback-v2.mp4"
    real_path.parent.mkdir(parents=True)
    real_path.write_bytes(b"real-model-video")
    fallback_path.write_bytes(b"static-silent-placeholder")
    _version(conn, shot_no=1, path=real_path, adopted=False)
    conn.execute(
        "UPDATE shot_versions SET technical_validation_json=?,qa_json=? WHERE id='v1'",
        ('{"passed":true}', '{"overall":0.18,"contract_facts":["action_match_below_contract"]}'),
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,image_inputs,
               technical_validation_json,qa_json,created_at
           ) VALUES('fallback-v2','s1',2,'fallback','fallback-key','succeeded',?,?,?, ?,0)""",
        (
            str(fallback_path),
            '{"delivery_fallback":true}',
            '{"passed":true}',
            '{"overall":1.0,"contract_facts":[]}',
        ),
    )
    conn.execute("UPDATE shots SET adopted_version_id='fallback-v2' WHERE id='s1'")
    conn.commit()

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)

    before = worker.episode_mix_status("e")
    assert before["shots_ready"] == 1
    assert before["shots"][0]["has_model_candidate"] is True
    assert media_evidence.grade_shot_video("s1")["version_id"] == "v1"

    selected = media_evidence.select_best_video_candidate("s1", force_best=True)

    assert selected is not None
    assert selected["version_id"] == "v1"
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'"
    ).fetchone()[0] == "v1"


def test_database_startup_quarantines_legacy_static_fallback_without_deleting_it() -> None:
    conn = _database((1,))
    _seed_current_delivery(conn)
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('fallback-v1','s1',1,'fallback','fallback-key','succeeded',?,0)""",
        ('{"delivery_fallback":true}',),
    )
    conn.execute("UPDATE shots SET adopted_version_id='fallback-v1' WHERE id='s1'")
    conn.commit()

    changed = db._quarantine_static_delivery_fallbacks(conn)
    conn.commit()

    assert changed == 1
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'"
    ).fetchone()[0] is None
    row = conn.execute(
        "SELECT status,error FROM shot_versions WHERE id='fallback-v1'"
    ).fetchone()
    assert row["status"] == "rejected_static_fallback"
    assert "不具备视频资格" in row["error"]
    assert conn.execute(
        "SELECT COUNT(*) FROM shot_versions WHERE id='fallback-v1'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT status FROM delivery_packages WHERE id='delivery-pkg'"
    ).fetchone()[0] == "superseded"
    assert conn.execute(
        "SELECT status FROM artifacts WHERE id='delivery-art'"
    ).fetchone()[0] == "superseded"
    assert conn.execute(
        "SELECT delivery_artifact_id FROM episodes WHERE id='e'"
    ).fetchone()[0] is None


def test_database_startup_keeps_delivery_when_fallback_is_only_history() -> None:
    conn = _database((1,))
    _seed_current_delivery(conn)
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('fallback-history','s1',1,'fallback','fallback-key','succeeded',?,0)""",
        ('{"delivery_fallback":true}',),
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('real-v1','s1',2,'real','real-key','succeeded','{}',0)"""
    )
    conn.execute("UPDATE shots SET adopted_version_id='real-v1' WHERE id='s1'")
    conn.commit()

    assert db._quarantine_static_delivery_fallbacks(conn) == 1
    conn.commit()

    assert conn.execute(
        "SELECT status FROM shot_versions WHERE id='fallback-history'"
    ).fetchone()[0] == "rejected_static_fallback"
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'"
    ).fetchone()[0] == "real-v1"
    assert conn.execute(
        "SELECT status FROM delivery_packages WHERE id='delivery-pkg'"
    ).fetchone()[0] == "approved"
    assert conn.execute(
        "SELECT status FROM artifacts WHERE id='delivery-art'"
    ).fetchone()[0] == "approved"
    assert conn.execute(
        "SELECT delivery_artifact_id FROM episodes WHERE id='e'"
    ).fetchone()[0] == "delivery-art"


def test_outdated_final_video_is_preserved_and_remains_visible(tmp_path, monkeypatch) -> None:
    conn = _database()
    project_root = tmp_path / "projects"
    final_path = project_root / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"existing-final")

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", project_root)

    assert artifacts.invalidate_episode_final("e") is True

    status = worker.episode_mix_status("e")
    assert final_path.read_bytes() == b"existing-final"
    assert final_path.with_suffix(".stale").is_file()
    assert status["final_video_url"].startswith(
        "/media/p/episodes/1/final/episode.mp4?v="
    )
    assert status["final_video_stale"] is True


def test_concat_timeout_preserves_previous_final_video(tmp_path, monkeypatch) -> None:
    conn = _database((1,))
    project_root = tmp_path / "projects"
    piece = project_root / "p" / "episodes" / "1" / "shots" / "shot-1.mp4"
    piece.parent.mkdir(parents=True)
    piece.write_bytes(b"new-piece")
    _version(conn, shot_no=1, path=piece, adopted=True)
    conn.commit()
    final_path = project_root / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"previous-final")

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 1))

    monkeypatch.setattr(worker.subprocess, "run", timeout)

    try:
        worker.concatenate_episode("e")
    except ValueError as exc:
        assert "上一版成片仍保留" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("合成超时必须返回可重试失败")
    assert final_path.read_bytes() == b"previous-final"


def test_concat_nonempty_undecodable_output_preserves_previous_final_video(
    tmp_path, monkeypatch,
) -> None:
    conn = _database((1,))
    project_root = tmp_path / "projects"
    piece = project_root / "p" / "episodes" / "1" / "shots" / "shot-1.mp4"
    piece.parent.mkdir(parents=True)
    piece.write_bytes(b"source-video")
    _version(conn, shot_no=1, path=piece, adopted=True)
    conn.commit()
    final_path = project_root / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"previous-final")
    stale_path = final_path.with_suffix(".stale")
    stale_path.write_text("outdated\n", encoding="utf-8")
    calls: list[tuple[list[str], dict]] = []

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def corrupt_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "ffprobe":
            return _probe_result(6.0)
        if command[-1] == "-":
            raise subprocess.CalledProcessError(
                1, command, stderr=b"Invalid data found when processing input",
            )
        Path(command[-1]).write_bytes(b"broken-but-nonempty")
        return SimpleNamespace(stdout=b"", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", corrupt_run)

    try:
        worker.concatenate_episode("e")
    except ValueError as exc:
        assert "完整解码失败" in str(exc)
        assert "上一版成片仍保留" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("坏的非空合片产物不得覆盖旧成片")

    assert final_path.read_bytes() == b"previous-final"
    assert stale_path.is_file()
    assert calls and all(call_kwargs.get("timeout") for _command, call_kwargs in calls)


def test_concat_abnormal_output_duration_preserves_previous_final_video(
    tmp_path, monkeypatch,
) -> None:
    conn = _database((1,))
    project_root = tmp_path / "projects"
    piece = project_root / "p" / "episodes" / "1" / "shots" / "shot-1.mp4"
    piece.parent.mkdir(parents=True)
    piece.write_bytes(b"source-video")
    _version(conn, shot_no=1, path=piece, adopted=True)
    conn.commit()
    final_path = project_root / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"previous-final")
    probed_source = False
    calls: list[tuple[list[str], dict]] = []

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def wrong_duration_run(command, **kwargs):
        nonlocal probed_source
        calls.append((command, kwargs))
        if command[0] == "ffprobe":
            if not probed_source:
                probed_source = True
                return _probe_result(6.0)
            return _probe_result(0.25)
        Path(command[-1]).write_bytes(b"nonempty-short-output")
        return SimpleNamespace(stdout=b"", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", wrong_duration_run)

    try:
        worker.concatenate_episode("e")
    except ValueError as exc:
        assert "时长异常" in str(exc)
        assert "上一版成片仍保留" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("时长异常的非空合片产物不得覆盖旧成片")

    assert final_path.read_bytes() == b"previous-final"
    assert calls and all(call_kwargs.get("timeout") for _command, call_kwargs in calls)


def test_cancel_video_adoption_keeps_candidate_and_marks_shot_pending(tmp_path, monkeypatch) -> None:
    conn = _database()
    video_path = tmp_path / "candidate.mp4"
    video_path.write_bytes(b"video")
    _version(conn, shot_no=1, path=video_path, adopted=True)
    conn.commit()
    invalidated: list[str] = []
    audits: list[tuple] = []
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(
        api.worker,
        "invalidate_episode_final",
        lambda episode_id: invalidated.append(episode_id),
    )
    patch_api_everywhere(monkeypatch, "_review_write_audit", lambda *args, **kwargs: audits.append((args, kwargs)))

    result = api._cancel_shot_adoption_core("s1")

    assert result["previous_adopted_version_id"] == "v1"
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'",
    ).fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM shot_versions WHERE id='v1'").fetchone()[0] == 1
    assert video_path.exists()
    assert invalidated == ["e"]
    assert audits


def test_adopt_version_persists_playback_rate_and_invalidates_previous_mix(
    tmp_path, monkeypatch,
) -> None:
    conn = _database()
    video_path = tmp_path / "candidate.mp4"
    video_path.write_bytes(b"video")
    _version(conn, shot_no=1, path=video_path, adopted=True)
    conn.execute(
        "UPDATE shot_versions SET technical_validation_json=? WHERE id='v1'",
        ('{"passed": true}',),
    )
    conn.commit()
    invalidated: list[str] = []

    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_review_assert_shot_positive", lambda *_args: None)
    monkeypatch.setattr(api.evidence_repository, "commit_artifact", lambda *_args, **_kwargs: {})
    patch_api_everywhere(monkeypatch, "_review_write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api.worker, "invalidate_episode_final", invalidated.append)
    from app.evidence import media as media_evidence
    monkeypatch.setattr(media_evidence, "record_video_candidate", lambda *_args, **_kwargs: {"id": "art-v1"})

    result = api._adopt_version_core("s1", {
        "version_id": "v1",
        "reason": "调整节奏后定稿",
        "playback_rate": 1.5,
    })

    assert result["playback_rate"] == 1.5
    assert conn.execute("SELECT playback_rate FROM shot_versions WHERE id='v1'").fetchone()[0] == 1.5
    assert invalidated == ["e"]


def test_mix_applies_each_adopted_versions_finalized_playback_rate(
    tmp_path, monkeypatch,
) -> None:
    conn = _database((1,))
    project_root = tmp_path / "projects"
    piece = project_root / "p" / "episodes" / "1" / "shots" / "shot-1.mp4"
    piece.parent.mkdir(parents=True)
    piece.write_bytes(b"source-video")
    _version(conn, shot_no=1, path=piece, adopted=True)
    conn.execute("UPDATE shot_versions SET playback_rate=2.0 WHERE id='v1'")
    conn.commit()
    final_path = project_root / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"old-final")
    final_path.with_suffix(".stale").write_text("outdated\n", encoding="utf-8")
    commands: list[list[str]] = []

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def successful_run(command, **_kwargs):
        commands.append(command)
        if command[0] == "ffprobe":
            duration_s = 8.0 if Path(command[-1]) == piece else 4.0
            return _probe_result(duration_s)
        if command[-1] == "-":
            return SimpleNamespace(stdout=b"", stderr=b"")
        Path(command[-1]).write_bytes(b"generated-video")
        return SimpleNamespace(stdout="", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", successful_run)

    status = worker.episode_mix_status("e")
    result = worker.concatenate_episode("e")
    status_after = worker.episode_mix_status("e")

    assert status["shots"][0]["playback_rate"] == 2.0
    # 一旦已采纳视频存在，episode_mix_status 的 effective_duration_s 改用实测
    # 视频流时长（8.0s，见 successful_run 对 piece 路径的探测）而非分镜合约的
    # 名义 duration_s（6），与合成后 result["total_duration_s"] 口径保持一致。
    assert status["shots"][0]["effective_duration_s"] == 4.0
    assert any("setpts=PTS/2.000000" in command for command in commands)
    # 音频归一现在把 atempo 和 aresample/asetpts/apad/atrim 拼进同一条 -af 滤镜链
    # （app.final_edit.audio_normalize_filter），倍速信息不再是独立的 argv 元素。
    assert any(
        any("atempo=2.000000" in arg for arg in command) for command in commands
    )
    assert result["playback_rates"] == {"1": 2.0}
    assert result["total_duration_s"] == 4.0
    assert result["final_edit"]["ok"] is False
    assert result["final_edit"]["runtime_blocking"] is False
    assert status_after["final_edit_report"]["fallback"] == "draft_concat"
    assert final_path.read_bytes() == b"generated-video"
    assert not final_path.with_suffix(".stale").exists()
    assert result["video_url"].startswith(
        "/media/p/episodes/1/final/episode.mp4?v="
    )
    assert result["video_url"] != status["final_video_url"]


def test_current_partial_adopted_video_delivery_manifest_skips_dangling_authority(
    tmp_path, monkeypatch,
) -> None:
    """红/绿：真实 downstream_authority（不 mock）复现并验证 CON-409 根因修复。

    重现真实第三集 ep_f0f0b4d4abef 库数据实测到的悬空态：镜头已"采纳"一个
    succeeded、文件真实落盘的版本，但采纳权威链不完整（这里用最直接的一种
    ——从未补上 artifact_id）。这会让
    downstream_authority.current_adopted_video_delivery_manifest 对那一镜抛出
    "镜 N 缺少已采纳的有效视频权威"——这正是用户报告里 CON-409 报错文案的字面
    来源。这个用例不走本文件其它用例依赖的"直通 mock"（那会把这条真实校验
    链路整个绕过），而是直接调用真实实现。
    """
    from app import downstream_authority
    from app.evidence import repository
    from app.harness.types import Evaluation, EvidenceArtifact

    # 本文件其它用例依赖的 autouse fixture（_published_authority_for_concat_
    # mechanics）把 current_adopted_video_delivery_manifest /
    # current_partial_adopted_video_delivery_manifest 都换成了直通 mock，专门
    # 用来隔离测拼接机制、不测这条真实校验链路。这个用例恰恰要测那条真实链
    # 路本身，所以先撤销 autouse 打的桩，拿回未被 mock 的真实实现。
    monkeypatch.undo()

    conn = _database((1, 2))
    project_root = tmp_path / "projects"
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)

    # 镜 1：走完整的采纳 -> Artifact -> 技术门禁评估链路，strict 与 partial
    # 两个 manifest 函数都必须认它有效。
    good_path = shot_dir / "shot-1.mp4"
    good_path.write_bytes(b"real-adopted-video")
    _version(conn, shot_no=1, path=good_path, adopted=True)
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    artifact = repository.create_artifact(
        EvidenceArtifact(
            type="shot_video", scope_type="shot", scope_id="s1",
            content={"kind": "shot_video", "version_id": "v1"},
            file_path=str(good_path), status="validated", trust_level="T2",
            contract_version="video-2.0.0",
        ),
        conn=conn,
    )
    artifact = repository.commit_artifact(None, artifact["id"], [Evaluation(
        evaluator_type="file", evaluator_name="video_technical_validator",
        evaluator_version="1", status="passed", hard_gate_passed=True, score=100,
    )])
    conn.execute(
        "UPDATE shot_versions SET artifact_id=?,technical_validation_json=? WHERE id='v1'",
        (artifact["id"], json.dumps({"passed": True, "issues": []})),
    )

    # 镜 2：已"采纳"一个 succeeded、文件真实落盘的版本，但从未补上
    # artifact_id（评估证据链没跟上）。这正是
    # downstream_authority._adopted_video_authority_for_row 第一条判据命中的
    # 场景，也是用户报告里"镜 3 缺少已采纳的有效视频权威"的字面复现。
    dangling_path = shot_dir / "shot-2.mp4"
    dangling_path.write_bytes(b"adopted-but-no-artifact")
    _version(conn, shot_no=2, path=dangling_path, adopted=True)
    conn.commit()

    # 红：严格版本（交付包路径用）必须仍然对镜 2 报错——回归防线，判据没被
    # 放宽。
    with pytest.raises(ValueError, match="镜 2 缺少已采纳的有效视频权威"):
        downstream_authority.current_adopted_video_delivery_manifest("e", conn=conn)

    # 绿：容错版本（成片台合片操作用）把镜 2 透明跳过，只回镜 1，并记录跳过
    # 原因；不因这一镜悬空就让整份清单计算失败。
    partial = downstream_authority.current_partial_adopted_video_delivery_manifest(
        "e", conn=conn,
    )
    assert [item["shot_no"] for item in partial["items"]] == [1]
    assert partial["skipped_shot_nos"] == [2]
    assert "镜 2 缺少已采纳的有效视频权威" in partial["skip_reasons"]["2"]


def test_con409_unadopted_shot_is_skipped_not_fatal_end_to_end(tmp_path, monkeypatch) -> None:
    """红/绿端到端：真实第三集库数据实测到的悬空态是 adopted_version_id 为空。

    只读核对 ep_f0f0b4d4abef 现场（data/manju.db，mode=ro）发现镜头悬空的
    具体形态是 shots.adopted_version_id 本身为空（重试后采纳指针被清空、
    新版本还没有被自动择优重新采纳），而不是指向某个已损坏的版本 id。这个
    用例不走本文件其它用例依赖的"直通 mock"，直接跑用户实际点击"合成成品"
    会命中的命令总线入口（app.capabilities.handlers.delivery.concatenate），
    证明修复前会在 claim_concat_operation 之前就因为一镜悬空而抛
    CON-409（镜 N 缺少已采纳的有效视频权威），修复后必须直接成功产出部分
    成片，并如实报告跳过了哪镜、为什么跳过。
    """
    import asyncio
    from app import downstream_authority
    from app.capabilities import inputs as I
    from app.capabilities.handlers import delivery as delivery_handler
    from app.evidence import repository
    from app.harness.types import Evaluation, EvidenceArtifact

    monkeypatch.undo()

    conn = _database((1, 2))
    project_root = tmp_path / "projects"
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)

    good_path = shot_dir / "shot-1.mp4"
    good_path.write_bytes(b"real-adopted-video")
    _version(conn, shot_no=1, path=good_path, adopted=True)
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    artifact = repository.create_artifact(
        EvidenceArtifact(
            type="shot_video", scope_type="shot", scope_id="s1",
            content={"kind": "shot_video", "version_id": "v1"},
            file_path=str(good_path), status="validated", trust_level="T2",
            contract_version="video-2.0.0",
        ),
        conn=conn,
    )
    artifact = repository.commit_artifact(None, artifact["id"], [Evaluation(
        evaluator_type="file", evaluator_name="video_technical_validator",
        evaluator_version="1", status="passed", hard_gate_passed=True, score=100,
    )])
    conn.execute(
        "UPDATE shot_versions SET artifact_id=?,technical_validation_json=? WHERE id='v1'",
        (artifact["id"], json.dumps({"passed": True, "issues": []})),
    )

    # 镜 2：有一个 succeeded、真实落盘的候选版本，但从未被采纳
    # （shots.adopted_version_id 为空）——真实第三集库数据实测到的确切悬空态。
    unadopted_path = shot_dir / "shot-2.mp4"
    unadopted_path.write_bytes(b"succeeded-but-never-adopted")
    _version(conn, shot_no=2, path=unadopted_path, adopted=False)
    conn.commit()

    with pytest.raises(ValueError, match="镜 2 缺少已采纳的有效视频权威"):
        downstream_authority.current_adopted_video_delivery_manifest("e", conn=conn)

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        downstream_authority,
        "verify_current_storyboard_release_authority",
        lambda episode_id, conn=None: {
            "published_storyboard_artifact_id": f"storyboard:{episode_id}",
            "release_qualification_hash": "release-current",
        },
    )

    def successful_run(command, **_kwargs):
        if command[0] == "ffprobe":
            return _probe_result(5.0)
        if command[-1] == "-":
            return SimpleNamespace(stdout=b"", stderr=b"")
        Path(command[-1]).write_bytes(b"partial-final")
        return SimpleNamespace(stdout="", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", successful_run)

    # 端到端：用户实际点击"合成成品"走的正是这条命令总线路径
    # （app.capabilities.handlers.delivery.concatenate，REST /concatenate 的
    # 首选路由）。修复前这里会在 claim_concat_operation 之前就抛 CON-409；
    # 修复后必须直接成功并如实报告跳过了镜 2、为什么跳过。
    args = I.EpisodeScopedInput(episode_id="e", idempotency_key="con409-repro")
    result = asyncio.run(delivery_handler.concatenate(args))

    assert result.status.value == "succeeded"
    assert result.data["partial"] is True
    assert result.data["included_shot_nos"] == [1]
    assert result.data["skipped_shot_nos"] == [2]
    assert "镜 2 缺少已采纳的有效视频权威" in result.data["skip_reasons"]["2"]


def test_concat_auto_adopts_all_playable_candidates_matching_real_episode_state(
    tmp_path, monkeypatch,
) -> None:
    """方案 B 端到端，复现第三集 ep_f0f0b4d4abef 的只读实测状态：12 镜全部
    生成成功，只有 4 镜（1/2/4/6）已采纳，其余 8 镜（3/5/7/8/9/10/11/12）
    有成功候选但从未被采纳。用户原话："只要它读到视频生成完了就可以合成
    啊"——合成前必须把 8 镜自动采纳为各自的最新成功候选，12 镜全部入选；
    采纳记录必须真实落库，用第二条独立连接读盘核对，不用写入那条连接自证。

    走的是用户实际点击"合成成品"命中的命令总线入口
    （app.capabilities.handlers.delivery.concatenate），而不是
    worker.concatenate_episode 的直接调用——自动采纳必须排在这里冻结
    release_authority/video_delivery_manifest 之前，否则会被
    _assert_concat_sources_current 误判成"发布前已采纳视频发生漂移"，
    这条用例同时覆盖这个排序正确性。
    """
    import asyncio
    from app.capabilities import inputs as I
    from app.capabilities.handlers import delivery as delivery_handler
    from app.evidence import media as media_evidence
    from app.evidence import repository
    from app.harness.types import Evaluation, EvidenceArtifact
    from app import downstream_authority

    monkeypatch.undo()

    db_path = tmp_path / "mix.db"
    conn = _database(tuple(range(1, 13)), db_path=db_path)
    project_root = tmp_path / "projects"
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)
    ftyp_bytes = b"\x00\x00\x00\x18ftypmp42" + b"x" * 64

    adopted_shot_nos = (1, 2, 4, 6)
    unadopted_shot_nos = (3, 5, 7, 8, 9, 10, 11, 12)
    assert sorted(adopted_shot_nos + unadopted_shot_nos) == list(range(1, 13))

    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    for shot_no in adopted_shot_nos:
        path = shot_dir / f"shot-{shot_no}.mp4"
        path.write_bytes(ftyp_bytes)
        _version(conn, shot_no=shot_no, path=path, adopted=True)
        artifact = repository.create_artifact(
            EvidenceArtifact(
                type="shot_video", scope_type="shot", scope_id=f"s{shot_no}",
                content={"kind": "shot_video", "version_id": f"v{shot_no}"},
                file_path=str(path), status="validated", trust_level="T2",
                contract_version="video-2.0.0",
            ),
            conn=conn,
        )
        artifact = repository.commit_artifact(None, artifact["id"], [Evaluation(
            evaluator_type="file", evaluator_name="video_technical_validator",
            evaluator_version="1", status="passed", hard_gate_passed=True, score=100,
        )])
        conn.execute(
            "UPDATE shot_versions SET artifact_id=?,technical_validation_json=? WHERE id=?",
            (artifact["id"], json.dumps({"passed": True, "issues": []}), f"v{shot_no}"),
        )
    for shot_no in unadopted_shot_nos:
        path = shot_dir / f"shot-{shot_no}.mp4"
        path.write_bytes(ftyp_bytes)
        _version(conn, shot_no=shot_no, path=path, adopted=False)
    conn.commit()

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_review_assert_shot_positive", lambda *a, **k: {})
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        downstream_authority,
        "verify_current_storyboard_release_authority",
        lambda episode_id, conn=None: {
            "published_storyboard_artifact_id": f"storyboard:{episode_id}",
            "release_qualification_hash": "release-current",
        },
    )

    def successful_run(command, **_kwargs):
        if command[0] == "ffprobe":
            # 12 镜全入选，拼接后总时长必须匹配每镜 5.0s 的探测口径之和，
            # 否则 _probe_concat_media 的时长校验会拒绝这版"成片"。
            duration_s = 12 * 5.0 if Path(command[-1]).name == "concat.mp4" else 5.0
            return _probe_result(duration_s)
        if command[-1] == "-":
            return SimpleNamespace(stdout=b"", stderr=b"")
        Path(command[-1]).write_bytes(b"generated-video")
        return SimpleNamespace(stdout="", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", successful_run)

    args = I.EpisodeScopedInput(episode_id="e", idempotency_key="auto-adopt-mix-e2e")
    result = asyncio.run(delivery_handler.concatenate(args))

    assert result.status.value == "succeeded", (result.summary, result.error_code, result.data)
    assert result.data["shots"] == 12
    assert result.data["included_shot_nos"] == list(range(1, 13))
    assert result.data["skipped_shot_nos"] == []

    # 独立观察点：不用写入那条 conn 自证，另开一条连接读盘上数据核对
    # 真提交（CLAUDE.md「验证要有独立观察点」）。
    verify_conn = sqlite3.connect(str(db_path))
    verify_conn.row_factory = sqlite3.Row
    try:
        adopted_rows = verify_conn.execute(
            "SELECT shot_no, adopted_version_id FROM shots WHERE episode_id='e' ORDER BY shot_no"
        ).fetchall()
        assert [row["adopted_version_id"] for row in adopted_rows] == [
            f"v{n}" for n in range(1, 13)
        ]
        auto_adopted_versions = {f"v{n}" for n in unadopted_shot_nos}
        audit_rows = verify_conn.execute(
            "SELECT target_version, reason FROM review_action_audit "
            "WHERE action='video_version.adopt' AND scope_type='shot'"
        ).fetchall()
        audited_by_version = {row["target_version"]: row["reason"] for row in audit_rows}
        assert auto_adopted_versions <= set(audited_by_version)
        for version_id in auto_adopted_versions:
            assert "成片合成时自动采纳" in audited_by_version[version_id]
        gate_count = verify_conn.execute(
            "SELECT COUNT(*) FROM gate_decisions WHERE gate_key='video_adoption'"
        ).fetchone()[0]
        assert gate_count >= len(unadopted_shot_nos)
    finally:
        verify_conn.close()


def test_auto_adopt_before_mix_skips_delivery_fallback_and_technically_invalid_candidates(
    tmp_path, monkeypatch,
) -> None:
    """回归：delivery_fallback 候选与技术校验不过的候选，即使文件落盘，也
    不得被合成时自动采纳自动带走——它们必须继续走既有的"跳过"路径。"""
    from app.evidence import media as media_evidence
    from app.evidence import repository

    conn = _database((1, 2, 3))
    project_root = tmp_path / "projects"
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)
    ftyp_bytes = b"\x00\x00\x00\x18ftypmp42" + b"x" * 64

    # 镜 1：正常可自动采纳的候选（对照组）。
    good_path = shot_dir / "shot-1.mp4"
    good_path.write_bytes(ftyp_bytes)
    _version(conn, shot_no=1, path=good_path, adopted=False)

    # 镜 2：delivery_fallback 静态图片/静音占位版本，不具备模型候选资格。
    fallback_path = shot_dir / "shot-2.mp4"
    fallback_path.write_bytes(ftyp_bytes)
    _version(conn, shot_no=2, path=fallback_path, adopted=False)
    conn.execute(
        "UPDATE shot_versions SET image_inputs=? WHERE id='v2'",
        (json.dumps({"delivery_fallback": True}),),
    )

    # 镜 3：文件落盘但不是合法 MP4（容器签名缺失），技术校验必定不通过。
    bad_path = shot_dir / "shot-3.mp4"
    bad_path.write_bytes(b"not-a-real-mp4-container")
    _version(conn, shot_no=3, path=bad_path, adopted=False)
    conn.commit()

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_review_assert_shot_positive", lambda *a, **k: {})
    # ffprobe 不可用：validate_video_file 只按容器签名判定，镜 3 稳定不通过。
    monkeypatch.setattr(worker.shutil, "which", lambda _name: None)

    result = worker._auto_adopt_playable_candidates_before_mix("e")

    assert result["auto_adopted_shot_nos"] == [1]
    assert "3" in result["auto_adopt_failures"]
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'"
    ).fetchone()[0] == "v1"
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s2'"
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s3'"
    ).fetchone()[0] is None


def test_auto_adopt_before_mix_leaves_shots_without_any_candidate_alone(
    tmp_path, monkeypatch,
) -> None:
    """回归：完全没有候选（从未生成、或候选未落盘）的镜头，自动采纳不得
    凭空造出一个采纳；这类镜头仍然只能走既有的"跳过并如实上报"路径。"""
    conn = _database((1,))
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)

    result = worker._auto_adopt_playable_candidates_before_mix("e")

    assert result["auto_adopted_shot_nos"] == []
    assert result["auto_adopt_failures"] == {}
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'"
    ).fetchone()[0] is None


def test_playback_rate_contract_rejects_unsafe_values() -> None:
    assert normalize_playback_rate(None) == 1.0
    assert normalize_playback_rate("1.25") == 1.25
    for value in (0.49, 2.01, float("nan"), float("inf"), "fast"):
        try:
            normalize_playback_rate(value)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"非法倍速未被拒绝：{value!r}")
