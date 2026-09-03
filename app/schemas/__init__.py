"""LLM 输出合同（PRD 原则 P5：一切 LLM 输出有 Schema）。对应 docs/PROMPT_SPEC.md。

原 app/schemas.py（2571 行 / 97 个顶层定义）按领域拆分为本包下的多个模块：
共享常量与 ID 归一化（common）、人物谱（character）、世界观/场景图（world）、
剧本大纲台账（screenplay_outline）与单集剧本（screenplay）、统一叙事连续性
契约拆成 narrative_core/narrative_action/narrative_audience/narrative_capacity/
narrative_plan/narrative_boundary/narrative_review 七个子模块、分镜
（shot_state/shot/storyboard）、以及模型输出 JSON 提取与修复
（json_repair_strings/json_repair_structure/json_extract）。

本文件是唯一的稳定入口：全仓所有 `from app.schemas import X` / `import
app.schemas` / `app.schemas.X` 使用方式必须不经改动继续可用——下面按来源
模块显式再导出每一个公开符号（禁止 `from .x import *`，见
app/FILE_CONVENTIONS.toml 的 star_import 闸门）。新增契约请加进对应领域的
子模块，不要加回本文件。
"""
from __future__ import annotations

from .character import AppearanceEvidence as AppearanceEvidence
from .character import Character as Character
from .character import CharacterAffiliation as CharacterAffiliation
from .character import CharacterAlias as CharacterAlias
from .character import CharacterRelation as CharacterRelation
from .character import Relationship as Relationship
from .character import character_is_portrait_eligible as character_is_portrait_eligible
from .common import AUDIO_TIMELINE_TYPES as AUDIO_TIMELINE_TYPES
from .common import CAMERA_MOVES as CAMERA_MOVES
from .common import CONTINUITY_MODES as CONTINUITY_MODES
from .common import DELIVERY_OWNERS as DELIVERY_OWNERS
from .common import EMOTIONS as EMOTIONS
from .common import KEY_LINE_ID_RE as KEY_LINE_ID_RE
from .common import NARRATIVE_CONTRACT_VERSION as NARRATIVE_CONTRACT_VERSION
from .common import NARRATOR_LABEL as NARRATOR_LABEL
from .common import PROMPT_CONTRACT_VERSION as PROMPT_CONTRACT_VERSION
from .common import SHOT_FORMS as SHOT_FORMS
from .common import SHOT_SIZES as SHOT_SIZES
from .common import SPINE_BEAT_ID_RE as SPINE_BEAT_ID_RE
from .common import STORY_EVENT_ID_RE as STORY_EVENT_ID_RE
from .common import SYSTEM_ENVIRONMENT_ENTITY_PREFIX as SYSTEM_ENVIRONMENT_ENTITY_PREFIX
from .common import TRANSITIONS as TRANSITIONS
from .common import is_narrator_label as is_narrator_label
from .common import is_system_environment_entity_id as is_system_environment_entity_id
from .common import system_environment_entity_id as system_environment_entity_id
from .json_extract import extract_json as extract_json
from .json_extract import schema_errors as schema_errors
from .narrative_action import ActionAgency as ActionAgency
from .narrative_action import ActionParticipantDelivery as ActionParticipantDelivery
from .narrative_action import ActionSemanticRelationAudit as ActionSemanticRelationAudit
from .narrative_action import AtomicAction as AtomicAction
from .narrative_action import AtomicActionPhase as AtomicActionPhase
from .narrative_action import NarrativeEvent as NarrativeEvent
from .narrative_action import TextProvenance as TextProvenance
from .narrative_audience import AssimilationTask as AssimilationTask
from .narrative_audience import AudiencePath as AudiencePath
from .narrative_audience import AudiencePriorContract as AudiencePriorContract
from .narrative_audience import AudienceStateSnapshot as AudienceStateSnapshot
from .narrative_audience import BeliefItem as BeliefItem
from .narrative_audience import CharacterBeliefSnapshot as CharacterBeliefSnapshot
from .narrative_audience import CharacterDramaticState as CharacterDramaticState
from .narrative_audience import ExperienceIntent as ExperienceIntent
from .narrative_audience import TargetDelta as TargetDelta
from .narrative_audience import WithheldProposition as WithheldProposition
from .narrative_boundary import BoundaryStateTransition as BoundaryStateTransition
from .narrative_boundary import CognitiveBridgePlan as CognitiveBridgePlan
from .narrative_boundary import NarrativeBoundaryContract as NarrativeBoundaryContract
from .narrative_capacity import AudienceStatePathRef as AudienceStatePathRef
from .narrative_capacity import NarrativeArcContract as NarrativeArcContract
from .narrative_capacity import ReadabilityWindow as ReadabilityWindow
from .narrative_capacity import SceneDramaticContract as SceneDramaticContract
from .narrative_capacity import SetupPayoffContract as SetupPayoffContract
from .narrative_capacity import ShotCapacityBudget as ShotCapacityBudget
from .narrative_capacity import ShotContribution as ShotContribution
from .narrative_core import AdaptationDecision as AdaptationDecision
from .narrative_core import DramaticQuestion as DramaticQuestion
from .narrative_core import NarrativeAnchor as NarrativeAnchor
from .narrative_core import NarrativeEvidence as NarrativeEvidence
from .narrative_core import NarrativeProposition as NarrativeProposition
from .narrative_core import SourceEvidence as SourceEvidence
from .narrative_core import SourceSpan as SourceSpan
from .narrative_core import StateFact as StateFact
from .narrative_core import StateFactValue as StateFactValue
from .narrative_plan import IdentityContractEvidence as IdentityContractEvidence
from .narrative_plan import NarrativeContinuityPlan as NarrativeContinuityPlan
from .narrative_plan import NarrativeIdentityContract as NarrativeIdentityContract
from .narrative_review import BlindAudienceObservation as BlindAudienceObservation
from .narrative_review import BlindSpontaneousRecall as BlindSpontaneousRecall
from .narrative_review import NarrativeReviewReport as NarrativeReviewReport
from .narrative_review import TargetDeltaResult as TargetDeltaResult
from .screenplay import EpisodeScreenplay as EpisodeScreenplay
from .screenplay import PrepPackCharacterAsset as PrepPackCharacterAsset
from .screenplay import PrepPackSceneAsset as PrepPackSceneAsset
from .screenplay import normalize_screenplay_json_shape as normalize_screenplay_json_shape
from .screenplay_outline import InformationItem as InformationItem
from .screenplay_outline import KeyDialogueChain as KeyDialogueChain
from .screenplay_outline import KeyDialogueTurn as KeyDialogueTurn
from .screenplay_outline import PlotSpine as PlotSpine
from .screenplay_outline import PlotSpineBeat as PlotSpineBeat
from .screenplay_outline import ScriptScene as ScriptScene
from .screenplay_outline import SourceCoverageDecision as SourceCoverageDecision
from .screenplay_outline import StoryEvent as StoryEvent
from .screenplay_outline import VoiceCanonical as VoiceCanonical
from .shot import Shot as Shot
from .shot_montage import MontageBeat as MontageBeat
from .shot_state import AudioTimelineItem as AudioTimelineItem
from .shot_state import CharacterContinuityState as CharacterContinuityState
from .shot_state import ContinuityState as ContinuityState
from .shot_state import Dialogue as Dialogue
from .shot_state import PropContinuityState as PropContinuityState
from .shot_state import RequiredOnScreenText as RequiredOnScreenText
from .shot_state import SceneContinuityState as SceneContinuityState
from .storyboard import Storyboard as Storyboard
from .storyboard import StoryboardContextRequirement as StoryboardContextRequirement
from .storyboard import StoryboardOutline as StoryboardOutline
from .storyboard import StoryboardOutlineShot as StoryboardOutlineShot
from .storyboard import StoryboardSceneContext as StoryboardSceneContext
from .world import Bible as Bible
from .world import Scene as Scene
from .world import World as World

