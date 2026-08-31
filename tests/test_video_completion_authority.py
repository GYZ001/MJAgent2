from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from app import db, hiagent
from app.completion_grant import (
    ensure_video_budget_authority_tables,
    GrantValidationError,
    authorize_episode_video_budget_increment,
    episode_video_completion_budget_requirement,
    episode_video_budget_snapshot,
    issue_video_completion_grant,
    reserve_provider_video_budget,
    validate_video_grant,
)
from app.schemas import Bible, Shot, Storyboard, World
from app.video_supervisor import (
    StoryboardRepairProposal,
    VideoSupervisorCheckpoint,
    _prepare_episode_reference_assets,
    _repair_authority_ids,
    _semantic_storyboard_repair_proposal,
    _ensure_supervisor_video_plan,
)
from tests.conftest import patch_completion_grant_everywhere, patch_video_plan_everywhere, patch_video_supervisor_everywhere


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "video-completion-authority.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def test_provider_video_budget_claims_never_exceed_approved_cap() -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('budget-p','P','created',1)"
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('budget-e','budget-p',1,'confirmed',1)"""
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,characters,dialogues
           ) VALUES('budget-s','budget-e',1,5,'[]','[]')"""
    )
    for index in (1, 2, 3, 4):
        conn.execute(
            """INSERT INTO shot_versions(
                   id,shot_id,version_no,prompt_text,idem_key,status,created_at
               ) VALUES(?,?,?,?,?,'queued',1)""",
            (
                f"budget-v{index}",
                "budget-s",
                index,
                "prompt",
                f"budget-key-{index}",
            ),
        )
        conn.execute(
            """INSERT INTO jobs(
                   id,kind,shot_id,version_id,episode_id,project_id,status,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,'queued',1,1)""",
            (
                f"budget-j{index}",
                "video",
                "budget-s",
                f"budget-v{index}",
                "budget-e",
                "budget-p",
            ),
        )
    conn.commit()

    # 授权口径见 completion_grant.VIDEO_BUDGET_RETRY_MARGIN_MULTIPLIER：cap
    # 不再等于首轮预估的 4.0，而是含重试余量的 4.0*3=12.0——覆盖同一镜头
    # 最多 2 次自动重投（合计 3 次付费尝试），但余量不是无限的。
    cap = authorize_episode_video_budget_increment(
        "budget-e",
        4.0,
        source="test-approval",
        conn=conn,
    )
    assert cap == 12.0
    for index in (1, 2, 3):
        assert reserve_provider_video_budget(
            episode_id="budget-e",
            job_id=f"budget-j{index}",
            version_id=f"budget-v{index}",
            operation_id=f"video-create-budget-v{index}",
            amount_cny=4.0,
            conn=conn,
        ) is True
    # 第 4 次尝试超出含余量的 cap（12.0）——余量覆盖真实重投需求，但仍是
    # 有限的，超出仍必须被拦，不能靠它把预算保护整个放宽。
    assert reserve_provider_video_budget(
        episode_id="budget-e",
        job_id="budget-j4",
        version_id="budget-v4",
        operation_id="video-create-budget-v4",
        amount_cny=4.0,
        conn=conn,
    ) is False
    assert episode_video_budget_snapshot("budget-e", conn=conn) == {
        "baseline_cny": 0.0,
        "claimed_cny": 12.0,
        "used_cny": 12.0,
        "cap_cny": 12.0,
        "remaining_cny": 0.0,
    }

    authorize_episode_video_budget_increment(
        "budget-e",
        4.0,
        source="test-topup",
        conn=conn,
    )
    assert reserve_provider_video_budget(
        episode_id="budget-e",
        job_id="budget-j4",
        version_id="budget-v4",
        operation_id="video-create-budget-v4",
        amount_cny=4.0,
        conn=conn,
    ) is True


