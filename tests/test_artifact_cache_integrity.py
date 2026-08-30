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

3. **中等**：只读作用域会掩盖发生在它内部的写。映射台首屏会走到
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


def _prep_pack_artifact() -> dict:
    """一份 episode_prep_pack（screenplay 契约 6.0.0，无 narrative_plan 概念）。"""
    created = evidence_repository.create_artifact(EvidenceArtifact(
        type="episode_prep_pack",
        scope_type="episode",
        scope_id="episode-prep-pack-cache",
        status="validated",
        trust_level="T2",
        content={
            "prep_pack_version": "1.1.0",
            "episode_no": 1,
            "episode_scope": {"chapter_indexes": [1], "source_segment_count": 1},
            "event_chain": [],
            "asset_manifest": {"characters": [], "scenes": []},
            "coverage_ledger": {
                "total_segments": 0, "delivered": [], "merged": [],
                "retained_as_context": [], "proven_duplicates": [], "uncovered": [],
            },
            "hook": "", "cliffhanger": "",
        },
        contract_version="6.0.0",
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

def _planned_screenplay_artifact() -> dict:
    """带 narrative_plan 的产物：`relationship_state` 是 bare `dict`。

    pydantic 只复制最外层容器，**`Any` 位置上的嵌套值仍然是同一个对象**
    （本仓 pydantic 2.13.4 实测）。剧本模型上所有这样的位置都在
    `narrative_plan` 里，所以隔离性必须在这里验，legacy 夹具验不出来。
    """
    created = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_document",
        scope_type="episode",
        scope_id="episode-cache",
        status="validated",
        trust_level="T1",
        content={
            "episode_no": 1,
            "title": "别名夹具",
            "full_script_text": "甲推门进入。",
            "narrative_plan": {
                "contract_version": "narrative-continuity.v2",
                "scope_id": "episode-cache",
                "character_states": [{
                    "character_state_id": "CS1",
                    "character_id": "C1",
                    "anchor": {"type": "beat", "id": "B1"},
                    "relationship_state": {"乙": {"trust": ["low"]}},
                }],
            },
        },
        contract_version="screenplay.v1",
    ))
    return evidence_repository.get_artifact(created["id"])


def test_cache_miss_does_not_alias_nested_artifact_content() -> None:
    """未命中路径原地改嵌套字段，绝不能写穿 artifact content。

    去掉隔离后实测：`trust` 会变成 `['low', 'POLLUTED']`——调用方的一次
    规范化污染了同一读作用域里所有其它读者看到的产物内容。
    """
    art = _planned_screenplay_artifact()
    source = art["content"]["narrative_plan"]["character_states"][0][
        "relationship_state"
    ]

    model = screenplay_from_artifact_record(art)
    state = model.narrative_plan.character_states[0].relationship_state
    assert state["乙"] is not source["乙"], "嵌套值仍与 artifact content 同一对象"

    state["乙"]["trust"].append("POLLUTED")
    assert source["乙"]["trust"] == ["low"]


def test_two_callers_never_share_one_model() -> None:
    """命中与未命中都必须给出彼此独立的模型。"""
    art = _planned_screenplay_artifact()

    first = screenplay_from_artifact_record(art)   # miss
    second = screenplay_from_artifact_record(art)  # hit

    assert first is not second
    first_state = first.narrative_plan.character_states[0].relationship_state
    second_state = second.narrative_plan.character_states[0].relationship_state
    assert first_state["乙"] is not second_state["乙"]

    first_state["乙"]["trust"].append("POLLUTED")
    assert second_state["乙"]["trust"] == ["low"]

    third = screenplay_from_artifact_record(art)
    assert third.narrative_plan.character_states[0].relationship_state == {
        "乙": {"trust": ["low"]}
    }


def test_scalar_edits_never_reach_the_artifact_record() -> None:
    art = _legacy_screenplay_artifact()
    original_title = art["content"]["title"]

    first = screenplay_from_artifact_record(art)
    first.title = "被调用方改过"

    assert art["content"]["title"] == original_title
    assert screenplay_from_artifact_record(art).title == original_title


