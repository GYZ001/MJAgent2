"""计划阶段的纯数据结构——不含任何数据库/文件 I/O，供 plan/report/apply 共用。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChapterRow:
    """只读快照：计划阶段读到的一条 ``chapters`` 行，之后用来做"写入前未被别
    人改过"的核验（见 ``apply._reverify``）。"""

    id: int
    idx: int
    title: str
    content: str


@dataclass
class ChapterWrite:
    """一条待写入的章节变更。``action`` 为 ``update``/``insert``/``delete``；
    ``id`` 对 ``update``/``delete`` 必填（保留原 ``chapters.id``，避免
    ``storyboard_source_bindings`` 的 ``ON DELETE CASCADE`` 被意外触发）。"""

    action: str
    idx: int
    id: int | None = None
    title: str = ""
    content: str = ""
    char_count: int = 0
    paratext_json: str | None = None


@dataclass
class EpisodeWrite:
    """一条待写入的分集变更，``action`` 为 ``update``/``insert``/``delete``。"""

    action: str
    episode_no: int
    episode_id: str | None = None
    title: str = ""
    synopsis: str = ""


@dataclass
class ConservationResult:
    """一段正文守恒校验的结果（见 ``conservation.check_conservation``）。
    ``lost_chars`` 必须为 0 才能接受该段的重切结果——旧文本不可逆，任何
    丢字都必须拒绝；``gained_chars``（如补救分支补出的「章」字）允许但要
    计数进报告，不静默吞掉。"""

    lost_chars: int = 0
    gained_chars: int = 0
    lost_samples: list[str] = field(default_factory=list)


@dataclass
class SegmentPlan:
    """一个"非冻结章节最大连续段"的处理结果。"""

    start_idx: int
    end_idx: int
    is_tail: bool
    starts_at_beginning: bool
    accepted: bool
    reason: str
    old_chapters: list[ChapterRow] = field(default_factory=list)
    new_chapters: list[dict] = field(default_factory=list)
    excluded_prefix: str = ""
    gained_chars: int = 0
    lost_samples: list[str] = field(default_factory=list)


@dataclass
class ProjectPlan:
    """一个项目的完整只读计划；``changed`` 为假时 apply 应当是纯粹的空操作。"""

    project_id: str
    ok: bool
    reject_reason: str
    frozen_episode_ids: set[str] = field(default_factory=set)
    frozen_idx: set[int] = field(default_factory=set)
    segments: list[SegmentPlan] = field(default_factory=list)
    chapter_writes: list[ChapterWrite] = field(default_factory=list)
    episode_writes: list[EpisodeWrite] = field(default_factory=list)
    unedited_episode_skips: list[str] = field(default_factory=list)
    bible_reference_report: dict = field(default_factory=dict)
    series_task_gap_report: str = ""
    old_chapter_count: int = 0
    new_chapter_count: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.chapter_writes or self.episode_writes)

    @property
    def rejected_segment_count(self) -> int:
        """被拒段数——``changed`` 为假不代表"新旧切章结果一致"：段也可能是
        因为丢字/序号不稳定被拒、保持原样，报告措辞必须把这两种情况分开说。"""
        return sum(1 for segment in self.segments if not segment.accepted)
