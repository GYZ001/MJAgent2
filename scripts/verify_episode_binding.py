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

退出码：0=全部通过；1=有角色未绑定，或跨集一致性不一致；2=参数错误或生成失败
（未达到 ready 终态）。日志同时写 logs/verify_episode_binding.log。

纪律：项目 id、集号、角色名、一致性分组全部来自命令行参数（含下面的
DEFAULT_* 常量，仅用作 argparse 默认值，可被命令行覆盖）；判定逻辑本身
（绑定检查 / 一致性比较）不含任何具体人名或集号的特判分支——换默认值只需改
DEFAULT_* 常量，不用碰下面的函数体。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8230"
SESSION = (ROOT / "data" / "regression_session_token.txt").read_text(encoding="utf-8").strip()
LOG = ROOT / "logs" / "verify_episode_binding.log"

DEFAULT_PROJECT_ID = "proj_3ac0b627fa46"
DEFAULT_EPISODES = [1, 2, 5, 6]
DEFAULT_EXPECT = ["许清", "李富贵"]
DEFAULT_CONSISTENCY_GROUPS = "许清:1,5,6;李富贵:1,2"


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
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
    request.add_header("X-Manju-Session", SESSION)
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
        "--consistency", action="store_true",
        help="附加跨集一致性检查（同一角色在多集的 visual_entity_id 是否一致）",
    )
    parser.add_argument(
        "--consistency-groups", default=DEFAULT_CONSISTENCY_GROUPS,
        help='一致性分组，格式 "name:ep,ep,...;name:ep,ep,..."，仅在 --consistency 时生效',
    )
    args = parser.parse_args()

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

    log(f"=== 定点验证开始：project={args.project} episodes={args.episodes} "
        f"expect={args.expect} regen={'否' if args.no_regen else '是'} "
        f"consistency={'是' if args.consistency else '否'} ===")

    generation_failed = False
    if not args.no_regen:
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
    else:
        log("--no-regen：跳过清除/重新生成，直接读取现有数据。")

    per_episode: dict[int, dict[str, Any]] = {}
    conn = _readonly_conn()
    try:
        for ep_no in args.episodes:
            eid = episode_ids[ep_no]
            manifest, status, artifact_id, note = load_asset_manifest(conn, eid)
            entry: dict[str, Any] = {
                "status": status, "artifact_id": artifact_id, "characters": {},
            }
            log(f"\n=== EP{ep_no} ({eid}) screenplay_status={status} "
                f"published_artifact={artifact_id} ===")
            if manifest is None:
                log(f"  无法读取 asset_manifest：{note}")
                for name in args.expect:
                    entry["characters"][name] = {"bound": False, "has_portrait": False}
                per_episode[ep_no] = entry
                continue

            extras = manifest.get("functional_extras") or []
            for name in args.expect:
                bound_char = find_bound_character(manifest, name)
                if bound_char is not None:
                    portrait_id = bound_char.get("portrait_id")
                    has_portrait = portrait_exists(conn, portrait_id)
                    provenance = bound_char.get("provenance") or {}
                    log(f"  [{name}] 绑定成功 -> characters[]")
                    log(
                        f"      identity_id={bound_char.get('identity_id')!r} "
                        f"portrait_id={portrait_id!r}"
                        + ("" if has_portrait else "  ← ⚠ character_portraits 表中不存在！")
                    )
                    log(
                        f"      visual_entity_id={bound_char.get('visual_entity_id')!r} "
                        f"display_appellation={bound_char.get('display_appellation')!r}"
                    )
                    log(f"      provenance.method={provenance.get('method')!r}")
                    entry["characters"][name] = {
                        "bound": True,
                        "has_portrait": has_portrait,
                        "identity_id": bound_char.get("identity_id"),
                        "portrait_id": portrait_id,
                        "visual_entity_id": bound_char.get("visual_entity_id"),
                        "display_appellation": bound_char.get("display_appellation"),
                        "provenance_method": provenance.get("method"),
                    }
                else:
                    log(f"  [{name}] 未绑定 —— 不在 characters[] 中（仍是无图群演/未识别）")
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
                    entry["characters"][name] = {"bound": False, "has_portrait": False}
            per_episode[ep_no] = entry
    finally:
        conn.close()

    unbound_pairs = [
        (ep_no, name)
        for ep_no, entry in per_episode.items()
        for name, c in entry["characters"].items()
        if not (c.get("bound") and c.get("has_portrait"))
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
    if generation_failed:
        log("FAIL（生成失败，见上方证据）")
        return 2
    if unbound_pairs:
        detail = "、".join(f"EP{ep}:{name}" for ep, name in unbound_pairs)
        log(f"FAIL —— 未绑定或缺少 portrait：{detail}")
        return 1
    if not consistency_ok:
        log("FAIL —— 跨集一致性检查未通过（见上方）")
        return 1
    log(
        "PASS —— 全部期望角色已绑定且有 portrait"
        + ("，跨集一致性通过" if args.consistency else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
