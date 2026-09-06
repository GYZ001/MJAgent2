"""分镜段落对原文核查（每轮映射台/分镜台结束后的检查项，用户 2026-09-06 指令）。

用法：MJ_DB=/root/MJAgent2/data/manju.db MJ_PROJECT=proj_xxx python scripts/inspect_storyboard_segments.py
在 B 上：ssh mjb "MJ_DB=data/manju.db .venv/bin/python scripts/inspect_storyboard_segments.py"

原始说明：
分镜台结束后的核查 v3（B 上只读）：按最新 storyboard 工件里每段的原文句单元区间看覆盖/重复，
台词跨段重复，说话人是否在本段资源里。
用法：ssh mjb "/root/MJAgent2/.venv/bin/python -" < /tmp/mj_inspect_storyboard.py
"""
import collections, json, os, re, sqlite3

PID = "proj_f8cf2eeb2e66"
DB = os.environ.get("MJ_DB", "data/manju.db"); PID = os.environ.get("MJ_PROJECT", PID)
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); c.row_factory = sqlite3.Row
eps = c.execute("select id, episode_no, status from episodes where project_id=? and episode_no<=30 order by episode_no", (PID,)).fetchall()


def norm(s):
    return re.sub(r"[\s“”\"'‘’「」『』，。！？…：；、,.!?:;]", "", str(s or ""))


def dialogue_items(seg):
    raw = seg.get("dialogue") or seg.get("dialogues") or []
    out = []
    for it in raw if isinstance(raw, list) else []:
        if isinstance(it, dict):
            out.append((str(it.get("speaker") or it.get("identity_id") or it.get("character") or ""), str(it.get("line") or it.get("text") or "")))
        elif isinstance(it, str):
            out.append(("", it))
    return [(s.split(":", 1)[-1], t) for s, t in out if t.strip()]


def resource_names(seg):
    names = set()
    for ch in ((seg.get("resources") or {}).get("characters") or []):
        if isinstance(ch, dict):
            for k in ("display_name", "name", "identity_id", "canonical_name"):
                v = str(ch.get(k) or "")
                if v:
                    names.add(v.split(":", 1)[-1])
        elif isinstance(ch, str):
            names.add(ch.split(":", 1)[-1])
    return names


totals = collections.Counter()
for ep in eps:
    row = c.execute("select content_json from artifacts where scope_id=? and type='storyboard' order by created_at desc limit 1", (ep["id"],)).fetchone()
    if not row:
        print(f"== 第{ep['episode_no']}集 [{ep['status']}] 无分镜工件"); continue
    shots = json.loads(row["content_json"]).get("shots") or []
    segs = [s.get("storyboard_pack_segment") or {} for s in shots]
    problems = []
    prev = None
    seen = collections.defaultdict(list)
    empty = 0
    for seg in segs:
        no = seg.get("segment_no")
        ranges = [(int(r.get("source_segment_index") or 0), int(r.get("from_unit") or 0), int(r.get("to_unit") or 0)) for r in seg.get("source_unit_ranges") or []]
        if not ranges:
            empty += 1
        elif prev:
            for (ps, pf, pt) in prev:
                for (s_, f, t) in ranges:
                    if s_ == ps:
                        if (f, t) == (pf, pt):
                            problems.append(f"段{no} 与上一段占同一块原文（段{ps} 句{pf}-{pt}）")
                        elif min(t, pt) - max(f, pf) + 1 > 0:
                            problems.append(f"段{no} 与上一段原文句单元重叠（段{ps} 句{max(f,pf)}-{min(t,pt)}）")
                        elif f < pf:
                            problems.append(f"段{no} 原文回跳（段{ps} 句{f} < 上一段句{pf}）")
                    elif s_ < ps:
                        problems.append(f"段{no} 原文段回跳（段{s_} < 段{ps}）")
        if ranges:
            prev = ranges
        names = resource_names(seg)
        for speaker, line in dialogue_items(seg):
            key = norm(line)
            if len(key) >= 6:
                seen[key].append(no)
            if speaker and names and speaker not in names and speaker != "旁白" and not re.fullmatch(r"[0-9a-f]{12,}", speaker):
                problems.append(f"段{no} 说话人「{speaker}」不在本段资源角色 {sorted(names)[:5]}")
    for key, nos in seen.items():
        if len(nos) > 1:
            problems.append(f"同一句台词出现在多段 {nos}: {key[:20]}")
    kinds = collections.Counter(re.sub(r"段\d+ ", "", p).split("（")[0][:12] for p in problems)
    totals.update(kinds)
    print(f"== 第{ep['episode_no']}集 [{ep['status']}] {len(segs)} 段（{empty} 段无原文区间），疑点 {len(problems)}：{dict(kinds)}")
    for p in problems[:5]:
        print("   ", p)
print("\n== 全部集疑点类别 ==", dict(totals.most_common(8)))
