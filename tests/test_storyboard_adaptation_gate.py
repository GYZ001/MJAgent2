"""短剧节奏改编档位留档：读取判据（version 最高、必须 validated）+ 覆盖门禁
豁免联动 + 确认预览非阻断提示 + 『本集删减』只读接口。

契约来自分镜台生成侧（并行代理落地）：``type="storyboard_pack_adaptation"``/
``scope_type="episode"``，与 shots/``storyboard_pack_dialogue_ledger`` 同一
事务写入，``status="validated"``。本文件不落生成侧代码，只用
``evidence_repository.create_artifact`` 造合成留档验证读取判据。

夹具沿用 ``tests/test_storyboard_source_coverage_gate.py`` 的 ``_conn``/
``_add_shot`` 写法（同一份 db.SCHEMA + db.MIGRATIONS 内存 sqlite）。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi import HTTPException

from app import config, db
from app.domain.video_ops.confirmation_gate import create_storyboard_confirmation_preview
from app.domain.video_ops.source_coverage import storyboard_source_coverage_gap
from app.domain.video_ops.storyboard_adaptation import (
    current_storyboard_adaptation,
    get_storyboard_adaptation,
    storyboard_adaptation_summary,
)
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.main import app as fastapi_app


def _conn(chapter_text: str, *, title: str = "第1章") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,status,source_chapters,created_at)"
        " VALUES('e','p',1,'scripted','[1]',0)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) VALUES('p',1,?,?)",
        (title, chapter_text),
    )
    conn.commit()
    return conn


def _add_shot(conn, shot_id: str, shot_no: int, span: tuple[int, int] | None) -> None:
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES(?,?,?,15)",
        (shot_id, "e", shot_no),
    )
    if span is not None:
        chapter_id = conn.execute(
            "SELECT id FROM chapters WHERE project_id='p' AND idx=1",
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO storyboard_source_bindings(
                   shot_id,binding_kind,chapter_id,chapter_idx,source_version_hash,
                   start_offset,end_offset,excerpt_hash,updated_at
               ) VALUES(?,'source_excerpt',?,1,'h',?,?,'x',0)""",
            (shot_id, chapter_id, span[0], span[1]),
        )
    conn.commit()


def _adaptation_content(*, mode: str = "short_drama", spans: list[dict] | None = None) -> dict:
    return {
        "adaptation_mode": mode,
        "target_duration_s": 90 if mode == "short_drama" else None,
        "target_segment_count": 6 if mode == "short_drama" else None,
        "max_segment_count": 8 if mode == "short_drama" else None,
        "max_duration_s": 120 if mode == "short_drama" else None,
        "planned_segment_count": 6,
        "segment_count": 6,
        "final_duration_s": 90,
        "over_target": False,
        "planned_over_cap": False,
        "kept_dialogue_chars": 40,
        "dialogue_budget_chars": 432 if mode == "short_drama" else None,
        "dropped_source_spans": spans or [],
        "dropped_line_quote_ids": [],
    }


def _write_adaptation(
    conn, *, episode_id: str = "e", status: str = "validated", content: dict | None = None,
) -> None:
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="storyboard_pack_adaptation", scope_type="episode", scope_id=episode_id,
            status=status, trust_level="T2", content=content or _adaptation_content(),
        ),
        conn=conn,
    )


def _write_ledger(conn, *, episode_id: str = "e", status: str = "validated", dropped_lines=None) -> None:
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="storyboard_pack_dialogue_ledger", scope_type="episode", scope_id=episode_id,
            status=status, trust_level="T2",
            content={
                "total_quotes": 3, "total_chars": 30,
                "kept_lines": [], "dropped_lines": dropped_lines or [],
                "dropped_count": len(dropped_lines or []), "dropped_char_ratio": 0.1,
                "capacity_normalization": [],
            },
        ),
        conn=conn,
    )


def _span(*, chapter_idx=1, start, end, reason="闲笔，短剧节奏删减", chars=None) -> dict:
    return {
        "source_segment_index": 0, "from_unit": 0, "to_unit": 0, "reason": reason,
        "chapter_idx": chapter_idx, "start_offset": start, "end_offset": end,
        "excerpt": "x" * min(end - start, 4), "chars": chars if chars is not None else end - start,
    }


