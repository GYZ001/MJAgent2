"""小说摄入：编码识别、章节切分、广告清洗（PRD §4.1）。纯本地处理，不调模型。

章节标题的正面定义、小节边界与尺寸拆分/合并判据在 ``app.novel.structure``
（拆分原因见该模块 docstring）；本模块负责解码/清洗/去重与整条摄入流水线。
"""
from __future__ import annotations

import json
import re

from app.novel.structure import (
    CHAPTER_ID_RE as CHAPTER_ID_RE,
    CHAPTER_ORDINAL_RE,
    CHAPTER_RE as CHAPTER_RE,
    _CHAPTER_NUMERALS,
    _extract_sections,
    _find_heading_matches,
    _merge_undersized_ending_chapters,
    _parse_chapter_number,
    _preamble_chapters,
    _split_oversized_chapters,
    _TRAILING_DECOR_RE,
)

_RTF_SIGNATURE_RE = re.compile(r"^\s*\{\\rtf\d", re.IGNORECASE)
_HTML_DOCUMENT_RE = re.compile(r"^\s*(?:<!DOCTYPE\s+html|<html[\s>])", re.IGNORECASE)

SEPARATOR_ONLY_RE = re.compile(r"^(?:[-_=~*]{4,}|[－—～·]{4,})$")
TRAILING_JUNK_ONLY_RE = re.compile(r"^[;；,，:：|丨]+$")

FALLBACK_CHUNK_CHARS = 3000
STUB_CHAPTER_MAX_CHARS = 120
MAX_NOVEL_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_CONTROL_CHAR_RATIO = 0.01


