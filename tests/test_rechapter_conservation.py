"""每段正文守恒校验（``app.domain.projects.rechapter.conservation``）与由它
触发的报告措辞——从 ``tests/test_rechapter_project.py`` 搬出来，纯粹是那个
文件快要碰到测试文件 500 行红线，不是职责拆分。

覆盖：重切丢字必拒、丢失片段进报告、apply 不写库；补救分支补出的字（新有
旧无）允许通过但要计数；有段被拒时报告不能说"新旧切章结果一致"。

丢字场景全部用替身制造，不依赖 ``app/ingest.py`` 当前是否真的会在"标题
紧挨、中间无正文"时丢字——那条路径正被另一个并发改动修掉（改成前一行并
入下一章开头而不是丢弃）。替身打在 ``segment`` 模块实际调用的绑定上，不
是 ``app.ingest`` 自己的属性——拆包后改源模块属性不影响已经 ``from ...
import ...`` 过去的绑定（CLAUDE.md「拆包会静默废掉 monkeypatch」），打在
``app.ingest`` 上这类测试会静默变绿但什么都没验证。
"""
from __future__ import annotations

from app.db import get_conn
from app.domain.projects.rechapter.apply import apply_project
from app.domain.projects.rechapter.conservation import check_conservation
from app.domain.projects.rechapter.plan import plan_project
from app.domain.projects.rechapter.report import format_report, plan_to_json

from tests.test_rechapter_project import _chapter_row, _freeze_via_artifact, _make_project, _pad

_LOST_MARKER = "丢失标记丢失标记丢失标记"


def _patch_lossy_resplit(monkeypatch) -> None:
    """让 ``segment._split_chapters_with_removed`` 这个绑定返回"比输入少了
    一段文字"的结果，模拟切章器实现本身出现数据丢失时的样子。"""
    from app.domain.projects.rechapter import segment as segment_module

    def fake_split(text: str):
        return [{
            "title": "第2章 续篇", "content": text.replace(_LOST_MARKER, ""), "paratext_json": None,
        }], []

    monkeypatch.setattr(segment_module, "_split_chapters_with_removed", fake_split)


def _lossy_chapters() -> list[tuple[str, str]]:
    return [
        ("第1章 开场", f"第1章 开场\n{_pad()}"),  # 冻结
        ("第2章 续篇", f"第2章 续篇\n{_pad()}{_LOST_MARKER}"),  # 尾段：替身会吞掉 _LOST_MARKER
    ]


def test_conservation_rejects_segment_that_drops_text(monkeypatch) -> None:
    """守恒校验必须独立于切章器实现本身发现丢字：原始上传不落盘，这种
    丢失不可逆，不能指望章数/序号对比侥幸发现。"""
    _patch_lossy_resplit(monkeypatch)
    conn = get_conn()
    project_id = _make_project(conn, _lossy_chapters())
    _freeze_via_artifact(conn, project_id, 1)
    before = _chapter_row(conn, project_id, 2)

    plan = plan_project(conn, project_id)
    assert plan.ok, plan.reject_reason
    tail = next(s for s in plan.segments if s.start_idx == 2)
    assert not tail.accepted
    assert "丢失" in tail.reason
    assert tail.lost_samples
    assert any(_LOST_MARKER in sample for sample in tail.lost_samples)
    assert not plan.changed

    result = apply_project(conn, project_id, plan)
    assert result["status"] == "unchanged"
    assert _chapter_row(conn, project_id, 2) == before


def test_check_conservation_passes_for_clean_resplit() -> None:
    """正常段：新旧文本去空白后逐字相同，守恒校验通过。"""
    original = "第1章 开场\n正文内容一二三四五六七八九十"
    new_chapters = [{"title": "第1章 开场", "content": "第1章 开场\n正文内容一二三四五六七八九十"}]
    result = check_conservation(original, new_chapters)
    assert result.lost_chars == 0
    assert not result.lost_samples


def test_check_conservation_allows_gained_chars_but_counts_them() -> None:
    """补救分支补出的字（如缺失的"章"）属于新有旧无，允许通过，但要计数，
    不能静默吞掉——报告里要能看到这件事发生过。"""
    original = "第五十三异变突生\n正文内容一二三四五六七八九十"
    new_chapters = [{
        "title": "第五十三章异变突生",
        "content": "第五十三章异变突生\n正文内容一二三四五六七八九十",
    }]
    result = check_conservation(original, new_chapters)
    assert result.lost_chars == 0
    assert result.gained_chars == 1


def test_report_does_not_claim_consistent_when_a_segment_is_rejected(monkeypatch) -> None:
    """复现沙箱里的假话：段被拒（守恒校验丢字）时整个项目的 chapter_writes/
    episode_writes 都是空的，``plan.changed`` 因此为假——但原因是"段被拒绝
    并保持原样"，不是"新旧切章结果一致"，报告措辞必须把这两种情况分开。
    丢字场景同样用替身制造（见上方 ``_patch_lossy_resplit``），不依赖
    ``app/ingest.py`` 当前行为。"""
    _patch_lossy_resplit(monkeypatch)
    conn = get_conn()
    project_id = _make_project(conn, _lossy_chapters())
    _freeze_via_artifact(conn, project_id, 1)

    plan = plan_project(conn, project_id)
    assert plan.rejected_segment_count == 1
    assert not plan.changed

    text = format_report(plan)
    assert "新旧切章结果一致" not in text
    assert "有 1 段被拒" in text

    payload = plan_to_json(plan)
    assert payload["rejected_segment_count"] == 1