def test_concurrent_provider_video_budget_claims_share_one_atomic_cap() -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('race-p','P','created',1)"
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('race-e','race-p',1,'confirmed',1)"""
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,characters,dialogues
           ) VALUES('race-s','race-e',1,5,'[]','[]')"""
    )
    for index in (1, 2):
        conn.execute(
            """INSERT INTO shot_versions(
                   id,shot_id,version_no,prompt_text,idem_key,status,created_at
               ) VALUES(?,?,?,?,?,'queued',1)""",
            (
                f"race-v{index}",
                "race-s",
                index,
                "prompt",
                f"race-key-{index}",
            ),
        )
        conn.execute(
            """INSERT INTO jobs(
                   id,kind,shot_id,version_id,episode_id,project_id,status,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,'queued',1,1)""",
            (
                f"race-j{index}",
                "video",
                "race-s",
                f"race-v{index}",
                "race-e",
                "race-p",
            ),
        )
    conn.commit()
    # 授权 4.0 首轮预估，含重试余量后 cap=12.0（见
    # completion_grant.VIDEO_BUDGET_RETRY_MARGIN_MULTIPLIER）。两笔并发认领
    # 各 7.0：合计 14.0 超过 12.0，只有一笔能挤进去——用来测原子互斥，claim
    # 金额必须仍大到能撞上含余量后的新 cap，不能沿用旧的零余量数字。
    authorize_episode_video_budget_increment(
        "race-e",
        4.0,
        source="concurrency-test",
        conn=conn,
    )
    # 建表在起线程之前做完：DDL 落进竞态窗口会让另一线程已编译的语句抛
    # sqlite3.OperationalError: database schema has changed，那是 setup 自己
    # 打架，与本测试要验的「并发认领共享同一个原子 cap」无关。
    ensure_video_budget_authority_tables(conn)
    barrier = threading.Barrier(2)

    def reserve(index: int) -> bool:
        barrier.wait()
        # 连接必须在线程内部取。app/db.py 的 get_conn() 是线程局部的
        # （_local = threading.local()），每个线程拿到自己的连接，两笔认领才会
        # 真的在 SQLite 层面争用同一个 cap——这正是本测试要验的东西。
        #
        # 2026-08-31 教训：连接所有权收紧那一轮（aa4d0d9）把主线程的 conn 传进
        # 了这里，两个线程于是共用一条连接，没有任何真实争用，两笔都能成功。
        # 症状是间歇性的（有时报 schema has changed，有时断言 [True, True]），
        # 所以它在几轮全量里都侥幸绿过——被削弱的测试比红的测试危险。
        return reserve_provider_video_budget(
            episode_id="race-e",
            job_id=f"race-j{index}",
            version_id=f"race-v{index}",
            operation_id=f"video-create-race-v{index}",
            amount_cny=7.0,
            conn=db.get_conn(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, (1, 2)))

    assert sorted(results) == [False, True]
    assert episode_video_budget_snapshot("race-e", conn=conn) == {
        "baseline_cny": 0.0,
        "claimed_cny": 7.0,
        "used_cny": 7.0,
        "cap_cny": 12.0,
        "remaining_cny": 5.0,
    }