# ------------------------------------------------------------------ 缺陷 3

@pytest.mark.parametrize("announce_the_write", [True, False])
def test_read_scope_never_serves_a_row_older_than_a_write_inside_it(
    announce_the_write: bool,
) -> None:
    """作用域内发生写之后，同一作用域的后续读必须看到新状态。

    `announce_the_write=False` 是关键那一半：写方**完全不知道**有这个作用域，
    一行失效代码都没调。正确性不能依赖 15 个写入点都记得配合。
    """
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
        if announce_the_write:
            evidence_repository.invalidate_artifact_read_scope(art["id"])

        after = evidence_repository.get_artifact(art["id"])

    assert after["status"] == "stale", "读作用域把作用域内的写掩盖掉了"


def test_a_write_to_an_unrelated_row_also_drops_the_memo() -> None:
    """`total_changes` 是连接级计数，判定必然保守——保守正是这里要的。

    宁可多读一次库，也不能让任何一次写被缓存掩盖。
    """
    art = _legacy_screenplay_artifact()
    other = _legacy_screenplay_artifact()

    with evidence_repository.artifact_read_scope():
        evidence_repository.get_artifact(art["id"])
        conn = db.get_conn()
        conn.execute(
            "UPDATE artifacts SET stale_reason='无关写入' WHERE id=?",
            (other["id"],),
        )
        conn.commit()
        conn.execute(
            "UPDATE artifacts SET status='stale' WHERE id=?", (art["id"],)
        )
        conn.commit()

        assert evidence_repository.get_artifact(art["id"])["status"] == "stale"


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


# ---------------------------------------------------------- episode_prep_pack

def test_prep_pack_contract_is_not_second_guessed_by_the_historical_bind_proxy() -> None:
    """回归用例（真实 EP1 生成暴露，run_17fb9f04ee8d 之前的失败）。

    _historical_screenplay_artifact_is_bound 把"被 production_revisions/
    completion_certificates/episodes 指针引用过"当作"这是需要 narrative_plan
    的历史重型产物"的代理判据。episode_prep_pack（screenplay 契约 6.0.0+）
    的原子发布同样会绑定 completion_certificates 与 episodes 指针（这正是
    发布语义要求的），代理因此在**每一次** prep_pack 发布后都会误命中，把
    刚发布的 'ready' 集重启后打回 'failed'（用户能看到的现象：观测台 run
    记录正常成功，但重启后的恢复扫描——app.domain.screenplay_ops.
    recover_screenplay_tasks，由 app.recovery.recover_all 在进程启动时调用
    ——把它标记为需要重建）。

    契约版本 >= 6 是显式自我声明："我是 prep_pack，没有 narrative_plan"；
    这个声明必须压过代理推断。"""
    art = _prep_pack_artifact()

    # 未绑定任何东西：本来就该过。
    screenplay_from_artifact_record(art)

    # 现在把它绑成 completion_certificates 的引用目标——这正是
    # _publish_prep_pack 的原子发布事务对每一份成功产物做的事，
    # 复现的是"发布后"的真实状态，不是假设。
    conn = db.get_conn()
    # completion_certificates alone is enough:
    # _historical_screenplay_artifact_is_bound checks
    # production_revisions/completion_certificates/episodes/evaluations/
    # lineage and returns True on the first match.
    conn.execute(
        """INSERT INTO completion_certificates
               (id, kind, scope_id, artifact_id, artifact_hash, issued_at)
           VALUES ('cert-prep-pack-cache', 'screenplay', 'episode-prep-pack-cache', ?, 'hash', 0)""",
        (art["id"],),
    )
    conn.commit()

    # Still must not raise: the explicit contract_version=6.0.0 declaration
    # short-circuits before the now-True historical-bind proxy is consulted.
    screenplay_from_artifact_record(evidence_repository.get_artifact(art["id"]))
