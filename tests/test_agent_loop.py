from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel

from app import api, db, stages
from app.evaluations.issues import issues_from_messages
from app.evidence import repository
from app.harness.context import ContextPack
from app.harness.types import EvidenceArtifact
from app.loops import AgentLoop, AgentLoopPolicy
from app.loops.base import AgentLoopFailure
from app.orchestration.engine import WorkflowRecorder
from app.schemas import EpisodeScreenplay
from app.stages import StoryboardShotDraft


class Candidate(BaseModel):
    value: int


def _loop(*, allow_warning: bool = False, max_iterations: int = 4) -> AgentLoop[Candidate]:
    return AgentLoop(
        stage_key="screenplay",
        contract_key="screenplay",
        goal="produce a valid candidate",
        scope_type="episode",
        scope_id="e1",
        artifact_type="episode_screenplay",
        policy=AgentLoopPolicy(
            max_iterations=max_iterations,
            stall_rounds=2,
            min_quality_gain=0.03,
            no_gain_rounds=2,
            allow_warning_candidate=allow_warning,
        ),
    )


def _evaluate(raw: str):
    value = Candidate.model_validate(json.loads(raw))
    messages = [] if value.value >= 2 else [f"value {value.value} is below contract"]
    return value, issues_from_messages(messages, subject="episode:e1")


def test_agent_loop_repairs_then_accepts() -> None:
    outputs = ['{"value": 1}', '{"value": 2}']

    async def producer(iteration, *_args):
        return outputs[iteration - 1]

    result = asyncio.run(_loop().run(producer, _evaluate))

    assert result.status == "accepted"
    assert result.exit_reason == "contract_passed"
    assert result.iterations == 2
    assert result.value.value == 2


def test_agent_loop_keeps_repairing_when_structural_issue_changes() -> None:
    """Regression for ERR-20260725-c8bb0d.

    JSON syntax, one bad field type, and another bad field type are distinct
    repair progress.  They must not share one coarse SCHEMA_INVALID stall
    fingerprint or accumulate no-quality-gain while no candidate exists.
    """
    outputs = ["syntax", "event", "list", "valid"]

    async def producer(iteration, *_args):
        return outputs[iteration - 1]

    def evaluate(raw: str):
        if raw == "syntax":
            return None, issues_from_messages(
                ["JSON 解析失败（Expecting ',' delimiter）"], subject="storyboard_checkpoint:e1:2"
            )
        if raw == "event":
            return None, issues_from_messages(
                ["字段 shot.story_event_id：Input should be a valid string"],
                subject="storyboard_checkpoint:e1:2",
            )
        if raw == "list":
            return None, issues_from_messages(
                ["字段 shot.new_information_ids：Input should be a valid list"],
                subject="storyboard_checkpoint:e1:2",
            )
        return Candidate(value=2), []

    result = asyncio.run(_loop(max_iterations=4).run(producer, evaluate))

    assert result.status == "accepted"
    assert result.iterations == 4


def test_schema_issue_fingerprint_includes_field_and_rule() -> None:
    subject = "storyboard_checkpoint:e1:2"
    json_issue = issues_from_messages(["JSON 解析失败（坏引号）"], subject=subject)[0]
    string_issue = issues_from_messages(
        ["字段 shot.story_event_id：Input should be a valid string"], subject=subject
    )[0]
    list_issue = issues_from_messages(
        ["字段 shot.new_information_ids：Input should be a valid list"], subject=subject
    )[0]

    assert len({json_issue.fingerprint, string_issue.fingerprint, list_issue.fingerprint}) == 3
    assert "shot.story_event_id" in string_issue.fingerprint
    assert "string_type" in string_issue.fingerprint


def test_agent_loop_stops_same_issue_fingerprint_and_rejects_blocker_warning() -> None:
    """VAL-422：allow_warning_candidate 不得把带 blocker 的候选当成 warning 通过。"""

    async def producer(_iteration, *_args):
        return '{"value": 1}'

    with pytest.raises(AgentLoopFailure) as exc:
        asyncio.run(_loop(allow_warning=True).run(producer, _evaluate))

    assert exc.value.exit_reason == "stalled"
    assert exc.value.iterations == 2
    assert exc.value.issues[0].repairable is True


def test_agent_loop_stops_when_issue_set_changes_without_quality_gain() -> None:
    async def producer(iteration, *_args):
        return json.dumps({"value": -iteration})

    def evaluate(raw: str):
        value = Candidate.model_validate(json.loads(raw))
        return value, issues_from_messages(
            [f"distinct problem {abs(value.value)}"], subject=f"episode:e{abs(value.value)}"
        )

    with pytest.raises(AgentLoopFailure) as exc:
        asyncio.run(_loop(allow_warning=True).run(producer, evaluate))

    assert exc.value.exit_reason == "no_quality_gain"
    assert exc.value.iterations == 3


