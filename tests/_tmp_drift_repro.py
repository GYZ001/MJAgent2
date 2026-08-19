from __future__ import annotations

import copy

from tests.test_screenplay_ir import _ir_payload, _bible, SOURCE
from app.screenplay_ir import ScreenplayGenerationIR, compile_screenplay_ir
from app.production.screenplay_document import (
    screenplay_to_document,
    document_to_screenplay,
)


DRIFT_SOURCE = "\n\n".join([
    "谷言独自在咖啡厅等待旧友。他看向门口说：“再等十分钟。”"
    "他又低声说：“他到底来不来。”中途还招手说：“服务员，再来一杯水。”",
    "旧友推门出现，把钥匙递给谷言说：“拿好这把钥匙。”",
    "门外响起更重的敲门声，旧友立刻说：“别开门。”危险已经逼近。",
])


def build_interleaved_payload() -> dict:
    payload = copy.deepcopy(_ir_payload())
    # Rebuild scene sc1 so two chains are interleaved in emission order.
    # Emission order:            A(chain wait) , B(chain aside) , C(chain wait)
    # Chain-grouped (structural): wait=[A,C], aside=[B] -> [A, C, B]
    # Story order (full_script_text emission):                 [A, B, C]
    payload["scenes"][0]["units"] = [
        {
            "kind": "dialogue",
            "text": "再等十分钟。",
            "event_key": "e1",
            "speaker_key": "g",
            "function": "decision",
            "source_text": "再等十分钟。",
            "chain_key": "wait",
        },
        {
            "kind": "dialogue",
            "text": "服务员，再来一杯水。",
            "event_key": "e1",
            "speaker_key": "g",
            "function": "statement",
            "source_text": "服务员，再来一杯水。",
            "chain_key": "aside",
        },
        {
            "kind": "dialogue",
            "text": "他到底来不来。",
            "event_key": "e1",
            "speaker_key": "g",
            "function": "statement",
            "source_text": "他到底来不来。",
            "chain_key": "wait",
        },
    ]
    return payload


def main() -> None:
    payload = build_interleaved_payload()
    ir = ScreenplayGenerationIR.model_validate(payload)
    screenplay = compile_screenplay_ir(
        ir,
        episode={
            "id": "ep-drift",
            "episode_no": 1,
            "title": "drift",
            "authorized_source_chapters": {"chapter-1": DRIFT_SOURCE},
        },
        source_text=DRIFT_SOURCE,
        bible=_bible(),
    )
    print("=== IR compile key_lines (path 1) ===")
    for k in screenplay.key_lines:
        print("  ", k)

    print("=== full_script_text ===")
    print(screenplay.full_script_text)

    doc = screenplay_to_document(screenplay)
    restored = document_to_screenplay(doc)
    print("=== document projection key_lines (path 2) ===")
    for k in restored.key_lines:
        print("  ", k)

    print("=== EQUAL? ===", screenplay.key_lines == restored.key_lines)

    # Show KL id references in plot_spine to check for cross-ref impact
    print("=== plot_spine beat key_line_ids (IR) ===")
    for b in screenplay.plot_spine.spine_beats:
        print("  ", b.beat_id, b.key_line_ids)


if __name__ == "__main__":
    main()
