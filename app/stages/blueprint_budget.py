"""叙事蓝图分片重试预算——_BlueprintGenerationBudget 调用/token/耗时三道断路器。"""
from __future__ import annotations

import json
import time
from typing import Any


from app.db import get_conn

from .common import StageError
from .constants import (
    BLUEPRINT_GENERATION_MAX_OUTPUT_TOKENS,
    BLUEPRINT_GENERATION_MAX_PROVIDER_CALLS,
    BLUEPRINT_GENERATION_MAX_WALL_SECONDS,
    BLUEPRINT_LEAF_CALL_HEADROOM,
    BLUEPRINT_LEAF_PROVIDER_CALLS,
    BLUEPRINT_SHARD_MIN_TOKENS,
)


# Deleting the screenplay is the terminal disposition of everything that
# production spent: the same command supersedes the active revision, so a
# retry grant -- which may only bind to an active revision -- can never be
# issued for a receipt that outlives it.  Marking the abandoned calls keeps
# their cost auditable while closing the liability the deleted product owned.
BLUEPRINT_CALL_ABANDONED_BY_DELETE = "ABANDONED_BY_SCREENPLAY_DELETE"


class _BlueprintGenerationBudget:
    """Reserve call exposure, then settle against provider-reported usage.

    A requested ``max_tokens`` value is only an upper bound for one active
    call.  Charging every historical request at that upper bound rejects a
    later retry even when earlier calls used a small fraction of their
    reservation.  Unknown outcomes remain conservatively charged at the full
    reservation so the cost cap never fails open.
    """

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.provider_calls = 0
        self.requested_output_tokens = 0
        self.actual_output_tokens = 0
        self.unknown_output_tokens = 0
        self._next_reservation_id = 1
        self._reservations: dict[int, dict[str, Any]] = {}
        self._durable_successful_operations: set[str] = set()
        self._durable_unknown_operations: dict[str, str] = {}
        self._durable_unknown_stage_calls: dict[str, tuple[int, str]] = {}
        self._durable_unknown_receipts: list[dict[str, Any]] = []
        self._explicit_retry_authorized = False
        self.retry_grant_id = ""
        self.planned_leaf_count = 0

    def adopt_shard_plan(self, planned_leaf_count: int) -> None:
        """Raise the runaway breakers to the size of the deterministic plan.

        The leaf plan is derived locally from the frozen source cover (and from
        already-validated cached leaves), never from model output, so it cannot
        be inflated by a provider response.  It only ever grows -- a dynamic
        split adds leaves mid-run -- and the caps grow with it, so a cap can
        never shrink below exposure that was already admitted.
        """
        count = max(0, int(planned_leaf_count))
        if count > self.planned_leaf_count:
            self.planned_leaf_count = count

    @property
    def max_provider_calls(self) -> int:
        if not self.planned_leaf_count:
            return BLUEPRINT_GENERATION_MAX_PROVIDER_CALLS
        return max(
            BLUEPRINT_GENERATION_MAX_PROVIDER_CALLS,
            self.planned_leaf_count * BLUEPRINT_LEAF_PROVIDER_CALLS
            + BLUEPRINT_LEAF_CALL_HEADROOM,
        )

    @property
    def plan_scale(self) -> int:
        """How many floor-sized activations the leaf plan justifies.

        Token and wall-clock exposure track the admissible call count, so all
        three breakers keep the calibration they were chosen with instead of
        one of them firing first purely because the episode is long.
        """
        floor = max(1, BLUEPRINT_GENERATION_MAX_PROVIDER_CALLS)
        return max(1, -(-self.max_provider_calls // floor))

    @property
    def max_output_tokens(self) -> int:
        return BLUEPRINT_GENERATION_MAX_OUTPUT_TOKENS * self.plan_scale

    @property
    def max_wall_seconds(self) -> float:
        return BLUEPRINT_GENERATION_MAX_WALL_SECONDS * self.plan_scale

    def remaining_seconds(self) -> float:
        """Wall clock left for one provider call in this activation."""
        return self.max_wall_seconds - (
            time.monotonic() - self.started_at
        )

    @classmethod
    def from_durable_calls(
        cls,
        *,
        run_id: str | None,
        started_at_epoch: float | None = None,
        episode_id: str = "",
        input_fingerprint: str = "",
        retry_grant_id: str = "",
        include_resolved_by_call_id: int | None = None,
    ) -> "_BlueprintGenerationBudget":
        budget = cls()
        if started_at_epoch is not None:
            elapsed = max(0.0, time.time() - float(started_at_epoch))
            budget.started_at = time.monotonic() - elapsed
        budget.retry_grant_id = str(retry_grant_id or "")
        if not run_id and not (episode_id and input_fingerprint):
            return budget
        query = (
            "SELECT pc.id,pc.response_json,pc.meta,pc.status,"
            "pc.recovery_disposition,pc.operation_id,pc.ts,"
            "pc.superseded_by_call_id,pc.run_id,pc.production_grant_id,"
            "pc.request_hash "
            "FROM provider_calls pc "
            "LEFT JOIN workflow_runs wr ON wr.id=pc.run_id "
            "WHERE pc.kind='chat' "
            "AND json_extract(meta,'$.stage_key') IN "
            "('screenplay_blueprint_shard','screenplay_blueprint_patch',"
            "'screenplay_blueprint_review') "
            "AND pc.kind != 'provider_cache_hit'"
        )
        params: tuple[Any, ...]
        if episode_id and input_fingerprint:
            query += (
                " AND ("
                "      (wr.scope_type='episode' AND wr.scope_id=?)"
                "      OR json_extract(pc.meta,'$.episode_id')=?"
                " )"
                " AND wr.input_fingerprint=?"
            )
            params = (episode_id, episode_id, input_fingerprint)
        else:
            query += " AND pc.run_id=?"
            params = (run_id,)
        query += " ORDER BY pc.id"
        rows = get_conn().execute(query, params).fetchall()
        latest_operation_status: dict[str, tuple[str, str]] = {}
        latest_stage_status: dict[str, tuple[int, str, str]] = {}
        for row in rows:
            try:
                meta = json.loads(row["meta"] or "{}")
                response = json.loads(row["response_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                meta, response = {}, {}
            try:
                stored_operation_id = row["operation_id"]
            except (IndexError, KeyError):
                stored_operation_id = None
            try:
                durable_call_at = float(row["ts"])
            except (IndexError, KeyError, TypeError, ValueError):
                durable_call_at = 0.0
            try:
                durable_run_id = str(row["run_id"] or "")
            except (IndexError, KeyError):
                durable_run_id = str(run_id or "")
            is_current_activation = bool(
                run_id and durable_run_id == str(run_id)
            )
            try:
                durable_grant_id = str(row["production_grant_id"] or "")
            except (IndexError, KeyError):
                durable_grant_id = ""
            durable_grant_id = durable_grant_id or str(
                meta.get("production_grant_id") or ""
            )
            try:
                superseded_by_call_id = row["superseded_by_call_id"]
            except (KeyError, IndexError):
                superseded_by_call_id = None
            resolved_by_expected = bool(
                include_resolved_by_call_id
                and int(superseded_by_call_id or 0)
                == int(include_resolved_by_call_id)
            )
            try:
                durable_disposition = str(row["recovery_disposition"] or "")
            except (KeyError, IndexError):
                durable_disposition = ""
            abandoned_by_delete = (
                durable_disposition == BLUEPRINT_CALL_ABANDONED_BY_DELETE
            )
            unresolved_liability = (
                not superseded_by_call_id or resolved_by_expected
            ) and not abandoned_by_delete
            if (
                not durable_grant_id
                and episode_id
                and durable_call_at > 0
                and (not superseded_by_call_id or resolved_by_expected)
            ):
                # One narrow migration bridge for pre-column calls: a run's
                # BASELINE_GENERATION_STARTED event names the exact revision.
                # Bind only if that revision had exactly one grant at call
                # time; ambiguous grant histories remain unresolved/fail-safe.
                try:
                    legacy_grants = get_conn().execute(
                        """SELECT g.id
                             FROM run_events e
                             JOIN production_grants g
                               ON g.production_revision_id=json_extract(
                                  e.payload_json,'$.revision_id')
                            WHERE e.run_id=?
                              AND e.event_type='BASELINE_GENERATION_STARTED'
                              AND g.episode_id=? AND g.issued_at<=?
                            ORDER BY g.issued_at""",
                        (durable_run_id, episode_id, durable_call_at),
                    ).fetchall()
                    if len(legacy_grants) == 1:
                        durable_grant_id = str(legacy_grants[0]["id"] or "")
                except Exception:  # noqa: BLE001 - legacy/mocked schemas
                    durable_grant_id = ""
            operation_id = str(
                stored_operation_id or meta.get("operation_id") or ""
            ).strip()
            requested = max(1, int(meta.get("requested_max_tokens") or 1))
            effective = max(
                1,
                int(meta.get("effective_max_tokens") or requested),
            )
            status = str(row["status"] or "").upper()
            stage_key = str(meta.get("stage_key") or "")
            if (
                stage_key
                and status in {"INTERRUPTED", "RUNNING"}
                and unresolved_liability
            ):
                try:
                    durable_call_id = int(row["id"])
                except (KeyError, TypeError, ValueError):
                    durable_call_id = 0
                latest_stage_status[stage_key] = (
                    durable_call_id,
                    status,
                    durable_grant_id,
                )
                try:
                    durable_request_hash = str(row["request_hash"] or "")
                except (IndexError, KeyError):
                    durable_request_hash = ""
                budget._durable_unknown_receipts.append({
                    "call_id": durable_call_id,
                    "stage_key": stage_key,
                    "operation_id": operation_id,
                    "request_hash": durable_request_hash,
                    "effective_max_tokens": effective,
                    "prior_grant_id": durable_grant_id,
                })
            if operation_id and not abandoned_by_delete:
                # A settled-abandoned call is not "the previous attempt of this
                # semantic operation" either: leaving it here made ``claim``
                # demand a Production Grant for an operation whose liability
                # had already been closed, so a cached shard replay after a
                # delete was blocked by a call nobody can authorize any more.
                latest_operation_status[operation_id] = (
                    status,
                    durable_grant_id,
                )
            if status not in {"OK", "SUCCESS", "SUCCEEDED"}:
                disposition = str(row["recovery_disposition"] or "").lower()
                delivery_state = str(meta.get("delivery_state") or "").lower()
                if (
                    disposition not in {"not_sent", "definitely_not_sent"}
                    and delivery_state not in {"not_sent", "definitely_not_sent"}
                    and unresolved_liability
                ):
                    # A fresh retry activation inherits unresolved paid/unknown
                    # liability, but not the elapsed wall clock of a dead
                    # activation.  Current-activation calls and unresolved
                    # historical calls both consume its call/token caps.
                    budget.requested_output_tokens += requested
                    budget.provider_calls += 1
                    budget.unknown_output_tokens += effective
                continue
            # Historical successful operations remain available for strict
            # cache replay, but their already-settled cost/call count does not
            # consume the new logical retry activation's execution epoch.
            if not is_current_activation and episode_id and input_fingerprint:
                continue
            budget.requested_output_tokens += requested
            budget.provider_calls += 1
            usage = response.get("usage") if isinstance(response, dict) else None
            actual = (
                usage.get("completion_tokens")
                if isinstance(usage, dict)
                else None
            )
            if isinstance(actual, int) and actual >= 0:
                budget.actual_output_tokens += actual
            else:
                budget.unknown_output_tokens += effective
        for operation_id, (status, prior_grant_id) in latest_operation_status.items():
            if status in {"OK", "SUCCESS", "SUCCEEDED"}:
                budget._durable_successful_operations.add(operation_id)
            elif status in {"INTERRUPTED", "RUNNING"}:
                budget._durable_unknown_operations[operation_id] = prior_grant_id
        for stage_key, (call_id, _status, prior_grant_id) in latest_stage_status.items():
            if call_id:
                budget._durable_unknown_stage_calls[stage_key] = (
                    call_id,
                    prior_grant_id,
                )
        return budget

    @property
    def requires_fresh_retry_grant(self) -> bool:
        """Whether unresolved provider outcomes require a new user grant."""
        if not self._durable_unknown_stage_calls:
            return False
        if self._explicit_retry_authorized and self.retry_grant_id:
            return False
        return True

    def authorize_unknown_retry(self, grant_id: str) -> None:
        """Bind the approval-minted grant to this exact projected receipt set."""
        if not grant_id or not self._durable_unknown_stage_calls:
            raise ValueError("unknown retry authorization requires receipts and grant")
        self.retry_grant_id = grant_id
        self._explicit_retry_authorized = True

    @property
    def unknown_receipts(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._durable_unknown_receipts]

    def assert_activation_admissible(
        self,
        *,
        minimum_output_tokens: int = BLUEPRINT_SHARD_MIN_TOKENS,
    ) -> None:
        """Read-only admission fence used before any character/provider call."""
        if self.requires_fresh_retry_grant:
            raise StageError(
                "剧本时空因果蓝图分片",
                [
                    "[BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED] "
                    "上次供应商结果未知；必须先签发新的 Production Grant"
                ],
            )
        elapsed = time.monotonic() - self.started_at
        if elapsed >= self.max_wall_seconds:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_GENERATION_TIME_BUDGET] 超过当前激活时间上限"],
            )
        if self.provider_calls >= self.max_provider_calls:
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_GENERATION_CALL_BUDGET] 超过全局调用上限"],
            )
        if (
            self.charged_output_tokens + int(minimum_output_tokens)
            > self.max_output_tokens
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_GENERATION_TOKEN_BUDGET] 剩余输出预算不足一次安全分片"],
            )

    def explicit_retry_call_id(self, stage_key: str) -> int | None:
        prior = self._durable_unknown_stage_calls.get(stage_key)
        if prior is None:
            return None
        call_id, prior_grant_id = prior
        # Gate on the SAME authorization state that ``claim`` checks
        # (``_explicit_retry_authorized``), not merely on ``retry_grant_id``
        # being present and distinct. ``retry_grant_id`` is populated from the
        # config snapshot in ``from_durable_calls`` even when Site B
        # authorization failed, so the weaker guard could hand back a prior
        # interrupted call id that ``claim`` would then refuse — the two gates
        # must not disagree.
        if not (
            self._explicit_retry_authorized
            and self.retry_grant_id
            and self.retry_grant_id != prior_grant_id
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                [
                    "[BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED] "
                    "上次供应商结果未知；缺少新的 Production Grant"
                ],
            )
        return call_id

    @property
    def reserved_output_tokens(self) -> int:
        return sum(
            int(item["max_tokens"])
            for item in self._reservations.values()
        )

    @property
    def charged_output_tokens(self) -> int:
        return self.actual_output_tokens + self.unknown_output_tokens

    def claim(
        self,
        *,
        max_tokens: int,
        requested_max_tokens: int | None = None,
        operation_id: str = "",
    ) -> int:
        durable_replay = bool(
            operation_id
            and operation_id in self._durable_successful_operations
        )
        prior_unknown_grant = self._durable_unknown_operations.get(operation_id)
        if prior_unknown_grant is not None and not durable_replay:
            if not self._explicit_retry_authorized:
                raise StageError(
                    "剧本时空因果蓝图分片",
                    [
                        "[BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED] "
                        "上次供应商结果未知；必须由新的 Production Grant "
                        "显式授权同一语义 operation 的下一 attempt"
                    ],
                )
        elapsed = time.monotonic() - self.started_at
        if (
            not durable_replay
            and elapsed >= self.max_wall_seconds
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_GENERATION_TIME_BUDGET] 超过全局时间上限"],
            )
        if (
            not durable_replay
            and self.provider_calls >= self.max_provider_calls
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_GENERATION_CALL_BUDGET] 超过全局调用上限"],
            )
        if (
            not durable_replay
            and (
                self.charged_output_tokens
                + self.reserved_output_tokens
                + max_tokens
                > self.max_output_tokens
            )
        ):
            raise StageError(
                "剧本时空因果蓝图分片",
                ["[BLUEPRINT_GENERATION_TOKEN_BUDGET] 超过全局输出 token 上限"],
            )
        reservation_id = self._next_reservation_id
        self._next_reservation_id += 1
        if not durable_replay:
            self.provider_calls += 1
            self.requested_output_tokens += int(
                requested_max_tokens or max_tokens
            )
        self._reservations[reservation_id] = {
            "max_tokens": int(max_tokens),
            "requested_max_tokens": int(
                requested_max_tokens or max_tokens
            ),
            "actual_tokens": 0,
            "fresh_responses": 0,
            "unknown_responses": 0,
            "reused_responses": 0,
            "durable_replay": durable_replay,
        }
        return reservation_id

    def record_usage(
        self,
        reservation_id: int,
        usage_event: dict[str, Any],
    ) -> None:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            return
        if usage_event.get("reused") is True:
            reservation["reused_responses"] += 1
            return
        reservation["fresh_responses"] += 1
        completion_tokens = usage_event.get("completion_tokens")
        if isinstance(completion_tokens, int) and completion_tokens >= 0:
            reservation["actual_tokens"] += completion_tokens
        else:
            reservation["unknown_responses"] += 1

    def settle(
        self,
        reservation_id: int,
        *,
        unreported_outcome: str = "unknown",
    ) -> dict[str, Any]:
        reservation = self._reservations.pop(reservation_id, None)
        if reservation is None:
            raise RuntimeError("蓝图输出 token 预留已结算或不存在")
        effective = int(reservation["max_tokens"])
        requested = int(reservation["requested_max_tokens"])
        durable_replay = bool(reservation["durable_replay"])
        fresh_responses = int(reservation["fresh_responses"])
        unknown_responses = int(reservation["unknown_responses"])
        actual = int(reservation["actual_tokens"])
        if fresh_responses == 0 and int(reservation["reused_responses"]) > 0:
            charged = 0
            actual_value: int | None = 0
        elif fresh_responses == 0 and unreported_outcome == "not_sent":
            charged = 0
            actual_value = 0
        elif unknown_responses == 0:
            if fresh_responses > 0:
                charged = actual
                actual_value = actual
                self.actual_output_tokens += actual
            else:
                charged = effective
                actual_value = None
                self.unknown_output_tokens += effective
        else:
            actual_value = None
            charged = actual + effective * unknown_responses
            self.actual_output_tokens += actual
            self.unknown_output_tokens += effective * unknown_responses
        return {
            "requested_max_tokens": requested,
            "effective_max_tokens": effective,
            "actual_completion_tokens": actual_value,
            "usage_reported": fresh_responses > 0 and unknown_responses == 0,
            "fresh_responses": fresh_responses,
            "reused_responses": int(reservation["reused_responses"]),
            "unknown_responses": unknown_responses,
            "durable_replay": durable_replay,
            "charged_output_tokens": charged,
            "global_charged_output_tokens": self.charged_output_tokens,
        }
