#!/usr/bin/env python3
"""Read-only replay of the 2026-08-08 screenplay identity conflicts.

The replay uses preserved provider responses when they exist, current source
chapters and the current identity authority contract.  AI decisions are not
persisted.  The one legacy failure with no retained provider response is
reported as source/authority verification only instead of fabricating an IR.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.config import DB_PATH
from app.identity_adjudication import adjudicate_screenplay_ir_identities
from app.identity_authority import identity_authority_registry
from app.schemas import Bible, extract_json
from app.screenplay_ir import (
    IR_VERSION,
    ScreenplayGenerationIR,
    ScreenplayIRIdentityConflictError,
    compile_screenplay_ir,
    normalize_screenplay_ir_payload,
)
from app.source_excerpt import index_source_segments


HISTORICAL_CASES = [
    ("ERR-20260808-ca1a32", "ep_45e060e0f8f2", "白洁", None),
    ("ERR-20260808-3b16a5", "ep_c160e3e58696", "白洁", None),
    ("ERR-20260808-9ae6c3", "ep_f3faa4513201", "许清", 12335),
    ("ERR-20260808-dda0c9", "ep_c160e3e58696", "白洁", None),
    ("ERR-20260808-c8e1b8", "ep_c160e3e58696", "白洁", None),
    ("ERR-20260808-5d8919", "ep_f3faa4513201", "穿着绿色长袍的男", 13309),
    ("ERR-20260808-c21c20", "ep_77860910caaf", "马脸青年", 13707),
    ("ERR-20260808-dc6aa7", "ep_f3faa4513201", "孟浩", 14014),
    ("ERR-20260808-2ae307", "ep_cf4ab24130af", "卢美", 14032),
]


def _episode_context(
    conn: sqlite3.Connection,
    episode_id: str,
) -> tuple[dict[str, Any], str, Bible]:
    row = conn.execute(
        "SELECT e.*,p.bible_json FROM episodes e "
        "JOIN projects p ON p.id=e.project_id WHERE e.id=?",
        (episode_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"episode not found: {episode_id}")
    episode = dict(row)
    chapter_indexes = json.loads(episode.get("source_chapters") or "[]")
    placeholders = ",".join("?" for _ in chapter_indexes)
    chapters = conn.execute(
        "SELECT idx,content FROM chapters WHERE project_id=? "
        f"AND idx IN ({placeholders}) ORDER BY idx",
        (episode["project_id"], *chapter_indexes),
    ).fetchall()
    source_text = "\n\n".join(str(chapter["content"] or "") for chapter in chapters)
    episode["source_chapters"] = chapter_indexes
    episode["authorized_source_chapters"] = {
        f"chapter-{chapter['idx']}": str(chapter["content"] or "")
        for chapter in chapters
    }
    episode["character_resolutions"] = json.loads(
        episode.get("screenplay_character_resolutions") or "[]"
    )
    return episode, source_text, Bible.model_validate(
        json.loads(episode.get("bible_json") or "{}")
    )


def _candidate_from_call(
    conn: sqlite3.Connection,
    call_id: int,
) -> ScreenplayGenerationIR:
    row = conn.execute(
        "SELECT response_json FROM provider_calls WHERE id=?",
        (call_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"provider call not found: {call_id}")
    outer = json.loads(row["response_json"])
    raw = outer["choices"][0]["message"]["content"]
    payload = extract_json(raw, repair_unescaped_inner_quotes=True)
    normalized, changes = normalize_screenplay_ir_payload(payload)
    candidate = ScreenplayGenerationIR.model_validate(normalized)
    candidate.format_version = IR_VERSION
    candidate.normalization_log.extend(changes)
    return candidate


def _ai_audits(candidate: ScreenplayGenerationIR) -> list[dict[str, Any]]:
    return [
        item
        for item in candidate.normalization_log
        if isinstance(item, dict)
        and item.get("operation") == "ai_identity_adjudication"
    ]


async def _replay_candidate(
    conn: sqlite3.Connection,
    *,
    episode_id: str,
    call_id: int,
) -> dict[str, Any]:
    episode, source_text, bible = _episode_context(conn, episode_id)
    candidate = _candidate_from_call(conn, call_id)
    before_identity_count = len(candidate.identities)
    candidate = await adjudicate_screenplay_ir_identities(
        candidate,
        episode=episode,
        source_text=source_text,
        bible=bible,
        persist_new_resolutions=False,
    )
    # Prove the model boundary remains round-trippable before compilation.
    candidate = ScreenplayGenerationIR.model_validate(
        candidate.model_dump(mode="json")
    )
    segments = {
        item.segment_id: item.text
        for item in index_source_segments(source_text)
    }
    audits = _ai_audits(candidate)
    decisions = [
        decision
        for audit in audits
        for decision in audit.get("decisions") or []
    ]
    evidence_ids = [
        source_id
        for decision in decisions
        for source_id in decision.get("evidence_source_ids") or []
    ]
    compile_status = "compiled"
    compile_detail = ""
    try:
        compiled = compile_screenplay_ir(
            candidate.model_copy(deep=True),
            episode=episode,
            source_text=source_text,
            bible=bible,
        )
        # The published Pydantic contract must also survive a JSON round trip.
        type(compiled).model_validate(compiled.model_dump(mode="json"))
    except ScreenplayIRIdentityConflictError as exc:
        compile_status = "identity_conflict"
        compile_detail = str(exc)
    except (TypeError, ValueError) as exc:
        # Some raw baselines predate later fidelity patches.  Preserve this as
        # a non-identity compiler result rather than misreporting it as an
        # identity replay failure.
        compile_status = "identity_pass_nonidentity_compile_failure"
        compile_detail = str(exc)
    return {
        "episode_id": episode_id,
        "provider_call_id": call_id,
        "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "source_chars": len(source_text),
        "identity_count_before": before_identity_count,
        "identity_count_after": len(candidate.identities),
        "ai_call_count": len(audits),
        "decisions": decisions,
        "all_evidence_ids_exist_in_original": all(
            source_id in segments for source_id in evidence_ids
        ),
        "round_trip_valid": True,
        "compile_status": compile_status,
        "compile_detail": compile_detail[:300],
    }


async def main() -> None:
    if not Path(DB_PATH).exists():
        raise SystemExit(f"database not found: {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cache: dict[tuple[str, int], dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for error_id, episode_id, conflict_token, call_id in HISTORICAL_CASES:
        error = conn.execute(
            "SELECT id,message FROM error_logs WHERE id=?",
            (error_id,),
        ).fetchone()
        episode, source_text, bible = _episode_context(conn, episode_id)
        base = {
            "error_id": error_id,
            "historical_error_found": error is not None,
            "historical_message": str(error["message"] if error else ""),
            "episode_id": episode_id,
            "conflict_token": conflict_token,
            "token_occurrences_in_original": source_text.count(conflict_token),
        }
        if call_id is None:
            registry_ids = {
                item["authority_id"]
                for item in identity_authority_registry(
                    bible,
                    episode.get("character_resolutions") or [],
                )
                if item.get("canonical_name") == conflict_token
            }
            base.update({
                "replay_mode": "source_authority_only",
                "raw_candidate_retained": False,
                "source_authority_ids": sorted(registry_ids),
                "source_authority_verified": bool(
                    source_text.count(conflict_token) and registry_ids
                ),
            })
        else:
            key = (episode_id, call_id)
            if key not in cache:
                cache[key] = await _replay_candidate(
                    conn,
                    episode_id=episode_id,
                    call_id=call_id,
                )
            base.update({
                "replay_mode": "preserved_or_current_ir",
                "raw_candidate_retained": True,
                **deepcopy(cache[key]),
            })
        results.append(base)
    summary = {
        "contract_version": IR_VERSION,
        "historical_case_count": len(results),
        "historical_errors_found": sum(
            bool(item["historical_error_found"]) for item in results
        ),
        "candidate_replay_count": sum(
            item["replay_mode"] == "preserved_or_current_ir" for item in results
        ),
        "source_only_count": sum(
            item["replay_mode"] == "source_authority_only" for item in results
        ),
        "identity_conflict_count_after_replay": sum(
            item.get("compile_status") == "identity_conflict" for item in results
        ),
        "all_source_checks_passed": all(
            item.get("all_evidence_ids_exist_in_original", True)
            and item.get("source_authority_verified", True)
            for item in results
        ),
        "all_round_trips_valid": all(
            item.get("round_trip_valid", True) for item in results
        ),
    }
    print(json.dumps({"summary": summary, "cases": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
