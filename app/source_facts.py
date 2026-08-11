"""Typed source-unit facts shared by Blueprint and scene compilation."""
from __future__ import annotations

import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.source_excerpt import index_source_segments


class SourceFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_unit_key: str
    source_segment_id: str
    unit_order: int
    projection: Literal["action", "dialogue"]
    text: str


def source_unit_key(source_segment_id: str, unit_order: int) -> str:
    return f"{source_segment_id}:unit:{unit_order:03d}"


def source_segment_facts(
    source_segment_id: str,
    source_text: str,
) -> list[SourceFact]:
    """Split one source segment by structural quotation boundaries."""
    text = str(source_text or "").strip()
    if not text:
        parts: list[tuple[Literal["action", "dialogue"], str]] = [
            ("action", ""),
        ]
    else:
        parts = []
        outside: list[str] = []
        quoted: list[str] = []
        quote_open = ""

        def has_content(value: str) -> bool:
            return any(
                not (
                    char.isspace()
                    or unicodedata.category(char).startswith("P")
                )
                for char in value
            )

        def flush_outside() -> None:
            value = "".join(outside).strip()
            outside.clear()
            if value and has_content(value):
                parts.append(("action", value))

        for char in text:
            category = unicodedata.category(char)
            quotation_mark = "QUOTATION MARK" in unicodedata.name(char, "")
            if not quote_open:
                if category == "Pi" or quotation_mark:
                    flush_outside()
                    quote_open = char
                    quoted.append(char)
                else:
                    outside.append(char)
                continue
            quoted.append(char)
            if category == "Pf" or (
                quotation_mark and char == quote_open
            ):
                value = "".join(quoted).strip()
                quoted.clear()
                quote_open = ""
                if value:
                    parts.append(("dialogue", value))

        if quoted:
            outside.extend(quoted)
        flush_outside()
        if not parts:
            parts = [("action", text)]

    return [
        SourceFact(
            source_unit_key=source_unit_key(source_segment_id, order),
            source_segment_id=source_segment_id,
            unit_order=order,
            projection=projection,
            text=value,
        )
        for order, (projection, value) in enumerate(parts, start=1)
    ]


def source_facts(source_text: str) -> list[SourceFact]:
    return [
        fact
        for segment in index_source_segments(source_text)
        for fact in source_segment_facts(segment.segment_id, segment.text)
    ]
