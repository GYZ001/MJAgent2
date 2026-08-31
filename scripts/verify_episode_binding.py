#!/usr/bin/env python3
"""定点验证：目标集里指定角色是否正确绑定人物谱、拿到定妆照。

背景：人物身份改造工具链正在迭代（app/identity_authority.py、app/portraits.py、
app/production/prep_pack.py 都可能在改动中）。迭代期每轮要验证的问题很具体——
某几集里某几个角色有没有正确绑定——而 scripts/yyft_serial10.py 是十集严格串行
驱动，跑一次约 40 分钟。本脚本只跑用户指定的那几集，几分钟内出结果。

清除剧本 / 重新生成 / 轮询终态的 HTTP 调用方式（BASE、X-Manju-Session 头、
approved() 两步审批、await_terminal 轮询、失败证据查询）与
scripts/yyft_serial10.py 语义完全一致，是从那个文件复制过来的，不是另起一套。
之所以复制而不是 `from scripts.yyft_serial10 import ...`：yyft_serial10.py 在
模块顶层无条件 `from app.production.prep_pack import PREP_PACK_VERSION`（供它自
己的 cmd_verify 用）；如果 prep_pack.py 正被另一个 agent 半编辑，那一行会让
整个 yyft_serial10 模块导入失败，进而拖累这个本该在同一时间窗口独立工作的点
检脚本。本脚本不需要那个常量，因此干脆不碰 app/ 下任何模块——纯 HTTP + 只读
SQLite，与 app/ 的编辑状态完全解耦。

并发模式（--concurrent [N]）：清除阶段仍串行（快速 HTTP 调用，串行不吃时间，
并发反而可能互相干扰），生成+轮询阶段并发跑指定的几集，四集约 12-15 分钟可缩
短到接近单集耗时。后端槽位排查结论（只读查证，未改 app/）：
  app/generation_concurrency.py 有两级进程级优先级信号量（per event loop 共
  享，不分项目/不分集号）：
    - "screenplay"/"storyboard" 整个工作流共享 "text_generation_workflows"
      门，上限取 settings.text_generation_workflow_concurrency（当前库中=10），
      封顶 MAX_TEXT_GENERATION_CONCURRENCY=16。start_screenplay 排队时打的
      「剧本任务已排队，等待文本生成槽位」就是在等这道门（app/domain/
      screenplay_ops.py 的 _screenplay_guarded -> run_with_generation_slot）。
    - 工作流内部实际的 LLM 请求另有一道更细的 "text_provider_calls" 门，上限
      取 settings.text_generation_concurrency（当前库中=6），工作流之间、同一
      工作流内的分片请求都在这道门后面排队，与上面那道门各自独立计数。
  结论：**不是全局 1**。当前配置下最多 10 个剧本/分镜工作流可以同时处于
  running（4 集验证远低于此），工作流内部的实际模型调用共享 6 个并发槽位。
  也就是说 4 集并发生成时，4 个工作流会真的同时跑，其内部请求在 6 个槽位
  上交替执行——是真并行，不是排队假象。这两个上限都是进程级全局设置，
  --concurrent 的 N 再大也换不来更多真实并发，超过 6~10 之后收益边际递减。
  DEFAULT_CONCURRENCY 因此保守取 3：清除阶段验证时段外该后端还服务其它请求
  （如另两个正在改 app/portraits.py、app/production/prep_pack.py 的 agent 可
  能触发的调用），3 集同时生成不会把 6 个 provider 槽位或 10 个工作流槽位占
  满，留出余量。

**⚠️ 并发模式只用于定点验证，不得用于十集最终验收。** 原因：跨集状态累积本
身就是被测对象之一——app/production/prep_pack.py 的
`_prep_pack_cross_episode_alias_conflict` 要读取其它集已发布的产物，
episodes.screenplay_character_resolutions 是逐集累加的。这些依赖的可见顺序
在串行验收中是确定的（EP1 先跑完、其产物才可能被 EP2 看到），并发会打乱这个
顺序——同一批集号可能同时都还没发布，也可能乱序发布，跨集判定路径因此走的
不是验收时的真实路径。并发模式下即使全部 PASS，也不能替代
`scripts/yyft_serial10.py` 的严格串行验收结果；最终验收永远走
scripts/yyft_serial10.py。

默认验证集 EP1/EP2/EP5/EP6，各自诊断职责（可用 --episodes 覆盖）：
  EP1  核心案发现场：许清（原文标签「银色长袍女子」）、李富贵（混在「其他被
       困少年」群演标签里）预期均未绑定——身份判定的起点用例。
  EP6  许清在此集原文标签「许师姐」——已登记别名，单独检验身份决议层
       "别名 -> identity" 这条接缝是否修通。
  EP5  许清在此集原文标签「许姓女子」——描述性标签、不在别名库中，检验未登记
       标签的候选判别新路径。
  EP2  李富贵在此集原本就已正确绑定——回归对照，确保改动没有把原本正确的绑
       定搞坏。

用法：
    # 默认：EP1/EP2/EP5/EP6，角色 许清/李富贵，先清除+重新生成，再判定
    .venv/bin/python scripts/verify_episode_binding.py

    # 只跑 EP1
    .venv/bin/python scripts/verify_episode_binding.py --episodes 1 --expect 许清 李富贵

    # 不清除/不重新生成，只读当前已有数据判定
    # （app/ 正被改动、此刻新生成的结果不可信时用这个模式）
    .venv/bin/python scripts/verify_episode_binding.py --no-regen

    # 附加跨集一致性检查：默认组 许清:EP1,EP5,EP6 / 李富贵:EP1,EP2 的
    # visual_entity_id 是否完全一致（可用 --consistency-groups 覆盖）
    .venv/bin/python scripts/verify_episode_binding.py --consistency

    # 换一批集号/角色/项目，一致性分组也可自定义
    .venv/bin/python scripts/verify_episode_binding.py --project proj_xxx \\
        --episodes 3 4 --expect 张三 --consistency --consistency-groups "张三:3,4"

    # 并发模式：清除仍串行，生成+轮询并发跑（不带值用保守默认并发数）；
    # 仅用于定点验证，不得用于十集最终验收，见上方警告与 --help
    .venv/bin/python scripts/verify_episode_binding.py --concurrent

    # 并发模式，显式指定最大并发数
    .venv/bin/python scripts/verify_episode_binding.py --concurrent 4

出场判据（零模型调用，确定性；2026-08-25 修订，见下方"判据修订说明"）：角色
在某集"未绑定"时，脚本判定它是"在场但未绑定"（FAIL）还是"本集确实不出场"
（SKIP）还是"信号不足、无法判定"（UNKNOWN，第三态，见下）。主判据取自本集
screenplay_identity_discovery 产物（身份判定阶段独立产出的候选发现结果，与
本脚本读取的 asset_manifest 是同一条生产管线的不同阶段产物，非本脚本另起
的名单）：该产物 candidates[] 里 authority_id=="bible:{角色规范名}" 的条目，
其 kind 字段是模型对"这个候选是否在本集画面内实际出现"的直接判断——
kind=="onscreen" 即真实出场，kind=="mentioned" 即仅被提及、未出镜。任一条目
onscreen -> FAIL（在场但未绑定，是缺陷）；有条目但全部 mentioned -> SKIP
（确实不出场，不是缺陷）；该 authority_id 在候选列表中完全没出现、或本集
根本没有该产物 -> UNKNOWN（判据数据不足，不猜测，单独列出交人工核对，不计
入 FAIL 也不计入 SKIP）。旧的"规范名/别名逐字出现在原文中即算出场"文本启发
式（character_appears_in_source()）保留作为日志里的辅助参考信息（非最终判
据），因为它只能证明"被提及"而不能证明"在场"，二者不等价——历史教训见函数
体注释。

判据修订说明：本判据替换的旧版本存在"提及≠在场"的误报——旧版只要角色规范
名/已确认别名在原文中逐字出现就判定出场，但原文里角色常被第三方提及（如
"许师姐的洞府""被赵武刚师兄拽入公开区"）而角色本人并未出场，2026-08-25 定
点验证在 EP8 许清、EP8 李富贵、EP5 赵武刚 三处产生了这类误报。新判据不再扫
描原文，改为读取身份判定阶段已经做出的 onscreen/mentioned 结构化判断——这
是该阶段模型判断的直接产物字段，不是本脚本发明的判据，也不含任何具体人名
或集号的特判分支（authority_id 前缀规则对人物谱内任意角色都成立）。

退出码：0=全部通过（含"绑定成功""本集不出场"与"无法判定"三类——无法判定不
是缺陷证据，不阻断退出码，只是信号不足，因此不视为失败）；1=有角色出场但
未绑定（或已绑定但缺 portrait），或跨集一致性不一致；2=参数错误或生成失败
（未达到 ready 终态）。与旧版本完全一致，未新增退出码分支；"无法判定"项总
是在日志里单独列出提示人工核对，无论最终 PASS/FAIL。日志同时写
logs/verify_episode_binding.log。

纪律：项目 id、集号、角色名、一致性分组全部来自命令行参数（含下面的
DEFAULT_* 常量，仅用作 argparse 默认值，可被命令行覆盖）；判定逻辑本身
（绑定检查 / 出场判据 / 一致性比较）不含任何具体人名或集号的特判分支——换默
认值只需改 DEFAULT_* 常量，不用碰下面的函数体。
"""
from __future__ import annotations