def test_agent_loop_persists_iterations_candidates_and_evaluations(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "agent-loop.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    recorder = WorkflowRecorder.create(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="input-v1",
    )
    recorder.start()
    source_artifact = repository.create_artifact(
        EvidenceArtifact(
            type="novel_source",
            scope_type="episode",
            scope_id="e1",
            status="approved",
            trust_level="T4",
            content={"text": "source"},
        )
    )
    outputs = ['{"value": 1}', '{"value": 2}']

    async def producer(iteration, *_args):
        return outputs[iteration - 1]

    async def operation():
        return await _loop().run(producer, _evaluate)

    _, result = asyncio.run(
        recorder.step(
            "screenplay",
            operation,
            contract_key="screenplay",
            input_artifact_ids=[source_artifact["id"]],
        )
    )
    recorder.succeed()

    steps = repository.get_steps(recorder.run_id)
    iteration_steps = [step for step in steps if step["step_key"] == "screenplay.iteration"]
    artifacts = db.rows_to_dicts(db.get_conn().execute(
        "SELECT * FROM artifacts WHERE scope_id='e1' AND type='episode_screenplay' ORDER BY version"
    ).fetchall())
    evaluations = db.rows_to_dicts(db.get_conn().execute(
        "SELECT * FROM evaluations ORDER BY created_at"
    ).fetchall())

    assert result.artifact_id == artifacts[-1]["id"]
    assert [step["status"] for step in iteration_steps] == ["WARNING", "SUCCEEDED"]
    assert [artifact["trust_level"] for artifact in artifacts] == ["T1", "T2"]
    assert artifacts[-1]["status"] == "approved"
    assert json.loads(artifacts[-1]["parent_artifact_ids_json"]) == [source_artifact["id"]]
    assert len(evaluations) == 2