# Resolve the forward references used by Shot/StoryboardOutlineShot/
# StoryboardOutline without moving them next to NarrativeBoundaryContract /
# StoryboardSceneContext / CognitiveBridgePlan (many callers import the
# public classes by their current module path). This reproduces, across
# module boundaries, the exact deferred-resolution timing that the original
# single-file app/schemas.py used (Shot/StoryboardOutlineShot/StoryboardOutline
# were defined before NarrativeBoundaryContract/StoryboardSceneContext/
# CognitiveBridgePlan existed in the same module, so their forward-referenced
# fields stayed unresolved until three explicit ``model_rebuild()`` calls at
# the bottom of that file). ``_types_namespace`` is passed explicitly instead
# of relying on frame-walking so the resolution does not depend on where in
# this file the calls happen to sit.
_BOUNDARY_TYPES_NAMESPACE = {"NarrativeBoundaryContract": NarrativeBoundaryContract}
_OUTLINE_TYPES_NAMESPACE = {
    "StoryboardSceneContext": StoryboardSceneContext,
    "CognitiveBridgePlan": CognitiveBridgePlan,
}
Shot.model_rebuild(_types_namespace=_BOUNDARY_TYPES_NAMESPACE)
StoryboardOutlineShot.model_rebuild(_types_namespace=_BOUNDARY_TYPES_NAMESPACE)
StoryboardOutline.model_rebuild(_types_namespace=_OUTLINE_TYPES_NAMESPACE)