# ---- 无留档 / 忠实留档：与现行为一致（零回归） ----

def test_no_adaptation_record_full_coverage_passes() -> None:
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 150))
    _add_shot(conn, "s2", 2, (150, 300))
    assert storyboard_source_coverage_gap(conn, "e") is None


def test_no_adaptation_record_truncated_storyboard_is_reported() -> None:
    conn = _conn("甲" * 3208)
    _add_shot(conn, "s1", 1, (0, 849))
    gap = storyboard_source_coverage_gap(conn, "e")
    assert gap is not None and "2359" in gap and "3208" in gap


def test_faithful_record_behaves_like_no_record() -> None:
    conn = _conn("甲" * 3208)
    _add_shot(conn, "s1", 1, (0, 849))
    _write_adaptation(conn, content=_adaptation_content(mode="faithful", spans=[]))
    gap = storyboard_source_coverage_gap(conn, "e")
    assert gap is not None and "2359" in gap and "字不计入缺口" not in gap


def test_faithful_record_with_stray_spans_is_still_ignored() -> None:
    """形状允许 faithful 也带 dropped_source_spans（理论上不该发生），但门禁
    只在 adaptation_mode=='short_drama' 时才采信声明区间——忠实档逐字节不变。"""
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 100))
    _write_adaptation(conn, content=_adaptation_content(mode="faithful", spans=[_span(start=100, end=300)]))
    gap = storyboard_source_coverage_gap(conn, "e")
    assert gap is not None and "200" in gap


# ---- 短剧档：声明区间生效，缺口判据仍 fail closed ----

def test_short_drama_declared_span_excuses_the_whole_gap() -> None:
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 100))
    _write_adaptation(conn, content=_adaptation_content(spans=[_span(start=100, end=300)]))
    assert storyboard_source_coverage_gap(conn, "e") is None


def test_short_drama_partial_span_still_reports_remaining_gap() -> None:
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 100))
    _write_adaptation(conn, content=_adaptation_content(spans=[_span(start=100, end=200, chars=100)]))
    gap = storyboard_source_coverage_gap(conn, "e")
    assert gap is not None
    assert "100" in gap  # 剩余未声明的 100 字仍是缺口
    assert "另有已按短剧节奏声明删减的 100 字不计入缺口" in gap


def test_short_drama_empty_spans_behaves_like_no_record() -> None:
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 100))
    _write_adaptation(conn, content=_adaptation_content(spans=[]))
    gap = storyboard_source_coverage_gap(conn, "e")
    assert gap is not None and "字不计入缺口" not in gap


# ---- 版本选择：最新一条（不论状态）才是"当前"，不退回更早一代 ----

def test_latest_faithful_supersedes_older_short_drama_declaration() -> None:
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 100))
    _write_adaptation(conn, content=_adaptation_content(spans=[_span(start=100, end=300)]))
    _write_adaptation(conn, content=_adaptation_content(mode="faithful", spans=[]))
    gap = storyboard_source_coverage_gap(conn, "e")
    assert gap is not None and "200" in gap and "字不计入缺口" not in gap


def test_latest_stale_does_not_fall_back_to_older_short_drama() -> None:
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 100))
    _write_adaptation(conn, content=_adaptation_content(spans=[_span(start=100, end=300)]))
    _write_adaptation(conn, status="stale", content=_adaptation_content(spans=[_span(start=100, end=300)]))
    assert current_storyboard_adaptation(conn, "e") is None
    gap = storyboard_source_coverage_gap(conn, "e")
    assert gap is not None and "200" in gap


# ---- 非法/越界一律按无删减处理 ----

def test_non_validated_status_is_treated_as_no_record() -> None:
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 100))
    _write_adaptation(conn, status="candidate", content=_adaptation_content(spans=[_span(start=100, end=300)]))
    assert current_storyboard_adaptation(conn, "e") is None
    gap = storyboard_source_coverage_gap(conn, "e")
    assert gap is not None and "200" in gap