def test_warning_fallback_keeps_value_issues_and_artifact_from_same_iteration(
    tmp_path, monkeypatch
) -> None:
    """A later T0 repair must not be linked to an earlier schema-valid value."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "agent-loop-coherence.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    recorder = WorkflowRecorder.create(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="input-v1",
    )
    recorder.start()
    outputs = ['{"value": 1}', '{"broken": 1}', '{"broken": 2}', '{"broken": 3}']

    async def producer(iteration, *_args):
        return outputs[iteration - 1]

    def evaluator(raw: str):
        payload = json.loads(raw)
        if "value" in payload:
            value = Candidate.model_validate(payload)
            return value, issues_from_messages(
                ["candidate business problem"], subject="episode:e1"
            )
        return None, issues_from_messages(
            [f"字段 value：第 {payload['broken']} 次修复缺失"],
            subject=f"episode:broken-{payload['broken']}",
        )

    async def operation():
        return await _loop(allow_warning=True).run(producer, evaluator)

    with pytest.raises(AgentLoopFailure):
        asyncio.run(recorder.step("screenplay", operation, contract_key="screenplay"))
    # 带 blocker 的 warning candidate 已被拒绝；仍应留下候选 Artifact 供审计
    artifacts = db.rows_to_dicts(db.get_conn().execute(
        "SELECT * FROM artifacts WHERE scope_id='e1' AND type='episode_screenplay' "
        "ORDER BY version"
    ).fetchall())
    assert len(artifacts) >= 1
    assert artifacts[0]["trust_level"] == "T1"


def test_storyboard_plural_shots_gets_targeted_repair_and_singular_contract(
    monkeypatch,
) -> None:
    shot = {
        "shot_no": 1,
        "duration_s": 5,
        "shot_size": "中景",
        "camera_move": "固定",
        "scene_setting": "日，庭院",
        "characters": ["萧炎"],
        "action_desc": "萧炎站在庭院中央缓缓握紧拳头。",
        "first_frame_desc": "萧炎站在庭院中央，双手自然垂落。",
        "last_frame_desc": "同一机位，萧炎握紧拳头，目光变得坚定。",
        "source_excerpt": "萧炎站在庭院里，沉默地握紧了自己的拳头。",
        "narration": "",
        "dialogues": [],
        "transition": "硬切",
        "continuity_from_prev": False,
    }
    outputs = [
        json.dumps(
            {"episode_no": 1, "is_final": False, "shots": [shot, {**shot, "shot_no": 2}]},
            ensure_ascii=False,
        ),
        json.dumps(
            {"episode_no": 1, "is_final": False, "shot": shot},
            ensure_ascii=False,
        ),
    ]
    prompts: list[str] = []

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages[-1]["content"])
        return outputs[len(prompts) - 1]

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    loop = AgentLoop(
        stage_key="storyboard_shot_1",
        contract_key="storyboard",
        goal="one shot",
        scope_type="storyboard_checkpoint",
        scope_id="e1:1",
        artifact_type="storyboard_shot",
    )
    result = asyncio.run(stages._run_with_agent_loop(
        "分镜脚本",
        "storyboard",
        "只生成第一镜",
        StoryboardShotDraft,
        lambda _draft: [],
        loop=loop,
        repair_output_contract="根对象只能包含单数 shot；禁止 shots 数组。",
        prefill={"episode_no": 1},
    ))

    assert result.shot.shot_no == 1
    assert len(prompts) == 2
    assert "逐镜合同只允许单数 shot 对象" in prompts[1]
    assert prompts[1].endswith("根对象只能包含单数 shot；禁止 shots 数组。")


def test_repair_loop_can_retain_complete_task_and_candidate(monkeypatch) -> None:
    """长剧本修复不得只看到头 3000/6000 字而丢失后半段台词。"""
    task_sentinel = "SOURCE_MAIN_DIALOGUE_AT_TASK_TAIL"
    candidate_sentinel = "SCRIPT_MAIN_DIALOGUE_AT_CANDIDATE_TAIL"
    user_prompt = "任务开头" + ("源" * 7000) + task_sentinel
    first = json.dumps(
        {"value": 1, "padding": "稿" * 7000, "sentinel": candidate_sentinel},
        ensure_ascii=False,
    )
    outputs = [first, '{"value": 2}']
    prompts: list[str] = []

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages[-1]["content"])
        return outputs[len(prompts) - 1]

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    result = asyncio.run(stages._run_with_agent_loop(
        "可拍剧本",
        "screenplay",
        user_prompt,
        Candidate,
        lambda candidate: [] if candidate.value >= 2 else ["主线台词缺失"],
        loop=_loop(max_iterations=2),
        repair_user_prompt_limit=None,
        repair_candidate_limit=None,
    ))

    assert result.value == 2
    assert task_sentinel in prompts[1]
    assert candidate_sentinel in prompts[1]


def test_storyboard_loop_exit_message_reflects_actual_reason() -> None:
    assert api._storyboard_loop_exit_text("max_iterations") == "已达到重试上限"
    assert "无质量提升" in api._storyboard_loop_exit_text("no_quality_gain")
    assert "相同问题" in api._storyboard_loop_exit_text("stalled")


def test_context_pack_records_hash_and_truncation_without_hiding_it() -> None:
    pack = ContextPack(goal="screenplay")
    selected = pack.add_text(
        "source_text",
        "abcdefghij",
        limit=4,
        source_artifact_id="art_source",
        truncation_strategy="head",
    )

    manifest = pack.manifest()

    assert selected == "abcd"
    assert manifest["items"][0]["source_artifact_id"] == "art_source"
    assert manifest["items"][0]["original_chars"] == 10
    assert manifest["items"][0]["selected_chars"] == 4
    assert manifest["items"][0]["truncated"] is True
    assert len(manifest["items"][0]["content_hash"]) == 64


def test_screenplay_task_keeps_repairing_without_publishing_warning(
    tmp_path, monkeypatch
) -> None:
    """普通 QA blocker 不得以 warning 可交付终态落库；应保持 repairing / 工作副本。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-warning.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) "
        "VALUES('p1','P','planned',NULL,1)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content) "
        "VALUES('p1',1,'Chapter','source text')"
    )
    conn.execute(
        """INSERT INTO episodes(
            id, project_id, episode_no, title, hook, cliffhanger, synopsis,
            source_chapters, target_duration_s, screenplay_status, status, created_at
        ) VALUES('e1','p1',1,'Episode','','','', '[1]', 50, 'running', 'planned', 1)"""
    )
    conn.commit()
    candidate = EpisodeScreenplay(
        episode_no=1,
        full_script_text="working repair candidate",
        stakes="失败失去资格",
        dramatic_question="能否赢？",
        protagonist_goal="赢",
        obstacle="阻力",
    )

    async def fake_production(**_kwargs):
        conn.execute(
            "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, "
            "screenplay_updated_at=? WHERE id=?",
            ("自动修复中：剩余 blocker", db.now(), "e1"),
        )
        conn.commit()
        return candidate

    monkeypatch.setattr(
        "app.production.screenplay_repair.run_screenplay_production",
        fake_production,
    )

    result = asyncio.run(api._screenplay_task("e1"))

    row = conn.execute(
        "SELECT screenplay_status, screenplay_error, screenplay_json "
        "FROM episodes WHERE id='e1'"
    ).fetchone()
    assert result is candidate
    assert row["screenplay_status"] == "repairing"
    assert row["screenplay_status"] != "warning"
    assert row["screenplay_status"] != "ready"
    # 未发布：页面交付位不应被未认证候选覆盖为 ready 文本
    assert "自动修复" in (row["screenplay_error"] or "")
