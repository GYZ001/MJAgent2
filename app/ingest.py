"""小说摄入：编码识别、章节切分、广告清洗（PRD §4.1）。纯本地处理，不调模型。"""
from __future__ import annotations

import re

CHAPTER_RE = re.compile(r"^\s*(第[0-9一二三四五六七八九十百千万零两]+[章卷回节][^\n]{0,40})\s*$", re.MULTILINE)

AD_MARKERS = (
    "http://", "https://", "www.", "微信", "qq群", "QQ群", "求收藏", "求推荐", "求月票",
    "本章完", "天才一秒记住", "最新章节", "笔趣阁", "顶点小说", "手机阅读",
)

FALLBACK_CHUNK_CHARS = 3000


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


def split_chapters(text: str) -> list[dict]:
    """按章节标题切分；识别不到 2 个标题时按字数等分并提示性命名。"""
    matches = list(CHAPTER_RE.finditer(text))
    chapters = []
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
    return chapters


def ingest_novel(raw: bytes) -> dict:
    text = decode_novel(raw)
    cleaned, removed_lines = clean_text(text)
    chapters = split_chapters(cleaned)
    return {
        "total_chars": len(cleaned),
        "removed_lines": removed_lines,
        "chapter_count": len(chapters),
        "auto_split": bool(chapters and "自动切分" in chapters[0]["title"]),
        "chapters": chapters,
    }