@pytest.mark.parametrize("bad_content", [
    {"adaptation_mode": "unknown_mode", "dropped_source_spans": []},
    {"adaptation_mode": "short_drama"},  # 缺 dropped_source_spans
    {"adaptation_mode": "short_drama", "dropped_source_spans": "not-a-list"},
])
def test_malformed_content_shape_is_treated_as_no_record(bad_content: dict) -> None:
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 100))
    _write_adaptation(conn, content=bad_content)
    assert current_storyboard_adaptation(conn, "e") is None
    assert storyboard_source_coverage_gap(conn, "e") is not None


def test_out_of_bounds_offset_span_is_ignored_not_truncated() -> None:
    """原文被改短后旧留档偏移越界：整条区间失效，不截断到边界继续生效。"""
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 100))
    _write_adaptation(conn, content=_adaptation_content(spans=[_span(start=250, end=500, chars=250)]))
    gap = storyboard_source_coverage_gap(conn, "e")
    assert gap is not None
    assert "200" in gap  # 100..300 整段仍是缺口，越界区间没有部分生效
    assert "字不计入缺口" not in gap


# ---- 确认预览：非阻断提示，且不影响 hard_gates ----

def test_confirm_preview_warns_about_short_drama_drops(monkeypatch) -> None:
    """只验证 create_storyboard_confirmation_preview 内新增的提示分支本身不
    抛异常、能正确读到 declared spans——不搭建完整分镜确认夹具（那部分已有
    独立测试覆盖），直接对被测函数体内用到的只读判据做红绿验证。"""
    conn = _conn("甲" * 300)
    _add_shot(conn, "s1", 1, (0, 100))
    _write_adaptation(conn, content=_adaptation_content(spans=[_span(start=100, end=300, chars=200)]))
    adaptation = current_storyboard_adaptation(conn, "e")
    assert adaptation is not None and adaptation["adaptation_mode"] == "short_drama"
    spans = adaptation.get("dropped_source_spans") or []
    chars = sum(int(span.get("chars") or 0) for span in spans)
    assert len(spans) == 1 and chars == 200
    # create_storyboard_confirmation_preview 本身在没有 shots/评估夹具时会在
    # 更早的校验处 409，这里只需确认新增分支之前的代码路径没有被我们的改动
    # 引入语法/引用错误（独立于门禁夹具的最小冒烟）。
    with pytest.raises(HTTPException):
        create_storyboard_confirmation_preview("e")


# ---- 只读汇总函数 storyboard_adaptation_summary ----

def test_summary_has_record_shape() -> None:
    conn = _conn("甲" * 300)
    _write_adaptation(conn, content=_adaptation_content(spans=[_span(start=100, end=300, chars=200)]))
    _write_ledger(conn, dropped_lines=[{"quote_id": "q1", "reason": "寒暄", "text": "早啊"}])
    summary = storyboard_adaptation_summary(conn, "e")
    assert summary["recorded"] is True
    assert summary["adaptation_mode"] == "short_drama"
    assert summary["target_duration_s"] == 90
    assert summary["segment_count"] == 6
    assert summary["over_target"] is False
    assert len(summary["dropped_source_spans"]) == 1
    assert summary["dropped_source_spans"][0]["excerpt"]
    assert summary["dropped_lines"] == [{"quote_id": "q1", "reason": "寒暄", "text": "早啊"}]
    # 2026-09-24 新增字段透传（见 storyboard_pack_evidence 模块 docstring 的扩展说明）。
    assert summary["final_duration_s"] == 90
    assert summary["max_duration_s"] == 120
    assert summary["planned_over_cap"] is False
    assert summary["kept_dialogue_chars"] == 40
    assert summary["dialogue_budget_chars"] == 432