def decode_novel(raw: bytes) -> str:
    bom_encodings = (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    )
    for bom, encoding in bom_encodings:
        if raw.startswith(bom):
            return raw.decode(encoding)
    for encoding in ("utf-8-sig", "gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def validate_novel_text(text: str) -> None:
    """Reject binary/corrupt uploads before they become an unusable project."""
    if not text or not text.strip():
        raise ValueError("文件没有可读取的正文内容，请检查后重新选择")
    if "\x00" in text:
        raise ValueError("文件包含二进制内容，不是可读取的 TXT 小说")
    # RTF/HTML 是纯 ASCII/文字编码，不会被上面的空字节或控制符占比拦下——
    # 但正文其实是格式标记（`{\rtf1...}`、`<html>...`），不是可读小说：曾有
    # 项目把 RTF 文件当 TXT 上传，全书被当无标题正文按 3000 字硬切，11/11
    # 次下游剧本生成失败。只在文档**开头**匹配，避免正文里偶然提到「html」
    # 之类词语被误拦。
    head = text.lstrip()
    if _RTF_SIGNATURE_RE.match(head):
        raise ValueError(
            "上传的是 RTF 富文本文件，不是纯文本小说，无法解析章节。"
            "请在文字处理软件里「另存为」，格式选 TXT（或改导出 EPUB）后重新导入"
        )
    if _HTML_DOCUMENT_RE.match(head):
        raise ValueError(
            "上传的是网页/HTML 文件，不是纯文本小说，无法解析章节。"
            "请把正文另存为 TXT（或改导出 EPUB）后重新导入"
        )
    controls = sum(
        1 for char in text
        if ord(char) < 32 and char not in {"\n", "\r", "\t", "\f"}
    )
    if controls / max(len(text), 1) > MAX_CONTROL_CHAR_RATIO:
        raise ValueError("文件包含过多不可见控制字符，请另存为 UTF-8 TXT 后重试")


def clean_text(text: str) -> tuple[str, int]:
    """Normalize layout without deleting prose according to its vocabulary."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    removed = 0
    for line in lines:
        stripped = line.strip()
        if stripped and (
            SEPARATOR_ONLY_RE.fullmatch(stripped)
            or TRAILING_JUNK_ONLY_RE.fullmatch(stripped)
        ):
            removed += 1
            continue
        kept.append(stripped)
    while kept and not kept[-1]:
        kept.pop()
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


def _chapter_ordinal(title: str) -> int | None:
    matches = list(CHAPTER_ORDINAL_RE.finditer(title or ""))
    return _parse_chapter_number(matches[-1].group(1)) if matches else None


def _find_recovery_split(lines: list[str], expected: int) -> tuple[int | None, str, str, str]:
    """在 ``lines[1:]`` 里找「补 ``expected`` 号缺失标题」的切点。

    返回 ``(split_at, normalized_title, right_first_line, prefix)``：
    ``normalized_title`` 去装饰供 ``title`` 字段用；``right_first_line`` 保留
    原文、不去装饰，供正文首行用——装饰串（如末尾的 ` ---`）是原文的一
    部分，只有标题字段需要干净，正文不能因此丢字。粘连分支（候选行是
    「核心前有其他内容」粘在一起）里，核心前的内容要和
    ``app.novel.structure._inline_heading_candidates`` 的判据一致地分真假：
    不含任何字母（``str.isalpha()`` 为假，汉字同样算字母）是标题自身的
    装饰/编号，整行原样留在正文首行、不挪给上一单元——否则第二次切（幂
    等性要求）会把 `第227章 … ---` 这整行当独占行标题重新识别，标题带上
    `---`；含字母才是上一单元的正文残片，挪到左半边末尾，``right_first_line``
    只留核心到行尾。补「章」字分支（候选行本就没有装饰，是缺字不是粘连）
    两者相同，也没有残片。
    """
    for line_index, line in enumerate(lines[1:], start=1):
        candidate = line.strip()
        recognized = list(CHAPTER_ORDINAL_RE.finditer(candidate))
        if recognized and _parse_chapter_number(recognized[-1].group(1)) == expected:
            core_start = recognized[-1].start()
            raw_tail = candidate[core_start:]
            title_candidate = _TRAILING_DECOR_RE.sub("", raw_tail).strip()
            raw_prefix = candidate[:core_start]
            if len(title_candidate) <= 80:
                if any(ch.isalpha() for ch in raw_prefix):
                    return line_index, title_candidate, raw_tail, raw_prefix.strip()
                return line_index, title_candidate, candidate, ""
        match = re.match(rf"^第([{_CHAPTER_NUMERALS}]+)", candidate)
        if not match or _parse_chapter_number(match.group(1)) != expected:
            continue
        remainder = candidate[match.end():].strip()
        if not remainder or remainder[0] in "章卷回节" or len(remainder) > 48:
            continue
        if remainder[0] in "步拜层阵剑声拳刀峰海关息日次色":
            continue
        normalized_title = f"{match.group(0)}章{remainder}"
        return line_index, normalized_title, normalized_title, ""
    return None, "", "", ""


def _recover_missing_unit_headings(chapters: list[dict]) -> list[dict]:
    """Recover standalone headings such as ``第五十三标题`` using ordinal context.

    A missing unit is accepted only when it is exactly the current ordinal + 1
    and the next recognized chapter proves that an ordinal was skipped.
    """
    recovered: list[dict] = []
    for index, chapter in enumerate(chapters):
        next_ordinal = (
            _chapter_ordinal(str(chapters[index + 1].get("title") or ""))
            if index + 1 < len(chapters) else None
        )
        fragments = [dict(chapter)]
        while next_ordinal is not None:
            current = fragments[-1]
            current_ordinal = _chapter_ordinal(str(current.get("title") or ""))
            if current_ordinal is None or next_ordinal <= current_ordinal + 1:
                break
            expected = current_ordinal + 1
            lines = str(current.get("content") or "").splitlines()
            split_at, normalized_title, right_first_line, prefix = _find_recovery_split(
                lines, expected,
            )
            if split_at is None:
                break
            left_lines = lines[:split_at] + ([prefix] if prefix else [])
            left, _ = clean_text("\n".join(left_lines))
            right_lines = lines[split_at:]
            right_lines[0] = right_first_line
            right, _ = clean_text("\n".join(right_lines))
            if len(left) < STUB_CHAPTER_MAX_CHARS or len(right) < STUB_CHAPTER_MAX_CHARS:
                break
            current["content"] = left
            current["char_count"] = len(left)
            fragments.append({
                "idx": 0,
                "title": normalized_title,
                "content": right,
                "char_count": len(right),
            })
        recovered.extend(fragments)
    for number, chapter in enumerate(recovered, start=1):
        chapter["idx"] = number
    return recovered


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


def _merge_heading_only_matches(
    text: str, matches: list[tuple[int, int, str]],
) -> list[dict]:
    """标题与下一个标题之间没有正文时，不丢弃这一行——原始上传文件不落盘，
    丢的是原文，丢了就永久找不回来，切章器不能有丢字路径。这条守卫原意是
    去掉目录式重复标题（标题后紧跟下一个标题，中间空无一字），改为把这一
    行并入下一章开头：下一章 content 从这个无正文标题的 start 算起，标题
    仍取下一章自己的；连续多个无正文标题依次累积（``pending_start`` 一直
    指向最早的那一个）。若无正文标题落在全文末尾（后面没有下一个标题），
    并入上一章末尾；整篇文档只有这一个标题且没有任何正文时没有"上一章"
    可并，退化为单独成章，避免整本书丢光。
    """
    chapters: list[dict] = []
    pending_start: int | None = None
    for i, (start, m_end, title) in enumerate(matches):
        is_last = i + 1 == len(matches)
        end = matches[i + 1][0] if not is_last else len(text)
        remainder = text[m_end:end].strip()
        effective_start = pending_start if pending_start is not None else start
        if not remainder and not is_last:
            pending_start = effective_start
            continue
        if not remainder and is_last and chapters:
            tail = text[effective_start:end].strip()
            chapters[-1]["content"] = f"{chapters[-1]['content']}\n\n{tail}".strip()
            return chapters
        chapters.append({
            "idx": len(chapters) + 1,
            "title": title,
            "content": text[effective_start:end].strip(),
        })
        pending_start = None
    return chapters


def _split_chapters_with_removed(text: str) -> tuple[list[dict], list[dict]]:
    matches = _find_heading_matches(text)
    chapters: list[dict] = []
    if matches:
        chapters = _merge_heading_only_matches(text, matches)
        chapters = _preamble_chapters(text, text[: matches[0][0]].strip()) + chapters
    if not chapters:
        for i in range(0, len(text), FALLBACK_CHUNK_CHARS):
            chunk = text[i:i + FALLBACK_CHUNK_CHARS].strip()
            if chunk:
                chapters.append({"idx": len(chapters) + 1, "title": f"第{len(chapters) + 1}段（自动切分）", "content": chunk})
    chapters = _recover_missing_unit_headings(chapters)
    chapters, removed = dedupe_stub_chapters(chapters)
    chapters = _merge_undersized_ending_chapters(chapters)
    chapters = _split_oversized_chapters(chapters)
    for number, chapter in enumerate(chapters, start=1):
        chapter["idx"] = number
        chapter["paratext_json"] = json.dumps(
            {"sections": _extract_sections(str(chapter["content"]))}, ensure_ascii=False,
        )
    return chapters, removed


def ingest_novel(raw: bytes) -> dict:
    if not raw:
        raise ValueError("文件为空，请选择包含正文的 TXT 小说")
    if len(raw) > MAX_NOVEL_UPLOAD_BYTES:
        limit_mb = MAX_NOVEL_UPLOAD_BYTES // (1024 * 1024)
        raise ValueError(f"小说文件超过 {limit_mb} MB，请拆分后再导入")
    text = decode_novel(raw)
    validate_novel_text(text)
    cleaned, removed_lines = clean_text(text)
    if not cleaned:
        raise ValueError("清理广告和空白后没有剩余正文，请检查源文件")
    chapters, duplicate_stubs = _split_chapters_with_removed(cleaned)
    return {
        "total_chars": len(cleaned),
        "removed_lines": removed_lines,
        "chapter_count": len(chapters),
        "deduplicated_stub_chapters": len(duplicate_stubs),
        "auto_split": bool(chapters and "自动切分" in chapters[0]["title"]),
        "chapters": chapters,
    }
