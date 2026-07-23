"""小说摄入：编码识别、章节切分、广告清洗（PRD §4.1）。纯本地处理，不调模型。"""
from __future__ import annotations

import re

CHAPTER_RE = re.compile(r"^\s*(第[0-9一二三四五六七八九十百千万零两]+[章卷回节][^\n]{0,40})\s*$", re.MULTILINE)
CHAPTER_ID_RE = re.compile(r"^(第[0-9一二三四五六七八九十百千万零两]+[章卷回节])(.*)$")

AD_MARKERS = (
    "http://", "https://", "www.", "微信", "qq群", "QQ群", "求收藏", "求推荐", "求月票",
    "本章完", "天才一秒记住", "最新章节", "笔趣阁", "顶点小说", "手机阅读",
)

FALLBACK_CHUNK_CHARS = 3000
STUB_CHAPTER_MAX_CHARS = 120


def decode_novel(raw: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def clean_text(text: str) -> tuple[str, int]:
    """去广告行、归一空白。返回 (清洗后文本, 删除行数)。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept, removed = [], 0
    for line in lines:
        stripped = line.strip()
        if stripped and any(marker in stripped for marker in AD_MARKERS):
            removed += 1
            continue
        kept.append(stripped)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
    return cleaned.strip(), removed


def normalize_chapter_title(title: str) -> tuple[str, str]:
    """Return a stable ``(ordinal, subject)`` identity for duplicate-heading checks."""
    compact = re.sub(r"[\s　]+", "", title or "")
    compact = re.sub(r"[！!？?，,。．·：:；;（）()【】\[\]《》〈〉“”\"'‘’—…_\-]+", "", compact)
    match = CHAPTER_ID_RE.match(compact)
    if not match:
        return "", compact
    ordinal, subject = match.group(1), match.group(2)
    # Scraped novels commonly publish a TOC-like heading "某章（上）" immediately
    # before the real body heading "某章".  Ignore only a trailing part marker when
    # comparing a short stub with its adjacent rich chapter.
    subject = re.sub(r"[上下中]$", "", subject)
    return ordinal, subject


def chapter_is_stub(chapter: dict) -> bool:
    """Whether a chapter contains little more than one or two copies of its title."""
    content = re.sub(r"\s+", "", str(chapter.get("content") or ""))
    title = re.sub(r"\s+", "", str(chapter.get("title") or ""))
    return len(content) <= max(STUB_CHAPTER_MAX_CHARS, len(title) * 3)


def chapter_titles_match(left: dict, right: dict) -> bool:
    """High-precision adjacent duplicate match used only when one side is a stub."""
    left_ordinal, left_subject = normalize_chapter_title(str(left.get("title") or ""))
    right_ordinal, right_subject = normalize_chapter_title(str(right.get("title") or ""))
    if not left_ordinal or left_ordinal != right_ordinal:
        return False
    if not left_subject or not right_subject:
        return left_subject == right_subject
    return (
        left_subject == right_subject
        or left_subject in right_subject
        or right_subject in left_subject
    )


def dedupe_stub_chapters(
    chapters: list[dict],
    *,
    reindex: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Drop adjacent title-only duplicates while preserving the richer chapter body.

    We intentionally require both a matching normalized title and a large content
    asymmetry.  This avoids merging legitimate adjacent parts that happen to reuse a
    chapter number in a malformed source file.
    """
    kept: list[dict] = []
    removed: list[dict] = []
    index = 0
    while index < len(chapters):
        current = chapters[index]
        if index + 1 < len(chapters):
            following = chapters[index + 1]
            current_stub = chapter_is_stub(current)
            following_stub = chapter_is_stub(following)
            if chapter_titles_match(current, following) and current_stub != following_stub:
                richer, stub = (following, current) if current_stub else (current, following)
                if len(str(richer.get("content") or "")) >= max(
                    STUB_CHAPTER_MAX_CHARS,
                    len(str(stub.get("content") or "")) * 3,
                ):
                    kept.append(dict(richer))
                    removed.append(dict(stub))
                    index += 2
                    continue
        kept.append(dict(current))
        index += 1
    if reindex:
        for number, chapter in enumerate(kept, start=1):
            chapter["idx"] = number
    return kept, removed


def _split_chapters_with_removed(text: str) -> tuple[list[dict], list[dict]]:
    matches = list(CHAPTER_RE.finditer(text))
    chapters: list[dict] = []
    if len(matches) >= 2:
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            if len(body) > len(m.group(1)) + 10:
                chapters.append({"idx": len(chapters) + 1, "title": m.group(1).strip(), "content": body})
        preamble = text[: matches[0].start()].strip()
        if len(preamble) > 200:
            chapters.insert(0, {"idx": 0, "title": "楔子", "content": preamble})
            for n, ch in enumerate(chapters):
                ch["idx"] = n + 1
    if not chapters:
        for i in range(0, len(text), FALLBACK_CHUNK_CHARS):
            chunk = text[i:i + FALLBACK_CHUNK_CHARS].strip()
            if chunk:
                chapters.append({"idx": len(chapters) + 1, "title": f"第{len(chapters) + 1}段（自动切分）", "content": chunk})
    chapters, removed = dedupe_stub_chapters(chapters)
    return chapters, removed


def split_chapters(text: str) -> list[dict]:
    """按章节标题切分；识别不到 2 个标题时按字数等分并提示性命名。"""
    chapters, _ = _split_chapters_with_removed(text)
    return chapters


def ingest_novel(raw: bytes) -> dict:
    text = decode_novel(raw)
    cleaned, removed_lines = clean_text(text)
    chapters, duplicate_stubs = _split_chapters_with_removed(cleaned)
    return {
        "total_chars": len(cleaned),
        "removed_lines": removed_lines,
        "chapter_count": len(chapters),
        "deduplicated_stub_chapters": len(duplicate_stubs),
        "auto_split": bool(chapters and "自动切分" in chapters[0]["title"]),
        "chapters": chapters,
    }
