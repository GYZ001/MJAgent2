"""``app.domain.screenplay_ops._published_screenplay_revalidation_eligibility``
必须认出映射台实际发布的 ``episode_prep_pack`` 类型，不能只认已退休的重管线
``screenplay_document`` 类型。

事故复盘：EP1（ep_3d523ff4d0a4）映射台跑通、62/62 段全覆盖，published
Artifact 的 type/scope/status 全部正确（``episode_prep_pack`` /
``episode`` / ``ep_3d523ff4d0a4`` / ``approved``），但这条判据把 ``type``
硬编码成只认 ``screenplay_document``，把健康的 prep_pack 判成「类型或作用域
不匹配」，界面主操作按钮因此从「进入分镜台」换成「刷新状态」。

修复只放开 ``type`` 一个判据（同一 artifact 既可能是已退休的重管线
``screenplay_document``——唯一仍存活的生产者是
``app.domain.screenplay_ops.edit_screenplay``（手工编辑草稿发布）；
``app.production.screenplay_repair.run_screenplay_production`` 及其专属子树
（``_complete_screenplay_from_working_artifact`` /
``_reusable_recovery_document`` / ``_revalidate_or_rebuild_resume_working``，
里面另外那几处 ``type="screenplay_document"``）在 app/ 内已无任何生产调用者，
只被测试直接调用——也可能是映射台当前发布的 ``episode_prep_pack``——见
``app.production.prep_pack._publish_prep_pack``；两者是同一个「screenplay」
生产阶段在不同合同版本下的产物，见
``app.production.certificate.issue_completion_certificate`` 里完全相同的
两类型集合）。``scope_type``/``scope_id``/``status=='approved'`` 三条判据
本身没有问题——它们防的是拿别的集、或未批准的工件冒充本集已发布产物，本文件
分别单独锁死这两侧不受影响。

本文件另外用一个真实发布的 episode_prep_pack 验证：
``app.domain.common._screenplay_ready_uncached`` 里紧挨着
``_prep_pack_ready_uncached`` 的旧分支也写着
``type != "screenplay_document"``，但那一支只在 episodes.screenplay_json 的
当前投影不带 ``prep_pack_version`` 标记时才会被读到——对一个刚发布的 prep_pack
集，这条分支永远执行不到，不属于本次要修的同一族判据。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import api, db
from app.production import prep_pack

EP6_FIXTURE = (
    Path(__file__).parent / "fixtures" / "episode_prep_pack_ep6_ep_94adca9b9942.json"
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "prep-pack-revalidation-eligibility.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _seed_episode(conn, *, episode_id: str, project_id: str, episode_no: int) -> None:
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES(?,?,?,?)",
        (project_id, "prep pack fixture", "{}", db.now()),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,source_chapters,target_duration_s,
               status,screenplay_status,screenplay_character_resolutions,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            episode_id, project_id, episode_no, "Fixture", "[]", 1800,
            "planned", "pending", "[]", db.now(),
        ),
    )
    conn.commit()


def _publish_ep6_fixture(conn, episode_id: str, *, project_id: str) -> dict:
    """Publish the real EP6 episode_prep_pack export through the production
    publish path (app.production.prep_pack._publish_prep_pack) so the
    published Artifact's type/scope/status/content_hash are exactly what a
    real映射台 run produces -- not a hand-rolled shape.
    """
    _seed_episode(conn, episode_id=episode_id, project_id=project_id, episode_no=6)
    payload = json.loads(EP6_FIXTURE.read_text(encoding="utf-8"))
    # 冻结的历史导出自带旧版 prep_pack_version；真实发布里 payload 生成与发布
    # 同一次调用完成，版本号不可能不同步——对齐到当前运行版本号，不然测的是
    # 一个真实流程里永远不会出现的版本漂移（同 test_prep_pack_storyboard_
    # projection.py 的处理）。
    payload["prep_pack_version"] = prep_pack.PREP_PACK_VERSION
    return prep_pack._publish_prep_pack(episode_id=episode_id, payload=payload, run_id=None)


def test_eligible_when_published_points_to_approved_episode_prep_pack():
    """健康的 episode_prep_pack（type/scope/status 全对，正是 EP1 事故里的
    真实状态）必须通过资格检查。"""
    conn = db.get_conn()
    episode_id = "ep-prep-pack-eligible"
    result = _publish_ep6_fixture(conn, episode_id, project_id="proj-eligible")
    episode = dict(conn.execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,),
    ).fetchone())

    eligibility = api._published_screenplay_revalidation_eligibility(episode, conn=conn)

    assert eligibility["eligible"] is True
    assert eligibility["code"] == "published_screenplay_revalidation_eligible"
    assert eligibility["artifact_id"] == result["artifact_id"]
    assert eligibility["screenplay"] is not None
    assert eligibility["screenplay"].episode_no == 6


def test_blocked_when_published_artifact_belongs_to_a_different_episode():
    """published 指针指向别的集发布的 episode_prep_pack：scope_id 不匹配必须
    仍然被拦——本次修复只放开 type，绝不放开 scope。"""
    conn = db.get_conn()
    donor_episode_id = "ep-prep-pack-donor"
    _publish_ep6_fixture(conn, donor_episode_id, project_id="proj-donor")
    donor_episode = dict(conn.execute(
        "SELECT * FROM episodes WHERE id=?", (donor_episode_id,),
    ).fetchone())
    donor_artifact_id = donor_episode["published_screenplay_artifact_id"]
    assert donor_artifact_id

    _seed_episode(
        conn, episode_id="ep-prep-pack-victim", project_id="proj-victim", episode_no=1,
    )
    conn.execute(
        "UPDATE episodes SET published_screenplay_artifact_id=?,"
        "screenplay_artifact_id=? WHERE id='ep-prep-pack-victim'",
        (donor_artifact_id, donor_artifact_id),
    )
    conn.commit()
    victim_episode = dict(conn.execute(
        "SELECT * FROM episodes WHERE id='ep-prep-pack-victim'",
    ).fetchone())

    eligibility = api._published_screenplay_revalidation_eligibility(victim_episode, conn=conn)

    assert eligibility["eligible"] is False
    assert eligibility["code"] == "published_screenplay_authority_invalid"


def test_blocked_when_episode_prep_pack_artifact_is_not_approved():
    """published 指针指向同一集、类型正确，但 Artifact 处于 candidate（未批准）
    状态：仍然必须被拦——status 判据本次修复同样不放开。"""
    conn = db.get_conn()
    episode_id = "ep-prep-pack-unapproved"
    result = _publish_ep6_fixture(conn, episode_id, project_id="proj-unapproved")
    conn.execute(
        "UPDATE artifacts SET status='candidate' WHERE id=?",
        (result["artifact_id"],),
    )
    conn.commit()
    episode = dict(conn.execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,),
    ).fetchone())

    eligibility = api._published_screenplay_revalidation_eligibility(episode, conn=conn)

    assert eligibility["eligible"] is False
    assert eligibility["code"] == "published_screenplay_not_approved"


def test_screenplay_ready_dispatches_healthy_prep_pack_without_the_legacy_type_gate():
    """``app.domain.common._screenplay_ready_uncached`` 里紧挨着
    ``_prep_pack_ready_uncached``（:535）的旧分支也硬编码
    ``type != "screenplay_document"``（:615），形状和本文件其它用例修的那处
    几乎一样。区别在于:那条分支只有在 ``episodes.screenplay_json`` 的当前
    投影解析后 *不含* ``prep_pack_version`` 标记时才会被执行到（见
    ``_screenplay_ready_uncached`` 顶部的 dispatch）；``_publish_prep_pack``
    在同一次 UPDATE 里把 screenplay_json/screenplay_artifact_id/
    published_screenplay_artifact_id 三者一起写成同一份 prep_pack payload，
    所以一个健康发布的 prep_pack 集永远走 _prep_pack_ready_uncached，永远
    到不了那条旧分支——不属于本次要修的同一族判据，这里只锁死这个事实，
    不改代码。"""
    conn = db.get_conn()
    episode_id = "ep-prep-pack-ready-dispatch"
    _publish_ep6_fixture(conn, episode_id, project_id="proj-ready-dispatch")
    episode = dict(conn.execute(
        "SELECT * FROM episodes WHERE id=?", (episode_id,),
    ).fetchone())

    assert api._screenplay_ready(episode) is True