import argparse
import sys
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# 直接 `python scripts/x.py` 运行时 sys.path[0] 是 scripts/，不是仓库根。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.session_token import session_token  # noqa: E402
BASE = "http://127.0.0.1:8230"
LOG = ROOT / "logs" / "verify_episode_binding.log"

DEFAULT_PROJECT_ID = "proj_3ac0b627fa46"
DEFAULT_EPISODES = [1, 2, 5, 6]
DEFAULT_EXPECT = ["许清", "李富贵"]
DEFAULT_CONSISTENCY_GROUPS = "许清:1,5,6;李富贵:1,2"
# 保守默认并发数：后端两道进程级槽位（见文件头 docstring 排查结论）当前配置为
# 工作流门=10、实际 provider 调用门=6，且都是全进程共享，不分项目/集号。3 留
# 出明显余量，不会把这两道门占满，同时对 4 集验证仍有实打实的加速。
DEFAULT_CONCURRENCY = 3

_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


# --- HTTP / 轮询 / 清除 helpers -- 与 scripts/yyft_serial10.py 语义一致（同一份
# BASE、同一个会话 token、同一套两步审批、同一套轮询终态判定），复制自那个文件，
# 理由见文件头 docstring。 ---------------------------------------------------

def call(method: str, path: str, body: dict | None = None,
         headers: dict | None = None, timeout: int = 90) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("X-Manju-Session", session_token())
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:500]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": repr(exc)}


def approved(method: str, path: str, body: dict, timeout: int = 120) -> tuple[int, dict]:
    """Issue one command, confirming the two-step approval when asked."""
    code, resp = call(method, path, body, timeout=timeout)
    token = resp.get("approval_token")
    if token:
        code, resp = call(
            method, path, body,
            headers={"x-manju-approval-token": token}, timeout=timeout,
        )
    return code, resp


def status_of(eid: str) -> dict:
    code, payload = call("GET", f"/api/episodes/{eid}/screenplay/status", timeout=60)
    if code != 200:
        return {"screenplay_status": f"HTTP{code}", "detail": payload}
    return payload


