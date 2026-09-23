"""改编强度档位留档：只读『当前生效的短剧节奏删减』，供覆盖门禁豁免与分镜台
『本集删减』面板复用同一份数据。

冻结契约（生成侧由另一代理落地，与 shots/``storyboard_pack_dialogue_ledger``
产物同一事务写入）：每次整集分镜生成都落一条 ``type="storyboard_pack_adaptation"``、
``scope_type="episode"``、``scope_id=<episode_id>`` 的 EvidenceArtifact，
``status="validated"``，content 形状见 ``_valid_adaptation_content``。本模块
只读它，不产出、不修改。

判定规则（fail closed，宁可多判缺口/少给豁免，不许少判/多给）：
- 取该集这个 artifact 类型 **version 最高的一条，不论状态**——这是"当前"的
  唯一定义，与 ``app.evidence.repository.latest_artifact`` 默认跳过 stale 行
  不同：若采用它的默认行为，最新一条被判 stale 后会返回更早一代仍是
  validated 的短剧留档，把"已作废的删减"当成"当前删减"喂给覆盖门禁，等于
  放行了一批本该拦的缺口（真实事故模式见 CLAUDE.md「退场时漏恢复半边状态」
  同类教训）。
- 拿到的这条必须 ``status == "validated"`` 且 content 形状合法（见
  ``_valid_adaptation_content``），否则一律按"无留档"处理——即当作忠实档：
  不给覆盖门禁任何豁免区间，也不给确认预览任何删减说明。

``conn`` 必传（CLAUDE.md「所有权必须显式」）：调用方决定用哪个连接/事务，
不落到 ``app.db.get_conn()`` 的隐式默认值，测试也因此能直接喂内存 sqlite，
不需要 monkeypatch 任何东西。
"""
from __future__ import annotations

import json

from app.db import get_conn
from app.domain.common import _episode_or_404, router
from app.project_settings import ADAPTATION_MODES

#: 与冻结契约同名，只在本模块内部使用，不对外导出——外部消费方应该通过
#: ``current_storyboard_adaptation``/``storyboard_adaptation_summary`` 拿数据，
#: 不应该自己拼 artifact type 字符串。
_ADAPTATION_ARTIFACT_TYPE = "storyboard_pack_adaptation"
_DIALOGUE_LEDGER_ARTIFACT_TYPE = "storyboard_pack_dialogue_ledger"


def _latest_validated_artifact_content(conn, artifact_type: str, episode_id: str) -> dict | None:
    """该集这个类型 version 最高的一条（不论状态）；只有它自己 validated 才
    返回 content，否则返回 ``None``——不退回更早一代（见模块 docstring）。"""
    row = conn.execute(
        """SELECT status, content_json FROM artifacts
            WHERE type=? AND scope_type='episode' AND scope_id=?
            ORDER BY version DESC LIMIT 1""",
        (artifact_type, episode_id),
    ).fetchone()
    if row is None or row["status"] != "validated":
        return None
    try:
        content = json.loads(row["content_json"] or "null")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return content if isinstance(content, dict) else None


def _valid_adaptation_content(content: dict) -> bool:
    """冻结契约的最小形状核验：档位合法、``dropped_source_spans`` 是列表。
    不逐字段深校验每条 span——覆盖门禁自己会对越界偏移 fail closed（见
    ``source_coverage._declared_drop_regions``），这里只挡明显不成形的记录。"""
    if content.get("adaptation_mode") not in ADAPTATION_MODES:
        return False
    return isinstance(content.get("dropped_source_spans"), list)


def current_storyboard_adaptation(conn, episode_id: str) -> dict | None:
    """本集当前生效的改编留档；无留档/非法/未生效一律返回 ``None``（按忠实档
    处理）。调用方：覆盖门禁（豁免声明删减区间）、确认预览（非阻断提示）、
    本模块的 REST 接口（『本集删减』面板）。"""
    content = _latest_validated_artifact_content(conn, _ADAPTATION_ARTIFACT_TYPE, episode_id)
    if content is None or not _valid_adaptation_content(content):
        return None
    return content


def _dropped_dialogue_lines(conn, episode_id: str) -> list[dict]:
    """同一规则取该集当前生效的对白台账，抽出弃置台词。台账与改编留档是否
    存在彼此独立——忠实档/无改编留档的分集也可能有台账（弃置率恒低但字段
    仍在），『本集删减』面板要把这份此前写了没人读的数据接活（见
    ``app.production.storyboard_dialogue_ledger.dialogue_ledger_summary``：
    每条弃置台词目前只有 ``quote_id``/``reason``/``text`` 三个字段，没有
    说话人——那是产出侧的既有形状，本模块只读不改）。"""
    content = _latest_validated_artifact_content(conn, _DIALOGUE_LEDGER_ARTIFACT_TYPE, episode_id)
    if content is None:
        return []
    dropped = content.get("dropped_lines")
    return dropped if isinstance(dropped, list) else []


def storyboard_adaptation_summary(conn, episode_id: str) -> dict:
    """『本集删减』面板的只读汇总。没有改编留档的老分集仍照常返回台账里的
    弃置台词（不因为没有改编记录就连同台账一起藏起来）。"""
    adaptation = current_storyboard_adaptation(conn, episode_id)
    dropped_lines = _dropped_dialogue_lines(conn, episode_id)
    if adaptation is None:
        return {
            "recorded": False,
            "adaptation_mode": "faithful",
            "target_duration_s": None,
            "segment_count": None,
            "over_target": False,
            "dropped_source_spans": [],
            "dropped_lines": dropped_lines,
        }
    return {
        "recorded": True,
        "adaptation_mode": adaptation.get("adaptation_mode"),
        "target_duration_s": adaptation.get("target_duration_s"),
        "segment_count": adaptation.get("segment_count"),
        "over_target": bool(adaptation.get("over_target", False)),
        "dropped_source_spans": adaptation.get("dropped_source_spans") or [],
        "dropped_lines": dropped_lines,
    }


@router.get("/episodes/{episode_id}/storyboard-adaptation")
def get_storyboard_adaptation(episode_id: str):
    """分镜台『本集删减』面板：短剧节奏档声明的原文删减区间 + 台账里的弃置
    台词。老分集/忠实档没有改编留档时 ``recorded=false``，台账若有仍照常给。"""
    _episode_or_404(episode_id)
    return storyboard_adaptation_summary(get_conn(), episode_id)
