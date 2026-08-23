"""缓存不得替代校验，也不得让两个调用方共享可变对象。

本轮性能整改给 `screenplay_from_artifact_record` 加了「按内容封印记住解析结果」
的缓存。最终代码审查查出三个缺陷，全部出在这段缓存里：

1. **严重**：门禁被放到了缓存命中之后。我在 docstring 里断言
   `_assert_screenplay_artifact_contract` 是 `(artifact_id, content_hash,
   contract_version)` 的纯函数——这是错的。它读两处**可变**状态：
   * `_historical_screenplay_artifact_is_bound`：查 production_revisions /
     completion_certificates / episodes / evaluations / lineage，
     同一份不可变内容的「是否已被绑定」会随时间改变；
   * `_current_ir_semantic_gaps`：沿 parent 链读 `status != 'validated'`，
     而 artifact.status 就是被 `UPDATE artifacts SET status='stale'` 改的列。
   于是缓存命中就等于**静默关掉一个数据完整性门禁**。

2. **中等**：未命中路径直接返回 `model_validate` 出来的模型。pydantic 对
   bare `dict`/`list` 字段不做深拷贝，返回的模型与 artifact content 共享嵌套
   对象；叠加读作用域里 `content` 本身就是跨调用方共享的，一次原地规范化就能
   污染同一请求内所有其它读者。改动前的代码是每次 `model_copy(deep=True)`，
   这是我引入的回归。

3. **中等**：只读作用域会掩盖发生在它内部的写。剧本台首屏会走到
   `assert_screenplay_matches_validated_v7_source`，那里 fail-closed 地把
   artifact 标成 stale 并 commit；作用域缓存却继续供应写前的 status。
   「Writers must never open this scope」是一句注释，注释保证不了这件事。
"""
from __future__ import annotations

import pytest

from app import db
from app.errors import ArtifactNeedsRebuildError
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.production import patch as patch_module
from app.production.patch import screenplay_from_artifact_record


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "artifact-cache.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    patch_module._SCREENPLAY_ARTIFACT_MODEL_CACHE.clear()


def _legacy_screenplay_artifact() -> dict:
    """一份 narrative_plan 为空、合同不要求叙事计划的历史剧本产物。"""
    created = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="episode-cache",
        status="validated",
        trust_level="T1",
        content={
            "episode_no": 1,
            "title": "缓存完整性夹具",
            "full_script_text": "甲推门进入。",
        },
        contract_version="screenplay.v1",
    ))
    return evidence_repository.get_artifact(created["id"])


# ------------------------------------------------------------------ 缺陷 1

def test_contract_gate_runs_on_every_call_not_just_on_cache_miss() -> None:
    """绑定状态在两次调用之间改变时，第二次必须被门禁拦下。

    这是缺陷 1 的端到端复现：同一份**不可变**内容、同一个 content_hash、
    同一个合同版本——缓存键三要素全部不变，但门禁的答案变了。
    """
    art = _legacy_screenplay_artifact()

    # 未被任何东西引用：历史产物合同允许它通过。
    first = screenplay_from_artifact_record(art)
    assert first.episode_no == 1

    # 现在把它绑成某个 revision 的工作产物——按门禁自己的规则，
    # 这份缺 narrative_plan 的产物从此必须重建。
    conn = db.get_conn()
    conn.execute(
        """INSERT INTO production_revisions
               (id, episode_id, kind, status, working_artifact_id,
                created_at, updated_at)
           VALUES ('rev-cache', 'episode-cache', 'screenplay', 'working', ?, 0, 0)""",
        (art["id"],),
    )
    conn.commit()

    with pytest.raises(ArtifactNeedsRebuildError):
        screenplay_from_artifact_record(evidence_repository.get_artifact(art["id"]))


def test_cache_hit_still_pays_the_gate(monkeypatch) -> None:
    """更直接的守卫：命中也必须调门禁，不许把校验记进缓存。"""
    art = _legacy_screenplay_artifact()
    calls: list[str] = []
    real = patch_module._assert_screenplay_artifact_contract

    def counting(record, content):
        calls.append(str(record.get("id") or ""))
        return real(record, content)

    monkeypatch.setattr(
        patch_module, "_assert_screenplay_artifact_contract", counting
    )
    screenplay_from_artifact_record(art)
    screenplay_from_artifact_record(art)

    assert len(calls) == 2, "缓存命中跳过了门禁"


# ------------------------------------------------------------------ 缺陷 2

def test_every_caller_gets_an_isolated_model_on_a_cache_miss() -> None:
    """未命中也必须隔离：改返回值不得写穿 artifact content。"""
    art = _legacy_screenplay_artifact()
    original_title = art["content"]["title"]

    first = screenplay_from_artifact_record(art)
    first.title = "被调用方改过"
    first.key_plot_points.append("调用方追加的条目")

    assert art["content"]["title"] == original_title
    assert not art["content"].get("key_plot_points")

    second = screenplay_from_artifact_record(art)
    assert second.title == original_title
    assert second.key_plot_points == []
    assert second is not first


def test_nested_mutable_fields_are_not_aliased_to_artifact_content() -> None:
    """pydantic 对 bare dict/list 字段不深拷贝——这条盯的正是那个特性。"""
    art = _legacy_screenplay_artifact()
    art["content"]["key_plot_points"] = ["原始条目"]
    # content 变了，封印也得跟着变，否则会被指纹漂移挡在门口。
    conn = db.get_conn()
    conn.execute(
        "UPDATE artifacts SET content_json=?, content_hash=? WHERE id=?",
        (
            __import__("json").dumps(art["content"], ensure_ascii=False),
            evidence_repository.content_hash(art["content"], None),
            art["id"],
        ),
    )
    conn.commit()
    art = evidence_repository.get_artifact(art["id"])

    model = screenplay_from_artifact_record(art)
    model.key_plot_points.append("污染")

    assert art["content"]["key_plot_points"] == ["原始条目"]


# ------------------------------------------------------------------ 缺陷 3

def test_read_scope_never_serves_a_row_older_than_a_write_inside_it() -> None:
    """作用域内发生写之后，同一作用域的后续读必须看到新状态。"""
    art = _legacy_screenplay_artifact()

    with evidence_repository.artifact_read_scope():
        before = evidence_repository.get_artifact(art["id"])
        assert before["status"] == "validated"

        conn = db.get_conn()
        conn.execute(
            "UPDATE artifacts SET status='stale',stale_reason='测试写入' WHERE id=?",
            (art["id"],),
        )
        conn.commit()
        evidence_repository.invalidate_artifact_read_scope(art["id"])

        after = evidence_repository.get_artifact(art["id"])

    assert after["status"] == "stale", "读作用域把作用域内的写掩盖掉了"


def test_read_scope_still_memoises_when_nothing_is_written() -> None:
    """失效机制不得把缓存本身废掉——没有写就应该只查一次库。"""
    art = _legacy_screenplay_artifact()
    reads: list[str] = []
    real_conn = db.get_conn()

    class CountingConn:
        def execute(self, sql, *args, **kwargs):
            if "FROM artifacts WHERE id=?" in sql:
                reads.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    with evidence_repository.artifact_read_scope():
        for _ in range(5):
            evidence_repository.get_artifact(art["id"], conn=CountingConn())

    assert len(reads) == 1, f"作用域没有生效，查了 {len(reads)} 次"


def test_invalidate_outside_a_scope_is_a_noop() -> None:
    evidence_repository.invalidate_artifact_read_scope("art_whatever")
    evidence_repository.invalidate_artifact_read_scope()