def brief(payload: dict) -> str:
    production = payload.get("screenplay_production") or {}
    return "{st} active={a} phase={p} stage={i}/{n} err={e}".format(
        st=payload.get("screenplay_status"),
        a=payload.get("active"),
        p=production.get("phase"),
        i=production.get("stage_index"),
        n=production.get("stage_count"),
        e=(payload.get("screenplay_error") or "").replace("\n", " ")[:200],
    )


def await_terminal(name: str, eid: str, interval: int = 10, limit: int = 1800) -> dict:
    """Poll until the episode reaches a terminal state (ready/failed/pending/
    repairing-not-active). limit=1800s 留足单集含新角色发现（身份判定 + 定妆照/
    场景参考图生成）时的真实模型调用耗时余量，与 yyft_serial10.py 的
    await_terminal 取值一致。"""
    waited = 0
    last = ""
    while waited <= limit:
        payload = status_of(eid)
        state = str(payload.get("screenplay_status") or "")
        line = brief(payload)
        if line != last:
            log(f"{name} :: {line}")
            last = line
        if state == "ready":
            return payload
        if state in {"failed"} and not payload.get("active"):
            return payload
        if state == "repairing" and not payload.get("active"):
            return payload
        if state == "pending" and not payload.get("active"):
            return payload
        time.sleep(interval)
        waited += interval
    log(f"{name} TIMEOUT after {waited}s")
    return status_of(eid)


