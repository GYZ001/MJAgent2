"""Typed source-unit facts shared by Blueprint and scene compilation.

This layer records source syntax only.  Quotation marks prove that a span is
quoted, but they do not prove that a character audibly speaks it.  The
Blueprint owns that semantic delivery decision so written text, remembered
words, sound effects, and actual speech cannot be conflated here.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.source_excerpt import (
    index_source_segments,
    quotation_closing,
    quotation_opening,
)


SOURCE_FACT_VERSION = "source-fact.v4"
ACTION_CLAUSE_BOUNDARIES = frozenset("。！？!?；;：:，,\n\r")
_STRUCTURAL_SECTION_DIVIDER = re.compile(
    r"(?m)^[^\w\s]{5,}[ \t]*(?:\n|\Z)"
)


class SourceFactQuotationError(ValueError):
    """A source unit contains an opening quote with no structural close."""

    def __init__(
        self,
        source_segment_id: str,
        *,
        opening: str,
        offset: int,
    ) -> None:
        self.source_segment_id = source_segment_id
        self.opening = opening
        self.offset = offset
        super().__init__(
            "[SOURCE_FACT_QUOTE_UNCLOSED] "
            f"{source_segment_id} offset={offset} 的开引号 "
            f"{opening!r} 没有闭引号"
        )


class SourceFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Older versions remain readable for historical failure audits. All newly
    # derived facts use v4, and cache fingerprints require the current value.
    contract_version: Literal[
        "source-fact.v2",
        "source-fact.v3",
        "source-fact.v4",
    ] = (
        SOURCE_FACT_VERSION
    )
    source_unit_key: str
    source_segment_id: str
    unit_order: int
    projection: Literal["action", "quoted", "paratext"]
    surface_form: Literal["prose", "quoted_span", "paratext_span"]
    text: str

    @model_validator(mode="after")
    def _validate_surface_projection(self) -> "SourceFact":
        expected = {
            "prose": "action",
            "quoted_span": "quoted",
            "paratext_span": "paratext",
        }[self.surface_form]
        if self.projection != expected:
            raise ValueError(
                "SourceFact projection 必须由 surface_form 确定"
            )
        return self


def source_unit_key(source_segment_id: str, unit_order: int) -> str:
    return f"{source_segment_id}:unit:{unit_order:03d}"


def source_segment_facts(
    source_segment_id: str,
    source_text: str,
) -> list[SourceFact]:
    """Split one source segment by quotation and action-clause boundaries."""
    text = str(source_text or "").strip()
    divider = _STRUCTURAL_SECTION_DIVIDER.search(text)
    paratext = ""
    if divider is not None:
        paratext = text[divider.end():].strip()
        text = text[:divider.start()].strip()
    if not text and paratext:
        parts = []
    elif not text:
        parts: list[
            tuple[
                Literal["action", "quoted", "paratext"],
                Literal["prose", "quoted_span", "paratext_span"],
                str,
            ]
        ] = [
            ("action", "prose", ""),
        ]
    else:
        parts = []
        outside: list[str] = []
        quoted: list[str] = []
        quote_open = ""
        quote_open_offset = -1

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
                parts.append(("action", "prose", value))

        for offset, char in enumerate(text):
            if not quote_open:
                if quotation_opening(char):
                    flush_outside()
                    quote_open = char
                    quote_open_offset = offset
                    quoted.append(char)
                else:
                    outside.append(char)
                    if char in ACTION_CLAUSE_BOUNDARIES:
                        flush_outside()
                continue
            quoted.append(char)
            if quotation_closing(quote_open, char):
                value = "".join(quoted).strip()
                quoted.clear()
                quote_open = ""
                quote_open_offset = -1
                if value:
                    parts.append(("quoted", "quoted_span", value))

        if quoted:
            raise SourceFactQuotationError(
                source_segment_id,
                opening=quote_open,
                offset=quote_open_offset,
            )
        flush_outside()
        if not parts:
            parts = [("action", "prose", text)]

    if paratext:
        appendix_unit: list[str] = []

        def flush_paratext() -> None:
            value = "".join(appendix_unit).strip()
            appendix_unit.clear()
            if value and any(
                not (
                    char.isspace()
                    or unicodedata.category(char).startswith("P")
                )
                for char in value
            ):
                parts.append(("paratext", "paratext_span", value))

        for char in paratext:
            appendix_unit.append(char)
            if char in ACTION_CLAUSE_BOUNDARIES:
                flush_paratext()
        flush_paratext()

    return [
        SourceFact(
            source_unit_key=source_unit_key(source_segment_id, order),
            source_segment_id=source_segment_id,
            unit_order=order,
            projection=projection,
            surface_form=surface_form,
            text=value,
        )
        for order, (projection, surface_form, value)
        in enumerate(parts, start=1)
    ]


def source_facts(source_text: str) -> list[SourceFact]:
    return [
        fact
        for segment in index_source_segments(source_text)
        for fact in source_segment_facts(segment.segment_id, segment.text)
    ]