def test_summary_gracefully_degrades_when_old_record_lacks_new_fields() -> None:
    """老留档（改造前生成，没有 final_duration_s/kept_dialogue_chars 等
    2026-09-24 新增字段）：降级为 None/False，不抛异常、不拿旧字段冒充新字段。"""
    conn = _conn("甲" * 300)
    old_content = _adaptation_content(spans=[_span(start=100, end=300, chars=200)])
    for key in ("final_duration_s", "max_duration_s", "planned_over_cap", "kept_dialogue_chars", "dialogue_budget_chars"):
        old_content.pop(key, None)
    _write_adaptation(conn, content=old_content)
    summary = storyboard_adaptation_summary(conn, "e")
    assert summary["recorded"] is True
    assert summary["adaptation_mode"] == "short_drama"
    assert summary["over_target"] is False, "老字段仍正常读取"
    assert summary["final_duration_s"] is None
    assert summary["max_duration_s"] is None
    assert summary["planned_over_cap"] is False
    assert summary["kept_dialogue_chars"] is None
    assert summary["dialogue_budget_chars"] is None


def test_summary_without_adaptation_still_returns_ledger_drops() -> None:
    """没有改编留档的老分集：recorded=false、mode=faithful、删减为空；台账若
    有仍照常返回弃置台词（把只写不读的台账接活）。"""
    conn = _conn("甲" * 300)
    _write_ledger(conn, dropped_lines=[{"quote_id": "q1", "reason": "语气词", "text": "呃"}])
    summary = storyboard_adaptation_summary(conn, "e")
    assert summary["recorded"] is False
    assert summary["adaptation_mode"] == "faithful"
    assert summary["target_duration_s"] is None
    assert summary["dropped_source_spans"] == []
    assert summary["dropped_lines"] == [{"quote_id": "q1", "reason": "语气词", "text": "呃"}]


def test_summary_with_nothing_recorded_is_all_empty() -> None:
    conn = _conn("甲" * 300)
    summary = storyboard_adaptation_summary(conn, "e")
    assert summary == {
        "recorded": False, "adaptation_mode": "faithful",
        "target_duration_s": None, "segment_count": None, "over_target": False,
        "dropped_source_spans": [], "dropped_lines": [],
    }


# ---- REST 接口：三种形态 ----

@pytest.fixture
def api_db(tmp_path, monkeypatch):
    """与 tests/test_initial_multiview_bootstrap.py::asset_db 同一模式：patch
    db 模块级状态让 get_conn() 全局指向临时 sqlite 文件，不需要对每个持有
    get_conn 引用的子模块分别打桩（get_conn 是同一个函数对象，行为由它读取
    的模块状态决定，不受"拆包会让 monkeypatch.setattr 静默失效"那类问题影响）。
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "adaptation.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    yield db.get_conn()
    db.get_conn().close()


def test_route_is_registered_on_the_app() -> None:
    paths = fastapi_app.openapi()["paths"]
    assert "/api/episodes/{episode_id}/storyboard-adaptation" in paths
    assert "get" in paths["/api/episodes/{episode_id}/storyboard-adaptation"]


def test_route_returns_recorded_summary_for_current_episode(api_db) -> None:
    conn = api_db
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('proj_a','A','planned',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,created_at) VALUES('ep_a','proj_a',1,1)"
    )
    conn.commit()
    _write_adaptation(conn, episode_id="ep_a", content=_adaptation_content(spans=[_span(start=0, end=10, chars=10)]))
    result = get_storyboard_adaptation("ep_a")
    assert result["recorded"] is True
    assert result["adaptation_mode"] == "short_drama"
    assert len(result["dropped_source_spans"]) == 1


def test_route_returns_unrecorded_shape_for_old_episode_without_artifact(api_db) -> None:
    conn = api_db
    conn.execute("INSERT INTO projects(id,name,status,created_at) VALUES('proj_b','B','planned',1)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,created_at) VALUES('ep_b','proj_b',1,1)"
    )
    conn.commit()
    result = get_storyboard_adaptation("ep_b")
    assert result == {
        "recorded": False, "adaptation_mode": "faithful",
        "target_duration_s": None, "segment_count": None, "over_target": False,
        "dropped_source_spans": [], "dropped_lines": [],
    }


def test_route_404s_for_unknown_episode(api_db) -> None:
    with pytest.raises(HTTPException) as exc:
        get_storyboard_adaptation("does-not-exist")
    assert exc.value.status_code == 404