def test_completion_budget_requirement_includes_sunk_duplicate_claims() -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('required-p','P','created',1)"
    )
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,status,created_at)
           VALUES('required-e','required-p',1,'confirmed',1)"""
    )
    for shot_no in (1, 2):
        conn.execute(
            """INSERT INTO shots(
                   id,episode_id,shot_no,duration_s,characters,dialogues
               ) VALUES(?,?,?,5,'[]','[]')""",
            (f"required-s{shot_no}", "required-e", shot_no),
        )
    for index in (1, 2):
        conn.execute(
            """INSERT INTO shot_versions(
                   id,shot_id,version_no,prompt_text,idem_key,status,created_at
               ) VALUES(?,?,?,?,?,'queued',1)""",
            (
                f"required-v{index}",
                "required-s1",
                index,
                "prompt",
                f"required-key-{index}",
            ),
        )
        conn.execute(
            """INSERT INTO jobs(
                   id,kind,shot_id,version_id,episode_id,project_id,status,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,'queued',1,1)""",
            (
                f"required-j{index}",
                "video",
                "required-s1",
                f"required-v{index}",
                "required-e",
                "required-p",
            ),
        )
    conn.commit()
    authorize_episode_video_budget_increment(
        "required-e",
        20,
        source="test-approval",
        conn=conn,
    )
    for index in (1, 2):
        operation_id = f"video-create-required-v{index}"
        assert reserve_provider_video_budget(
            episode_id="required-e",
            job_id=f"required-j{index}",
            version_id=f"required-v{index}",
            operation_id=operation_id,
            amount_cny=4,
            conn=conn,
        ) is True

    assert episode_video_completion_budget_requirement("required-e", conn=conn) == {
        "used_cny": 8.0,
        "claimed_current_shots": 1,
        "shots_total": 2,
        "unclaimed_first_pass_cny": 4.0,
        "required_completion_cap_cny": 12.0,
    }


def _narrative_authority_case() -> dict:
    """Publish a narrative-authority screenplay + storyboard with a real
    certificate/revision chain, then issue a video completion grant against it.

    This intentionally does not use the deleted cold-audience-review/
    calibration path (``app.narrative_review`` / ``app.narrative_calibration``,
    removed together with their certificate-authority tests). Those were
    already score-only/non-authoritative for release qualification --
    ``_narrative_review_material`` in app.completion_grant hardcodes
    ``required=False, verified=True`` -- so the certificate/shots drift
    detection under test here does not depend on them at all.

    Reuses ``tests.test_narrative_continuity._persist_review_projection``,
    which was explicitly kept alive (with an in-file docstring explaining
    why) after that deletion specifically because it is a generic "publish
    one narrative-authority screenplay" fixture with no such dependency,
    and is already relied on by tests/test_screenplay_authority.py.
    """
    from app.continuity import PROMPT_CONTRACT_VERSION
    from app.evidence import repository as evidence_repository
    from app.harness.types import Evaluation, EvidenceArtifact
    from app.production.certificate import (
        consume_completion_certificate,
        issue_completion_certificate,
    )
    from app.production.revision import (
        ensure_production_revision,
        mark_baseline_generated,
        set_published_artifact,
    )
    from app.schemas import StoryboardOutline, StoryboardOutlineShot
    from app.storyboard_authority import persist_storyboard_outline_authority
    from tests.test_narrative_continuity import _board, _persist_review_projection, _screenplay

    screenplay = _screenplay()
    screenplay.full_script_text = "A complete source-grounded screenplay projection."
    board = _board()
    board.shots[-1].is_final = True
    for shot in board.shots:
        shot.prompt_contract_version = PROMPT_CONTRACT_VERSION
    screenplay_artifact, _shot_artifacts = _persist_review_projection(screenplay, board)

    outline = StoryboardOutline(
        episode_no=board.episode_no,
        shots=[
            StoryboardOutlineShot.model_validate({
                key: value
                for key, value in shot.model_dump(mode="json").items()
                if key in StoryboardOutlineShot.model_fields
            })
            for shot in board.shots
        ],
    )
    persist_storyboard_outline_authority("episode-generic", outline)

    contract_version = "narrative-continuity.v1"
    working_candidate = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="storyboard_document",
            scope_type="episode",
            scope_id="episode-generic",
            status="approved",
            trust_level="T2",
            content=board.model_dump(mode="json"),
            parent_artifact_ids=[screenplay_artifact["id"]],
            contract_version=contract_version,
        )
    )
    revision = ensure_production_revision(
        episode_id="episode-generic",
        kind="storyboard",
        resume=False,
    )
    mark_baseline_generated(
        revision.id,
        baseline_artifact_id=working_candidate["id"],
        working_artifact_id=working_candidate["id"],
    )
    conn = db.get_conn()
    conn.execute(
        """UPDATE production_revisions
              SET input_fingerprint='storyboard-input-v1',
                  contract_version=?,qa_profile_version='storyboard-full-gate-2'
            WHERE id=?""",
        (contract_version, revision.id),
    )
    conn.commit()
    full_gate = evidence_repository.create_evaluation(
        working_candidate["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="storyboard_full_gate",
            evaluator_version=contract_version,
            status="passed",
            hard_gate_passed=True,
            evaluation_role="runtime_gate",
            runtime_blocking=True,
            score=100,
        ),
    )
    storyboard_certificate = issue_completion_certificate(
        kind="storyboard",
        scope_id="episode-generic",
        artifact_id=working_candidate["id"],
        artifact_hash=working_candidate["content_hash"],
        input_fingerprint="storyboard-input-v1",
        contract_version=contract_version,
        qa_profile_version="storyboard-full-gate-2",
        evaluation_ids=[full_gate["id"]],
        production_revision_id=revision.id,
    )
    consume_completion_certificate(storyboard_certificate.certificate_id)
    set_published_artifact(
        revision.id,
        working_candidate["id"],
        certificate_id=storyboard_certificate.certificate_id,
    )
    conn.execute("UPDATE episodes SET status='confirmed' WHERE id='episode-generic'")
    conn.commit()

    episode = conn.execute(
        "SELECT project_id,storyboard_artifact_id FROM episodes WHERE id='episode-generic'"
    ).fetchone()
    grant, _token = issue_video_completion_grant(
        episode_id="episode-generic",
        project_id=episode["project_id"],
        storyboard_artifact_id=episode["storyboard_artifact_id"],
        shots_total=len(board.shots),
    )
    return {"board": board, "working_candidate": working_candidate, "grant": grant}


@pytest.mark.parametrize("drift", ["certificate", "shots"])
@pytest.mark.asyncio
async def test_asset_preparation_has_zero_calls_after_release_authority_drift(
    monkeypatch,
    drift: str,
) -> None:
    """A narrative-authority episode's completion certificate binds an exact
    artifact hash; its storyboard release authority separately binds the
    live shots table's projection to that same frozen artifact content
    (app.production.certificate.verify_current_storyboard_completion_authority).
    Corrupting either after the grant was issued must fail the paid-asset
    boundary closed, before any external provider call is made -- not
    silently generate against stale/tampered authority.
    """
    case = _narrative_authority_case()
    grant = case["grant"]
    conn = db.get_conn()
    if drift == "certificate":
        certificate_id = conn.execute(
            "SELECT storyboard_completion_certificate_id FROM episodes "
            "WHERE id='episode-generic'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE completion_certificates SET artifact_hash='drifted' WHERE id=?",
            (certificate_id,),
        )
    else:
        conn.execute(
            "UPDATE shots SET action_desc=action_desc || ' authority drift' "
            "WHERE episode_id='episode-generic' AND shot_no=1"
        )
    conn.commit()

    calls: list[str] = []

    def forbidden_scan(_episode_id: str):
        calls.append("scan")
        raise AssertionError("authority verifier must run before asset scanning")

    async def forbidden_external(*_args, **_kwargs):
        calls.append("external")
        raise AssertionError("stale release must not call an asset provider")

    import app.multiview as multiview
    import app.refs as refs
    import app.scenes as scenes

    patch_video_supervisor_everywhere(monkeypatch, "_reference_asset_scan", forbidden_scan)
    monkeypatch.setattr(multiview, "complete_legacy_character_pack", forbidden_external)
    monkeypatch.setattr(multiview, "complete_legacy_scene_pack", forbidden_external)
    monkeypatch.setattr(refs, "generate_refs", forbidden_external)
    monkeypatch.setattr(scenes, "generate_scene_refs", forbidden_external)

    cp = VideoSupervisorCheckpoint(
        episode_id="episode-generic",
        grant_id=grant.grant_id,
        storyboard_artifact_id=grant.storyboard_artifact_id,
    )
    with pytest.raises(GrantValidationError) as fenced:
        await _prepare_episode_reference_assets(
            "episode-generic",
            cp=cp,
            run_id=None,
        )
    assert fenced.value.code in {
        "RELEASE_QUALIFICATION_INVALID",
        "RELEASE_QUALIFICATION_CHANGED",
    }
    assert calls == []


def test_grant_recomputes_bound_plan_and_capability_snapshot(monkeypatch) -> None:
    from app.evidence import repository as evidence_repository
    import app.video_plan as video_plan
    from tests.test_video_plan_reconcile import _conn

    conn = _conn()
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "provider")
    monkeypatch.setattr(
        hiagent,
        "active_model",
        lambda _kind, _provider=None: "model",
    )
    # completion_grant 是包（2026-08-31 拆分），裸的 setattr 只改到包属性，够不到
    # 各子模块自己绑的 get_conn 副本——必须走 helper。evidence_repository 是单
    # 文件模块，循环打桩仍然有效。
    patch_completion_grant_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(evidence_repository, "get_conn", lambda: conn)
    patch_video_plan_everywhere(monkeypatch, "get_conn", lambda: conn)
    grant, _token = issue_video_completion_grant(
        episode_id="e",
        project_id="p",
        storyboard_artifact_id="storyboard_rev_1",
        shots_total=2,
    )
    assert grant.episode_video_plan_id == "evp-1"
    assert grant.episode_video_plan_revision == 1
    assert grant.video_plan_release_hash
    assert grant.capability_snapshot_id == "cap-1"

    conn.execute("UPDATE shots SET adopted_version_id='v1' WHERE id='s1'")
    conn.commit()
    video_plan.reconcile_adopted_revision("s1", "v1", conn=conn)
    assert validate_video_grant(
        grant.grant_id,
        episode_id="e",
        storyboard_artifact_id="storyboard_rev_1",
    ).grant_id == grant.grant_id

    conn.execute(
        "UPDATE provider_video_capability_snapshots SET probe_result='drifted' WHERE id='cap-1'"
    )
    conn.commit()
    with pytest.raises(GrantValidationError) as stale:
        validate_video_grant(
            grant.grant_id,
            episode_id="e",
            storyboard_artifact_id="storyboard_rev_1",
        )
    assert stale.value.code == "RELEASE_QUALIFICATION_CHANGED"


def test_grant_leaves_plan_pending_after_provider_selection_changes(
    monkeypatch,
) -> None:
    from app.evidence import repository as evidence_repository
    from tests.test_video_plan_reconcile import _conn

    conn = _conn()
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "provider-2")
    monkeypatch.setattr(
        hiagent,
        "active_model",
        lambda _kind, _provider=None: "model-2",
    )
    # completion_grant 是包（2026-08-31 拆分），裸的 setattr 只改到包属性，够不到
    # 各子模块自己绑的 get_conn 副本——必须走 helper。evidence_repository 是单
    # 文件模块，循环打桩仍然有效。
    patch_completion_grant_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(evidence_repository, "get_conn", lambda: conn)
    patch_video_plan_everywhere(monkeypatch, "get_conn", lambda: conn)

    grant, _token = issue_video_completion_grant(
        episode_id="e",
        project_id="p",
        storyboard_artifact_id="storyboard_rev_1",
        shots_total=2,
    )

    assert grant.episode_video_plan_id is None
    assert grant.episode_video_plan_revision is None
    assert grant.capability_snapshot_id is None
    assert grant.release_qualification["generation_plan"] == {
        "applicable": False,
        "compatibility": "plan_pending_at_grant_issue",
    }


@pytest.mark.asyncio
async def test_supervisor_checkpoint_acquires_exact_grant_plan_binding(monkeypatch) -> None:
    from app.evidence import repository as evidence_repository
    from tests.test_video_plan_reconcile import _conn

    conn = _conn()
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "provider")
    monkeypatch.setattr(
        hiagent,
        "active_model",
        lambda _kind, _provider=None: "model",
    )
    # completion_grant 是包（2026-08-31 拆分），裸的 setattr 只改到包属性，够不到
    # 各子模块自己绑的 get_conn 副本——必须走 helper。evidence_repository 是单
    # 文件模块，循环打桩仍然有效。
    patch_completion_grant_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(evidence_repository, "get_conn", lambda: conn)
    # app.video_supervisor and app.video_plan are real packages: a loop-based
    # setattr on the bare package object would silently miss submodules like
    # video_supervisor/authority.py or video_plan/release_manifest.py that
    # hold their own `from app.db import get_conn` copy (see
    # patch_video_supervisor_everywhere's / patch_video_plan_everywhere's
    # docstrings in tests/conftest.py).
    patch_video_supervisor_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_video_plan_everywhere(monkeypatch, "get_conn", lambda: conn)
    grant, _token = issue_video_completion_grant(
        episode_id="e",
        project_id="p",
        storyboard_artifact_id="storyboard_rev_1",
        shots_total=2,
    )
    cp = VideoSupervisorCheckpoint(
        episode_id="e",
        grant_id=grant.grant_id,
        storyboard_artifact_id="storyboard_rev_1",
    )
    verified = await _ensure_supervisor_video_plan(cp)

    assert verified is not None
    assert cp.episode_video_plan_id == grant.episode_video_plan_id == "evp-1"
    assert cp.episode_video_plan_revision == grant.episode_video_plan_revision == 1
    assert cp.video_plan_release_hash == grant.video_plan_release_hash
    assert cp.capability_snapshot_id == grant.capability_snapshot_id == "cap-1"


@pytest.mark.asyncio
async def test_semantic_repair_accepts_unfamiliar_action_without_phrase_rules(
    monkeypatch,
) -> None:
    original = Shot(
        shot_no=1,
        shot_id="shot-authority-1",
        duration_s=5,
        shot_size="medium",
        camera_move="locked",
        scene_setting="abstract chamber",
        scene_name="abstract chamber",
        characters=["A"],
        action_desc="A crosses the chamber and seals the unstable aperture in one motion.",
        first_frame_desc="A faces an unstable aperture across the silent chamber.",
        last_frame_desc="A stands beside the sealed aperture after the movement resolves.",
        source_excerpt="A crosses the chamber and seals the unstable aperture before it expands.",
    )
    candidate = original.model_copy(
        update={
            "action_desc": (
                "A pivots beneath the unfamiliar lattice, redirects its momentum, "
                "and settles at the sealed boundary."
            )
        }
    )
    affected = _repair_authority_ids([original, candidate])
    proposal = StoryboardRepairProposal(
        proposal_id="semantic-proposal-unfamiliar",
        base_shot_id="database-shot-1",
        operation="replace",
        reason="The visible state change needs a single readable kinetic intention.",
        expected_total_duration_s=5,
        affected_authority=affected,
        candidate_shots=[candidate],
    )

    from app.harness import model_gateway

    from tests.conftest import patch_validators_everywhere

    async def fake_chat(*_args, **_kwargs):
        return json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    patch_validators_everywhere(monkeypatch, "validate_storyboard", lambda *_a, **_k: [])
    patch_validators_everywhere(
        monkeypatch,
        "validate_storyboard_continuity_contract",
        lambda *_a, **_k: [],
    )
    bible = Bible(characters=[], world=World(visual_style_canonical="graphic"))
    result, candidate_board = await _semantic_storyboard_repair_proposal(
        board=Storyboard(episode_no=1, shots=[original]),
        shot_index=0,
        database_shot_id="database-shot-1",
        screenplay=None,
        bible=bible,
        target_duration_s=5,
        episode_id="legacy-episode",
        repair_plan=None,
        observed_issue_codes=["UNFAMILIAR_RELATION_FAILURE"],
    )
    assert result.proposal_id == "semantic-proposal-unfamiliar"
    assert candidate_board.shots[0].action_desc == candidate.action_desc


def test_completion_grant_connection_ownership_is_explicit() -> None:
    """app/completion_grant.py 的公共入口不得给 conn 留默认值。

    这些函数决定「这一集能花多少钱」「预留算不算数」「grant 还有没有效」，全都在
    调用方的事务里被读写。conn=None + `db = conn or get_conn()` 让漏传毫无阻力：
    被调用方回退到自己的连接，读到的是调用方事务里看不见的状态——金额判断与事实
    不一致，而且不报错。必传之后，漏传在调用那一刻就是 TypeError。
    （CLAUDE.md「Ownership Must Be Explicit」：可选参数是缺陷的温床。）
    """
    import ast
    import inspect
    from pathlib import Path

    from app import completion_grant

    source_path = Path(inspect.getfile(completion_grant))
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        defaulted = dict(zip([a.arg for a in args.kwonlyargs], args.kw_defaults))
        if args.defaults:
            positional = [a.arg for a in args.args][-len(args.defaults):]
            defaulted.update(dict(zip(positional, args.defaults)))
        default = defaulted.get("conn")
        if isinstance(default, ast.Constant) and default.value is None:
            offenders.append(f"{node.name}（第 {node.lineno} 行）")
    assert not offenders, (
        "以下函数的 conn 又有默认值了：" + "、".join(offenders)
        + "。漏传会静默读到另一个事务的状态。"
    )


def test_completion_grant_never_falls_back_to_its_own_connection() -> None:
    """源码级守卫：不得再出现 `conn or get_conn()` 兜底。

    这个写法比 conn=None 本身更隐蔽——签名看起来像是"可选优化"，实际是在调用方
    的事务之外另开一条连接读写同一批余额。
    """
    import ast
    import inspect
    from pathlib import Path

    from app import completion_grant

    tree = ast.parse(Path(inspect.getfile(completion_grant)).read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.Or)
        and any(
            isinstance(value, ast.Call) and getattr(value.func, "id", None) == "get_conn"
            for value in node.values
        )
    ]
    assert not offenders, f"第 {offenders} 行出现了 `conn or get_conn()` 兜底"
