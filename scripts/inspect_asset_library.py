"""人物谱/场景库/物件库核查（每轮映射台/分镜台结束后的检查项，用户 2026-09-06 指令）。

用法：MJ_DB=/root/MJAgent2/data/manju.db MJ_PROJECT=proj_xxx python scripts/inspect_asset_library.py
在 B 上：ssh mjb "MJ_DB=data/manju.db .venv/bin/python scripts/inspect_asset_library.py"

原始说明：
映射台结束后的人物谱/场景库/物件库核查（在 B 上以只读方式跑）。
用法：ssh mjb "/root/MJAgent2/.venv/bin/python - < /tmp/mj_inspect_mapping.py"（从 A 用 stdin 传入）。
输出：人物卡清单 + 疑点标记（同姓氏键多卡 / 合称 / 纯描述称谓 / 无定妆），场景库与物件库清单与重复名。
"""
import collections, json, os, re, sqlite3

PID = "proj_f8cf2eeb2e66"
DB = os.environ.get("MJ_DB", "data/manju.db"); PID = os.environ.get("MJ_PROJECT", PID)
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); c.row_factory = sqlite3.Row
TITLES = ("师兄", "师姐", "师弟", "师妹", "师叔", "师伯", "师祖", "师父", "师傅", "师尊", "长老", "前辈", "道友", "道长",
          "公子", "姑娘", "小姐", "大人", "仙子", "真人", "上人", "老祖", "老爷", "掌门", "宗主", "先生", "夫人",
          "兄", "姐", "弟", "妹", "叔", "伯", "爷", "哥")
GROUP_RE = re.compile(r"(两个|二人|三个|三名|几名|众|一群|数名|两名|众人)")
DESCRIPTOR_RE = re.compile(r"(男子|女子|青年|老者|少年|老人|汉子|女修|弟子|少女|孩童|小孩)$")


def surname_key(label):
    m = re.match(r"^(\S)姓", label)
    if m:
        return m.group(1)
    for t in TITLES:
        if label.endswith(t) and len(label) - len(t) == 1:
            return label[0]
    return None


bible = json.loads(c.execute("select bible_json from projects where id=?", (PID,)).fetchone()[0] or "{}")
chars = bible.get("characters") or []
portraits = collections.Counter(r[0] for r in c.execute("select character_name from character_portraits where project_id=? and ep_start>=0", (PID,)))
print(f"== 人物谱 {len(chars)} 张卡 ==")
keys = collections.defaultdict(list)
for ch in chars:
    name = ch.get("name") or ""
    aliases = [a.get("text") for a in ch.get("aliases") or []]
    flags = []
    if GROUP_RE.search(name):
        flags.append("合称")
    if DESCRIPTOR_RE.search(name) and not surname_key(name):
        flags.append("描述称谓")
    if not portraits.get(name):
        flags.append("无定妆")
    for form in [name, *aliases]:
        k = surname_key(form) or (form[0] if len(form) >= 2 and surname_key(form) is None and any(surname_key(x) for x in [name, *aliases]) else None)
        if k:
            keys[k].append(name)
    print(f"  {name:<10} {ch.get('role','')}  定妆{portraits.get(name,0)}  别名{aliases}  {'/'.join(flags)}  | {(ch.get('appearance_canonical') or '')[:40]}")
dups = {k: sorted(set(v)) for k, v in keys.items() if len(set(v)) > 1}
print("同姓氏键多卡（疑似同一人）:", dups or "无")
print("\n== 场景库 ==")
scenes = c.execute("select scene_name, ep_start, ep_end, scene_canonical, image_path is not null img, pack_status from scene_references where project_id=? order by ep_start, scene_name", (PID,)).fetchall()
names = collections.Counter(r["scene_name"] for r in scenes)
for r in scenes:
    print(f"  {r['scene_name']:<14} ep{r['ep_start']}-{r['ep_end']}  {'图' if r['img'] else '无图'} {r['pack_status'] or ''}  | {(r['scene_canonical'] or '')[:40]}")
print("重名场景:", [n for n, k in names.items() if k > 1] or "无")
print("\n== 物件库 ==")
props = c.execute("select prop_name, ep_start, ep_end, appearance, image_path is not null img, status from prop_references where project_id=? order by ep_start, prop_name", (PID,)).fetchall()
pnames = collections.Counter(r["prop_name"] for r in props)
for r in props:
    print(f"  {r['prop_name']:<12} ep{r['ep_start']}-{r['ep_end']}  {'图' if r['img'] else '无图'} {r['status'] or ''}  | {(r['appearance'] or '')[:40]}")
print("重名物件:", [n for n, k in pnames.items() if k > 1] or "无")
print("\n== 集状态 ==", dict(c.execute("select status,count(*) from episodes where project_id=? and episode_no<=30 group by status", (PID,)).fetchall()))
