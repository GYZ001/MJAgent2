"""Golden：《陨落的天才》第 1 集 Supervisor 真人 LLM E2E。

默认跳过（CI 无密钥）。本地开启：

  set MANJU_GOLDEN_LIVE=1
  set MANJU_GOLDEN_EPISODE_ID=ep_23517af4b5a8   # 可选，默认按标题解析
  .\\.venv\\Scripts\\python.exe scripts/run_golden_storyboard.py
  .\\.venv\\Scripts\\python.exe -m pytest tests/test_golden_storyboard_e2e.py -m golden -s

验收（PRD §18.3）：
- Supervisor 跑通后执行显式人工确认
- 不产生付费视频
- 写入 golden/runs/ 报表（含 renderability 对照）
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _live_enabled() -> bool:
    return os.environ.get("MANJU_GOLDEN_LIVE", "").strip() in {"1", "true", "yes"}


@pytest.mark.golden
@pytest.mark.skipif(not _live_enabled(), reason="需 MANJU_GOLDEN_LIVE=1 + 真实 LLM 密钥")
def test_golden_yunluo_ep1_manual_confirm_live():
    from scripts.run_golden_storyboard import run_golden

    report = run_golden(
        episode_id=os.environ.get("MANJU_GOLDEN_EPISODE_ID") or None,
        title_hint="陨落的天才",
        episode_no=1,
        poll_timeout_s=float(os.environ.get("MANJU_GOLDEN_TIMEOUT_S", "7200")),
        write_report=True,
    )
    assert report["ok"] is True, report
    assert report["confirmed"] is True
    assert report.get("video_jobs_started", 0) == 0
    assert Path(report["report_path"]).is_file()


def test_golden_report_scaffold_offline(tmp_path, monkeypatch):
    """离线：报表脚手架与解析逻辑不依赖 LLM。"""
    from scripts import run_golden_storyboard as golden

    monkeypatch.setattr(golden, "ROOT", tmp_path)
    (tmp_path / "golden" / "runs").mkdir(parents=True)
    (tmp_path / "golden" / "renderability").mkdir(parents=True)
    baseline = {
        "label": "test",
        "shot_count": 24,
        "total_duration_s": 144,
    }
    (tmp_path / "golden" / "renderability" / "doupocangqiong_ep1_baseline.json").write_text(
        __import__("json").dumps(baseline), encoding="utf-8"
    )

    score = {
        "shot_count": 12,
        "total_duration_s": 80,
        "shot_count_le_70pct_baseline": True,
        "checks": {"ok": True},
    }
    path = golden.write_run_report(
        episode_id="ep_test",
        title="陨落的天才",
        score=score,
        supervisor={"phase": "SUCCEEDED", "outcome": "SUCCEEDED_READY_FOR_CONFIRM"},
        extra={"confirmed": True, "video_jobs_started": 0},
        out_dir=tmp_path / "golden" / "runs",
    )
    assert path.is_file()
    data = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert data["episode_id"] == "ep_test"
    assert data["confirmed"] is True
    assert data["score"]["shot_count"] == 12
    assert "ts" in data


def test_resolve_episode_prefers_explicit_id(tmp_path, monkeypatch):
    from app import db
    from scripts import run_golden_storyboard as golden

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "g.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES('p1','斗破','planned',1)"
    )
    conn.execute(
        """INSERT INTO episodes(
               id, project_id, episode_no, title, source_chapters, target_duration_s,
               screenplay_status, status, created_at
           ) VALUES('ep_23517af4b5a8','p1',1,'第1章 陨落的天才','[1]',120,'ready','scripted',1)"""
    )
    conn.commit()
    row = golden.resolve_episode(episode_id="ep_23517af4b5a8", title_hint="陨落的天才", episode_no=1)
    assert row["id"] == "ep_23517af4b5a8"
    row2 = golden.resolve_episode(episode_id=None, title_hint="陨落的天才", episode_no=1)
    assert row2["id"] == "ep_23517af4b5a8"
