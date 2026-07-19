from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ContextItem:
    key: str
    content_hash: str
    source_artifact_id: str | None
    original_chars: int
    selected_chars: int
    truncated: bool
    truncation_strategy: str


@dataclass(slots=True)
class ContextPack:
    goal: str
    items: list[ContextItem] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_text(
        self,
        key: str,
        text: str,
        *,
        limit: int,
        source_artifact_id: str | None = None,
        truncation_strategy: str = "head",
    ) -> str:
        encoded = text.encode("utf-8")
        selected = text if len(text) <= limit else text[:limit]
        self.items.append(
            ContextItem(
                key=key,
                content_hash=hashlib.sha256(encoded).hexdigest(),
                source_artifact_id=source_artifact_id,
                original_chars=len(text),
                selected_chars=len(selected),
                truncated=len(selected) < len(text),
                truncation_strategy=truncation_strategy,
            )
        )
        return selected

    def manifest(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "items": [asdict(item) for item in self.items],
            "metadata": self.metadata,
        }