def start_or_resume(name: str, eid: str) -> bool:
    payload = status_of(eid)
    state = str(payload.get("screenplay_status") or "")
    eligibility = (payload.get("screenplay_production") or {}).get("eligibility") or {}
    resumable = bool(eligibility.get("resumable"))
    stamp = int(time.time())
    if resumable and state in {"repairing", "failed"}:
        code, resp = approved(
            "POST", f"/api/episodes/{eid}/screenplay/resume",
            {"idempotency_key": f"verify-binding-resume-{eid}-{stamp}"},
        )
        log(f"{name} resume -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
    else:
        code, resp = approved(
            "POST", f"/api/episodes/{eid}/screenplay",
            {"idempotency_key": f"verify-binding-start-{eid}-{stamp}"},
        )
        log(f"{name} start -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:200]}")
    return resp.get("status") in {"queued", "running", "repairing"}


def clear_one(name: str, eid: str) -> bool:
    payload = status_of(eid)
    state = payload.get("screenplay_status")
    if state in {"queued", "running", "repairing"} and payload.get("active"):
        code, resp = approved("POST", f"/api/episodes/{eid}/screenplay/cancel", {})
        log(f"{name} cancel -> HTTP{code} {json.dumps(resp, ensure_ascii=False)[:160]}")
        for _ in range(60):
            time.sleep(2)
            if not status_of(eid).get("active"):
                break
    code, resp = approved("DELETE", f"/api/episodes/{eid}/screenplay", {})
    final = status_of(eid).get("screenplay_status")
    log(f"{name} clear: {state} -> delete HTTP{code} -> {final}")
    return final == "pending"


def _readonly_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path if db_path is not None else (ROOT / "data" / "manju.db")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def recent_failure_evidence(eid: str, since: float) -> str:
    """Durable evidence for the last failure: error logs + provider calls
    (same query shape as scripts/yyft_serial10.py's homonymous function)."""
    conn = _readonly_conn()
    try:
        chunks = [
            f"{row['id']} | {row['category']} | {row['exc_type']} | {row['message']}"
            for row in conn.execute(
                "SELECT id, category, exc_type, message FROM error_logs "
                "WHERE ts>=? AND json_extract(context_json,'$.episode_id')=? "
                "ORDER BY ts DESC LIMIT 6",
                (since, eid),
            )
        ]
        chunks += [
            f"call {row['id']} http={row['http_status']} status={row['status']} "
            f"err={str(row['error'] or '')[:300]}"
            for row in conn.execute(
                "SELECT id, http_status, status, error FROM provider_calls "
                "WHERE ts>=? AND status!='OK' ORDER BY id DESC LIMIT 8",
                (since,),
            )
        ]
    finally:
        conn.close()
    return "\n".join(chunks)


# --- 本脚本特有部分：集号解析 / asset_manifest 读取 / 绑定判定 / 一致性比较 ---

def resolve_episode_id(conn: sqlite3.Connection, project_id: str, episode_no: int) -> str | None:
    row = conn.execute(
        "SELECT id FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, episode_no),
    ).fetchone()
    return row["id"] if row is not None else None


def load_asset_manifest(
    conn: sqlite3.Connection, eid: str,
) -> tuple[dict | None, str | None, str | None, str]:
    """返回 (asset_manifest, screenplay_status, published_artifact_id, note)。

    manifest 为 None 时 note 说明原因；不要求当前 screenplay_status=='ready'
    才读——已发布 artifact 是独立于实时状态的既成事实（与 yyft_serial10.py
    cmd_verify 读取 published_screenplay_artifact_id 的方式一致）。
    """
    row = conn.execute(
        "SELECT screenplay_status, published_screenplay_artifact_id "
        "FROM episodes WHERE id=?", (eid,),
    ).fetchone()
    if row is None:
        return None, None, None, "分集记录不存在"
    status = row["screenplay_status"]
    artifact_id = row["published_screenplay_artifact_id"]
    if not artifact_id:
        return None, status, None, "无已发布 artifact"
    art = conn.execute(
        "SELECT content_json FROM artifacts WHERE id=?", (artifact_id,),
    ).fetchone()
    if art is None:
        return None, status, artifact_id, "已发布 artifact 记录缺失（悬空引用）"
    try:
        content = json.loads(art["content_json"] or "{}")
    except json.JSONDecodeError:
        return None, status, artifact_id, "artifact content_json 解析失败"
    manifest = content.get("asset_manifest") or {}
    return manifest, status, artifact_id, ""


def find_bound_character(manifest: dict, name: str) -> dict | None:
    """在 asset_manifest.characters[] 中找 display_name==name 或 name 出现在
    aliases[] 的条目。不匹配 functional_extras——那是本脚本刻意留给人工判断的
    部分（见文件头 docstring：标签文本可能与角色名完全不同，机械匹配只会制
    造假阳性/假阴性，判断权交给运行脚本的人看打印出的标签列表）。"""
    for character in manifest.get("characters") or []:
        if character.get("display_name") == name or name in (character.get("aliases") or []):
            return character
    return None


def load_character_aliases(conn: sqlite3.Connection, project_id: str) -> dict[str, list[str]]:
    """读取 projects.bible_json -> characters[].aliases[].text，构造
    {规范名: [已确认别名, ...]}。这是"出场判据"启发式的数据源之一——与候选集
    构造用的是同一份 bible_json，不额外引入其它判据。"""
    row = conn.execute(
        "SELECT bible_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if row is None or not row["bible_json"]:
        return {}
    try:
        bible = json.loads(row["bible_json"])
    except json.JSONDecodeError:
        return {}
    aliases_by_name: dict[str, list[str]] = {}
    for character in bible.get("characters") or []:
        cname = character.get("name")
        if not cname:
            continue
        aliases_by_name[cname] = [
            a.get("text") for a in (character.get("aliases") or []) if a.get("text")
        ]
    return aliases_by_name


def load_episode_source_text(conn: sqlite3.Connection, project_id: str, episode_no: int) -> str:
    """复刻 app/domain/common.py::_episode_source_text 的主路径（episodes.
    source_chapters 记录的章节序号 -> chapters 表逐章拼接「【title】\\ncontent」），
    只读 SQL，不导入 app/ 下任何模块——与文件头 docstring 说明的解耦原则一致。

    不复刻该函数里处理"标题占位章节"的历史兼容修复分支（chapter_is_stub /
    chapter_titles_match，只影响老项目导入时残留的占位章节去重，是边界情况）：
    这里只是"出场判据"的启发式输入，不追求处理这类历史遗留边界情况。"""
    row = conn.execute(
        "SELECT source_chapters FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, episode_no),
    ).fetchone()
    if row is None or not row["source_chapters"]:
        return ""
    try:
        chapter_idxs = json.loads(row["source_chapters"])
    except json.JSONDecodeError:
        return ""
    if not chapter_idxs:
        return ""
    placeholders = ",".join("?" for _ in chapter_idxs)
    chapters = conn.execute(
        f"SELECT title, content FROM chapters WHERE project_id=? AND idx IN ({placeholders}) "
        "ORDER BY idx",
        (project_id, *chapter_idxs),
    ).fetchall()
    return "\n\n".join(f"【{c['title']}】\n{c['content']}" for c in chapters)


def character_appears_in_source(name: str, aliases: list[str], source_text: str) -> tuple[bool, str]:
    """旧版启发式，2026-08-25 起降级为"辅助参考"，不再是出场判据本身（判据见
    character_presence_verdict()）：角色规范名或其在人物谱里已确认的别名，是
    否逐字出现在该集原文中。

    降级原因：这条启发式只能证明"角色的名字被提到过"，不能证明"角色本人在
    场"——原文里角色经常被第三方提及（如"许师姐的洞府""被赵武刚师兄拽入公开
    区""在小胖子面前施展"），提到名字的那一刻角色本人往往根本不在场。
    2026-08-25 一轮定点验证里，这条启发式在 EP8 许清、EP8 李富贵、EP5 赵武刚
    三处把"被提及"误判成"在场"，进而把正确的"不出场故不绑定"错误报成 FAIL。
    保留这个函数只是因为它的匹配结果（命中了哪些词/完全没命中）仍是人工核对
    时的有用参考信号，在日志里作为辅助行打印，不用于决定 fail/skip/unknown。
    """
    terms = [name] + [a for a in aliases if a]
    seen: set[str] = set()
    unique_terms = [t for t in terms if t and not (t in seen or seen.add(t))]
    terms_desc = "、".join(f"「{t}」" for t in unique_terms)
    matched = [t for t in unique_terms if t in source_text]
    if matched:
        matched_desc = "、".join(f"「{t}」" for t in matched)
        return True, f"原文命中{matched_desc}（判据集：{terms_desc}）"
    return False, f"原文未出现{terms_desc}（规范名+已确认别名，共{len(unique_terms)}项）"


def load_identity_discovery_candidates(
    conn: sqlite3.Connection, eid: str,
) -> tuple[list[dict] | None, str]:
    """读取本集 screenplay_identity_discovery 产物的 candidates[]，作为出场判
    据的数据源（见 character_presence_verdict()）。

    该产物由身份判定阶段（app/portraits.py::discover_character_candidates，
    独立于本脚本、独立于 asset_manifest 所在的 episode_prep_pack）在生成剧本
    时写入 artifacts 表：candidates[] 里每条候选都有 kind 字段
    ("onscreen"/"mentioned")，是模型对"这个候选是否在本集画面内实际出现"的
    直接判断；对已归入人物谱的候选，authority_id 统一写作
    "bible:{canonical_name}"——已对全库现存的全部 screenplay_identity_discovery
    产物核验过，identity_kind=="named" 且已归入人物谱的候选无一例外遵循此格
    式（未归入人物谱的候选 authority_id 为空串，不会与 "bible:xxx" 误匹配）。
    这是身份判定管线自己产出的结构化结论，不是本脚本另起的名单或文本匹配。

    一集可能有多条该类型产物：同一次生成内部可能有多轮判定（如现存数据里
    EP5 有两条，version=1/2，created_by_step_run_id 相同，version=2 是同一轮
    生成内部的修订版，不是两次独立生成）。取 version 最大的一批（并列再取
    created_at 最新的），并排除 stale_reason/superseded_by_artifact_id 已标记
    过期的记录（若排除后一条不剩，退回未排除前的全集，不无谓地制造"无数
    据"）。返回 None 表示本集确实没有任何可用的该类型产物——调用方必须判定
    为"无法判定"，不得回退用其它信号猜测。"""
    rows = conn.execute(
        "SELECT id, content_json, version, created_at, stale_reason, "
        "superseded_by_artifact_id FROM artifacts "
        "WHERE type='screenplay_identity_discovery' AND scope_type='episode' "
        "AND scope_id=?",
        (eid,),
    ).fetchall()
    if not rows:
        return None, "本集无 screenplay_identity_discovery 产物"
    fresh = [r for r in rows if not r["stale_reason"] and not r["superseded_by_artifact_id"]]
    pool = fresh if fresh else rows
    max_version = max(r["version"] for r in pool)
    chosen = [r for r in pool if r["version"] == max_version]
    if len(chosen) > 1:
        latest_ts = max(r["created_at"] for r in chosen)
        chosen = [r for r in chosen if r["created_at"] == latest_ts]
    candidates: list[dict] = []
    used_ids: list[str] = []
    for row in chosen:
        try:
            content = json.loads(row["content_json"] or "{}")
        except json.JSONDecodeError:
            continue
        candidates.extend(content.get("candidates") or [])
        used_ids.append(row["id"])
    if not used_ids:
        return None, f"该集 {len(rows)} 条 screenplay_identity_discovery 产物 content_json 均解析失败"
    return (
        candidates,
        f"取 version={max_version} 的产物 {used_ids}（该集共有候选产物 {len(rows)} 条）",
    )


def character_presence_verdict(
    name: str, candidates: list[dict] | None, legacy_appears: bool,
) -> tuple[str, str]:
    """判定角色是否在本集"出场"（画面内实际出现，而非仅被提及）。返回三态之
    一："present"（确定在场）/"absent"（确定未出场）/"unknown"（信号不足，不
    猜测）。

    主判据：candidates（见 load_identity_discovery_candidates()）里
    authority_id=="bible:{name}" 的条目的 kind 字段——
      - 任一条目 kind=="onscreen" -> "present"；
      - 有条目但全部 kind=="mentioned" -> "absent"；
      - 该 authority_id 在候选列表中完全没出现，或 candidates 为 None（本集
        没有该产物）-> 见下面的"零证据兜底"。

    零证据兜底（仅用于"完全没有候选条目"这一种情况，不影响上面两条已有明确
    kind 的分支）：额外看 legacy_appears——角色规范名或已确认别名是否逐字出
    现在本集原文的任意位置（character_appears_in_source() 的布尔结果，两个
    独立信号源：一个是喂过全量人物谱给模型做的语义扫描，一个是对全文做的字
    面子串扫描）。
      - legacy_appears 也是 False（两个独立信号都找不到任何证据）
        -> "absent"：这与旧判据被推翻的方向不同——旧判据的错误是"提到名字
        就当作在场"（假阳性方向），这里用的是相反方向"两种独立方法都完全找
        不到任何提及，判定未出场"（假阴性风险极低：真出场的角色若连一次规
        范名/别名都不曾被提及、又逃过了喂了全量人物谱的语义扫描，是双重小
        概率事件的交集）。
      - legacy_appears 为 True（原文其实提到过这个名字，但识别管线没有产出
        对应候选、既没判 onscreen 也没判 mentioned）-> "unknown"：两个信号出
        现矛盾（原文有证据，但结构化识别没消化），这才是真正"信号不足，不
        该猜"的情形，交人工核对。

    不含任何具体人名或集号的特判分支：authority_id 前缀规则对人物谱内任意
    角色都成立，name/legacy_appears 完全来自调用方传入的参数。"""
    authority_id = f"bible:{name}"
    if candidates is None:
        if not legacy_appears:
            return (
                "absent",
                "本集无 screenplay_identity_discovery 产物，且原文中规范名/已确认别名也"
                "完全未出现（原文扫描独立确认缺席）",
            )
        return (
            "unknown",
            "本集无 screenplay_identity_discovery 产物，但原文中规范名/已确认别名确实"
            "出现过——缺少结构化信号消化这次出现，无法判断是否在场",
        )
    kinds = {c.get("kind") for c in candidates if c.get("authority_id") == authority_id}
    if "onscreen" in kinds:
        return "present", f"identity_discovery 候选 authority_id={authority_id!r} 含 kind=onscreen"
    if kinds:
        return (
            "absent",
            f"identity_discovery 候选 authority_id={authority_id!r} 存在但 kind 均为"
            f"{sorted(kinds)}（无 onscreen）",
        )
    if not legacy_appears:
        return (
            "absent",
            f"identity_discovery 候选列表中未出现 authority_id={authority_id!r}，且原文中"
            "规范名/已确认别名也完全未出现（两个独立信号都指向缺席）",
        )
    return (
        "unknown",
        f"identity_discovery 候选列表中未出现 authority_id={authority_id!r} 对应条目，"
        "但原文中规范名/已确认别名确实出现过——识别管线未对这次出现给出 onscreen/"
        "mentioned 判定，信号矛盾，无法判断",
    )


def portrait_exists(conn: sqlite3.Connection, portrait_id: str | None) -> bool:
    if not portrait_id:
        return False
    return bool(conn.execute(
        "SELECT 1 FROM character_portraits WHERE id=?", (portrait_id,),
    ).fetchone())


def parse_consistency_groups(spec: str) -> dict[str, list[int]]:
    """解析 "name:ep,ep,ep;name:ep,ep" 形式的字符串为 {name: [ep,...]}。纯格式
    解析，不含任何具体人名/集号的特判——spec 本身（含默认值）才是数据。"""
    groups: dict[str, list[int]] = {}
    spec = spec.strip()
    if not spec:
        return groups
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, eps = part.partition(":")
        name = name.strip()
        if not sep or not name or not eps.strip():
            raise ValueError(f"--consistency-groups 格式错误：{part!r}，期望 name:ep,ep,...")
        try:
            ep_list = [int(token) for token in eps.split(",") if token.strip()]
        except ValueError as exc:
            raise ValueError(f"--consistency-groups 集号必须是整数：{part!r}") from exc
        if not ep_list:
            raise ValueError(f"--consistency-groups 集号列表为空：{part!r}")
        groups[name] = ep_list
    return groups


def main() -> int:
    parser = argparse.ArgumentParser(
        description="定点验证指定集里指定角色是否正确绑定人物谱、拿到定妆照。",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT_ID, help="项目 id")
    parser.add_argument(
        "--episodes", type=int, nargs="+", default=list(DEFAULT_EPISODES),
        help="目标集号（纯数字，如 1 5 6）",
    )
    parser.add_argument(
        "--expect", nargs="+", default=list(DEFAULT_EXPECT),
        help="要检查的角色名（display_name，逐个空格分隔）",
    )
    parser.add_argument(
        "--no-regen", action="store_true",
        help="跳过清除+重新生成，只读现有数据判定",
    )
    parser.add_argument(
        "--concurrent", type=int, nargs="?", const=DEFAULT_CONCURRENCY, default=None,
        metavar="N",
        help=(
            "并发模式：清除阶段仍串行，生成+轮询阶段最多 N 集并发跑"
            f"（只写 --concurrent 不带 N 时用保守默认 N={DEFAULT_CONCURRENCY}）。"
            "不传本参数时行为与现在完全一致（串行）。"
            "⚠️ 仅用于定点验证单个绑定判断是否修好，不得用于十集最终验收——"
            "跨集状态累积（prep_pack 的跨集别名冲突检查读取其它集已发布产物、"
            "screenplay_character_resolutions 逐集累加）本身就是被测对象，并发"
            "会打乱这些依赖的可见顺序；最终验收永远走 scripts/yyft_serial10.py "
            "的严格串行流程，见文件头 docstring。"
        ),
    )
    parser.add_argument(
        "--consistency", action="store_true",
        help="附加跨集一致性检查（同一角色在多集的 visual_entity_id 是否一致）",
    )
    parser.add_argument(
        "--consistency-groups", default=DEFAULT_CONSISTENCY_GROUPS,
        help='一致性分组，格式 "name:ep,ep,...;name:ep,ep,..."，仅在 --consistency 时生效',
    )
    args = parser.parse_args()

    if args.concurrent is not None and args.concurrent < 1:
        log(f"参数错误：--concurrent 必须 >= 1，收到 {args.concurrent}")
        return 2

    try:
        consistency_groups = (
            parse_consistency_groups(args.consistency_groups) if args.consistency else {}
        )
    except ValueError as exc:
        log(f"参数错误：{exc}")
        return 2

    conn = _readonly_conn()
    try:
        episode_ids: dict[int, str] = {}
        for ep_no in args.episodes:
            eid = resolve_episode_id(conn, args.project, ep_no)
            if eid is None:
                log(f"EP{ep_no} 在项目 {args.project} 下找不到分集记录，退出。")
                return 2
            episode_ids[ep_no] = eid
    finally:
        conn.close()

    if args.no_regen:
        regen_mode = "否"
    elif args.concurrent:
        regen_mode = f"是（并发 N={args.concurrent}，仅供定点验证，见 --help 警告）"
    else:
        regen_mode = "是（串行）"
    log(f"=== 定点验证开始：project={args.project} episodes={args.episodes} "
        f"expect={args.expect} regen={regen_mode} "
        f"consistency={'是' if args.consistency else '否'} ===")

    generation_failed = False
    if args.no_regen:
        log("--no-regen：跳过清除/重新生成，直接读取现有数据。")
    elif args.concurrent:
        max_workers = min(args.concurrent, len(args.episodes))
        log("--- 阶段 1/2：串行清除各集剧本（清除不并发，避免相互干扰） ---")
        for ep_no in args.episodes:
            eid = episode_ids[ep_no]
            name = f"EP{ep_no}"
            log(f"--- {name} 清除剧本 ---")
            if not clear_one(name, eid):
                log(f"{name} 清除未达到 pending 状态，仍继续尝试生成。")

        log(f"--- 阶段 2/2：并发生成各集剧本（有效并发数={max_workers}） ---")

        def _regen_and_wait(ep_no: int) -> tuple[int, bool]:
            eid = episode_ids[ep_no]
            name = f"EP{ep_no}"
            try:
                since = time.time()
                log(f"--- {name} 重新生成 ---")
                if not start_or_resume(name, eid):
                    log(f"{name} 无法启动生成，判定生成失败。")
                    return ep_no, True
                payload = await_terminal(name, eid)
                state = str(payload.get("screenplay_status") or "")
                if state != "ready":
                    log(f"{name} 未达到 ready：state={state} "
                        f"err={(payload.get('screenplay_error') or '')[:300]}")
                    for line in recent_failure_evidence(eid, since).splitlines()[:10]:
                        log(f"    {name} {line}")
                    return ep_no, True
                log(f"{name} READY")
                return ep_no, False
            except Exception as exc:  # noqa: BLE001 - 隔离单集异常，不拖垮其它集
                log(f"{name} 并发生成过程中抛出异常，判定生成失败：{exc!r}")
                return ep_no, True

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_regen_and_wait, ep_no): ep_no for ep_no in args.episodes
            }
            for future in as_completed(futures):
                _ep_no, failed = future.result()
                if failed:
                    generation_failed = True
    else:
        for ep_no in args.episodes:
            eid = episode_ids[ep_no]
            name = f"EP{ep_no}"
            log(f"--- {name} 清除剧本 ---")
            if not clear_one(name, eid):
                log(f"{name} 清除未达到 pending 状态，仍继续尝试生成。")
            since = time.time()
            log(f"--- {name} 重新生成 ---")
            if not start_or_resume(name, eid):
                log(f"{name} 无法启动生成，判定生成失败。")
                generation_failed = True
                continue
            payload = await_terminal(name, eid)
            state = str(payload.get("screenplay_status") or "")
            if state != "ready":
                generation_failed = True
                log(f"{name} 未达到 ready：state={state} "
                    f"err={(payload.get('screenplay_error') or '')[:300]}")
                for line in recent_failure_evidence(eid, since).splitlines()[:10]:
                    log(f"    {line}")
            else:
                log(f"{name} READY")

    per_episode: dict[int, dict[str, Any]] = {}
    conn = _readonly_conn()
    try:
        alias_map = load_character_aliases(conn, args.project)
        for ep_no in args.episodes:
            eid = episode_ids[ep_no]
            manifest, status, artifact_id, note = load_asset_manifest(conn, eid)
            entry: dict[str, Any] = {
                "status": status, "artifact_id": artifact_id, "characters": {},
            }
            log(f"\n=== EP{ep_no} ({eid}) screenplay_status={status} "
                f"published_artifact={artifact_id} ===")
            # 出场判据用的信号与 manifest 是否可读无关（manifest 缺失时依旧要能
            # 区分"未绑定因为没出场"和"未绑定因为真出问题"），因此在两个分支之
            # 前统一算好。source_text 只喂给降级为辅助参考的旧启发式；主判据
            # 用的 identity_candidates 是独立于 asset_manifest 的另一份产物。
            source_text = load_episode_source_text(conn, args.project, ep_no)
            identity_candidates, identity_note = load_identity_discovery_candidates(conn, eid)
            log(f"  识别信号来源：{identity_note}")

            if manifest is None:
                log(f"  无法读取 asset_manifest：{note}")
                for name in args.expect:
                    legacy_appears, legacy_basis = character_appears_in_source(
                        name, alias_map.get(name, []), source_text,
                    )
                    state, basis = character_presence_verdict(
                        name, identity_candidates, legacy_appears,
                    )
                    verdict = {"present": "fail", "absent": "skip", "unknown": "unknown"}[state]
                    if verdict == "fail":
                        log(f"  [{name}] FAIL —— 出场但未绑定（本集无可读 asset_manifest）"
                            f"—— 判据：{basis}")
                    elif verdict == "skip":
                        log(f"  [{name}] SKIP（{basis}）—— 判定本集不出场，不计入 FAIL")
                    else:
                        log(f"  [{name}] 无法判定（{basis}）—— 既不计入 FAIL 也不计入 SKIP，"
                            "需人工核对")
                    log(f"      旧文本启发式仅供参考（非最终判据）：{legacy_basis}")
                    entry["characters"][name] = {
                        "bound": False, "has_portrait": False, "verdict": verdict,
                        "presence_state": state, "presence_basis": basis,
                        "legacy_text_match": legacy_appears, "legacy_text_basis": legacy_basis,
                    }
                per_episode[ep_no] = entry
                continue

            extras = manifest.get("functional_extras") or []
            for name in args.expect:
                bound_char = find_bound_character(manifest, name)
                if bound_char is not None:
                    portrait_id = bound_char.get("portrait_id")
                    has_portrait = portrait_exists(conn, portrait_id)
                    verdict = "bound_ok" if has_portrait else "fail"
                    provenance = bound_char.get("provenance") or {}
                    log(f"  [{name}] 绑定成功 -> characters[]")
                    log(
                        f"      identity_id={bound_char.get('identity_id')!r} "
                        f"portrait_id={portrait_id!r}"
                        + ("" if has_portrait else "  ← ⚠ character_portraits 表中不存在！FAIL")
                    )
                    log(
                        f"      visual_entity_id={bound_char.get('visual_entity_id')!r} "
                        f"display_appellation={bound_char.get('display_appellation')!r}"
                    )
                    log(f"      provenance.method={provenance.get('method')!r}")
                    entry["characters"][name] = {
                        "bound": True,
                        "has_portrait": has_portrait,
                        "verdict": verdict,
                        "identity_id": bound_char.get("identity_id"),
                        "portrait_id": portrait_id,
                        "visual_entity_id": bound_char.get("visual_entity_id"),
                        "display_appellation": bound_char.get("display_appellation"),
                        "provenance_method": provenance.get("method"),
                    }
                else:
                    legacy_appears, legacy_basis = character_appears_in_source(
                        name, alias_map.get(name, []), source_text,
                    )
                    state, basis = character_presence_verdict(
                        name, identity_candidates, legacy_appears,
                    )
                    verdict = {"present": "fail", "absent": "skip", "unknown": "unknown"}[state]
                    if verdict == "fail":
                        log(f"  [{name}] FAIL —— 出场但未绑定 —— 不在 characters[] 中"
                            f"（仍是无图群演/未识别）—— 判据：{basis}")
                        if extras:
                            log(f"      本集 functional_extras 全部标签"
                                f"（人工核对是否是「{name}」的误判标签）：")
                            for ex in extras:
                                ex_provenance = (ex.get("provenance") or {}).get("method")
                                log(
                                    f"        - label={ex.get('label')!r} "
                                    f"visual_entity_id={ex.get('visual_entity_id')!r} "
                                    f"provenance.method={ex_provenance!r}"
                                )
                        else:
                            log("      本集 functional_extras 为空列表。")
                    elif verdict == "skip":
                        log(f"  [{name}] SKIP（{basis}）—— 判定本集不出场，不计入 FAIL")
                    else:
                        log(f"  [{name}] 无法判定（{basis}）—— 既不计入 FAIL 也不计入 SKIP，"
                            "需人工核对")
                        if extras:
                            log(f"      本集 functional_extras 全部标签"
                                f"（人工核对是否是「{name}」的误判标签）：")
                            for ex in extras:
                                ex_provenance = (ex.get("provenance") or {}).get("method")
                                log(
                                    f"        - label={ex.get('label')!r} "
                                    f"visual_entity_id={ex.get('visual_entity_id')!r} "
                                    f"provenance.method={ex_provenance!r}"
                                )
                    log(f"      旧文本启发式仅供参考（非最终判据）：{legacy_basis}")
                    entry["characters"][name] = {
                        "bound": False, "has_portrait": False, "verdict": verdict,
                        "presence_state": state, "presence_basis": basis,
                        "legacy_text_match": legacy_appears, "legacy_text_basis": legacy_basis,
                    }
            per_episode[ep_no] = entry
    finally:
        conn.close()

    unbound_pairs = [
        (ep_no, name)
        for ep_no, entry in per_episode.items()
        for name, c in entry["characters"].items()
        if c.get("verdict") == "fail"
    ]
    skip_pairs = [
        (ep_no, name)
        for ep_no, entry in per_episode.items()
        for name, c in entry["characters"].items()
        if c.get("verdict") == "skip"
    ]
    bound_ok_pairs = [
        (ep_no, name)
        for ep_no, entry in per_episode.items()
        for name, c in entry["characters"].items()
        if c.get("verdict") == "bound_ok"
    ]
    unknown_pairs = [
        (ep_no, name)
        for ep_no, entry in per_episode.items()
        for name, c in entry["characters"].items()
        if c.get("verdict") == "unknown"
    ]

    consistency_ok = True
    if args.consistency:
        log("\n=== 跨集一致性检查 ===")
        if not consistency_groups:
            log("  --consistency-groups 为空，无分组可比较。")
        for name, ep_list in consistency_groups.items():
            relevant = [ep for ep in ep_list if ep in per_episode]
            skipped = [ep for ep in ep_list if ep not in per_episode]
            if skipped:
                log(f"  [{name}] 分组含未在本次 --episodes 中的集号 {skipped}，已跳过这些集。")
            values = {
                ep: per_episode[ep]["characters"][name].get("visual_entity_id")
                for ep in relevant
                if per_episode[ep]["characters"].get(name, {}).get("bound")
            }
            # 未绑定的集（不管是"本集不出场"的 skip 还是"出场但未绑定"的 fail）
            # 都没有 visual_entity_id 可比，天然被上面的 values 过滤掉——不会因
            # 为某集是"本集不出场"就把一致性判成 FAIL。这里只是把排除原因打印
            # 出来，方便人工核对。
            excluded_verdicts = {
                ep: per_episode[ep]["characters"].get(name, {}).get("verdict")
                for ep in relevant
                if ep not in values
            }
            if excluded_verdicts:
                log(f"  [{name}] 组内以下集不参与比较（未绑定）：{excluded_verdicts}")
            if len(values) < 2:
                log(f"  [{name}] 组 {ep_list} 中只有 {len(values)} 集绑定成功，"
                    f"无法比较（各集实际值：{values}）")
                continue
            distinct = set(values.values())
            if len(distinct) == 1:
                log(f"  [{name}] PASS —— {values}")
            else:
                consistency_ok = False
                log(f"  [{name}] FAIL —— visual_entity_id 不一致：{values}")

    log("\n=== 总判定 ===")
    log(f"绑定成功 {len(bound_ok_pairs)} / 未绑定 {len(unbound_pairs)} / "
        f"本集不出场 {len(skip_pairs)} / 无法判定 {len(unknown_pairs)}")
    # "无法判定"不是缺陷证据（信号不足，不是"出场但未绑定"），因此不参与 FAIL
    # 判断、不影响退出码——但既然承认了不知道，就必须把它显眼地列出来，不能
    # 让它悄悄消失在 PASS 里，无论最终走的是哪条分支都要打印。
    if unknown_pairs:
        unknown_detail = "、".join(f"EP{ep}:{name}" for ep, name in unknown_pairs)
        log(f"⚠ 无法判定 {len(unknown_pairs)} 项，需人工核对（既不计入 FAIL 也不计入 SKIP，"
            f"不影响退出码）：{unknown_detail}")
    if generation_failed:
        log("FAIL（生成失败，见上方证据）")
        return 2
    if unbound_pairs:
        detail = "、".join(f"EP{ep}:{name}" for ep, name in unbound_pairs)
        log(f"FAIL —— 出场但未绑定或缺少 portrait：{detail}")
        if skip_pairs:
            skip_detail = "、".join(f"EP{ep}:{name}" for ep, name in skip_pairs)
            log(f"    （另有本集不出场、已跳过不计入 FAIL：{skip_detail}）")
        return 1
    if not consistency_ok:
        log("FAIL —— 跨集一致性检查未通过（见上方）")
        return 1
    log(
        "PASS —— 全部期望角色已绑定且有 portrait，或本集不出场已跳过"
        + ("，跨集一致性通过" if args.consistency else "")
    )
    if skip_pairs:
        skip_detail = "、".join(f"EP{ep}:{name}" for ep, name in skip_pairs)
        log(f"    （本集不出场已跳过：{skip_detail}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
