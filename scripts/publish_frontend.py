#!/usr/bin/env python3
"""把前端构建产物显式、原子地发布到 frontend/dist。

后端 ``app/main.py`` 用 ``SpaStaticFiles`` 挂载 ``frontend/dist``，而 StaticFiles
每次请求都读盘——写进 dist 的那一刻就等于发布到生产，没有后端那种「改完必须手工
重启才生效」的闸门。于是 ``npm run build`` 这个人人都会顺手跑的自检动作带上了发布
副作用。

2026-08-30 实测事故：并行 agent 跑 ``npm run build`` 做自检，前端 18:06 发布、后端
进程还停在 14:50，用户拿到新前端配旧后端，小说导入预检直接挂掉。

现在 vite 的 outDir 指向 ``frontend/dist-staging``，构建不再有副作用；发布是这个
脚本，它做三件事：

1. 构建并校验产物完整（缺 index.html 或 assets 为空一律拒绝发布）；
2. 版本偏斜闸门——后端进程的启动时间必须晚于所有 ``app/**/*.py`` 的 mtime，
   否则说明后端还没加载当前代码，此刻发布新前端就是在复现上面那次事故；
3. 原子替换——先把 dist 改名留档，再把 staging 改名成 dist；第二步失败就把留档
   改回去，中途失败旧产物原封不动。

用法::

    py scripts/publish_frontend.py            # 构建 + 校验 + 发布
    py scripts/publish_frontend.py --no-build # 用现成的 dist-staging 发布
    py scripts/publish_frontend.py --check    # 只体检，不改任何东西
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
STAGING = FRONTEND / "dist-staging"
LIVE = FRONTEND / "dist"
SUPERSEDED_PREFIX = "dist.superseded-"
KEEP_SUPERSEDED = 3

# 后端进程的判据：argv 里有一个元素**精确等于** app.main:app，且有一个元素的
# basename 以 uvicorn 开头。
#
# 关键是逐元素精确比对，不是对拼接后的命令行做子串匹配。子串匹配会自匹配：任何
# 提到这两个词的 shell 命令都会被认成后端进程。本仓库已经被这个形态咬过三次——
# `pkill -f "uvicorn app.main"` 匹配到自己的命令行把 shell 一起杀掉、`pgrep -f
# "pytest tests/"` 让人误以为全量测试在跑（其实早被自己 kill 了）、写这个脚本时
# 的验证命令也自匹配了一次。
#
# 逐元素比对结构上免疫：shell 的 argv 是 [bash, -c, <整段脚本>]，整段脚本是**一个**
# 元素，永远不会等于 app.main:app。
_BACKEND_APP_ARG = "app.main:app"


def _staging_ready() -> tuple[bool, str]:
    """产物完整性：判据挂在文件本身存不存在，不挂构建命令的退出码。"""
    if not STAGING.is_dir():
        return False, f"构建产物目录不存在：{STAGING}"
    index = STAGING / "index.html"
    if not index.is_file() or index.stat().st_size == 0:
        return False, f"缺少 index.html 或为空：{index}"
    assets = STAGING / "assets"
    if not assets.is_dir():
        return False, f"缺少 assets 目录：{assets}"
    n = sum(1 for _ in assets.iterdir())
    if n == 0:
        return False, f"assets 目录为空：{assets}"
    return True, f"产物完整（index.html + assets/ {n} 个文件）"


def _is_backend_argv(argv: list[str]) -> bool:
    """argv 逐元素判定，见 _BACKEND_APP_ARG 的注释。"""
    if _BACKEND_APP_ARG not in argv:
        return False
    return any(Path(arg).name.startswith("uvicorn") for arg in argv)


def _backend_processes() -> list[tuple[int, float]]:
    """返回全部后端进程 [(pid, 启动时间 epoch)]，按启动时间升序。

    /proc/<pid> 的 mtime 就是进程启动时刻。
    """
    me = os.getpid()
    found: list[tuple[int, float]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            argv = [a for a in (entry / "cmdline").read_bytes().decode().split("\0") if a]
            if _is_backend_argv(argv):
                found.append((int(entry.name), entry.stat().st_mtime))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
    return sorted(found, key=lambda item: item[1])


def _newest_backend_code() -> tuple[float, Path] | None:
    newest: tuple[float, Path] | None = None
    for path in (ROOT / "app").rglob("*.py"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    return newest


def _skew_gate() -> tuple[bool, str]:
    """后端必须已经加载了当前的后端代码，否则不许发布新前端。

    后端没在跑时放行：此时不存在偏斜，下次启动自然会加载当前代码。这不是「强制
    忽略」——判据本身就是从「谁在服务、它加载的是哪一版」推导出来的。
    """
    procs = _backend_processes()
    if not procs:
        return True, "后端未运行，放行（下次启动会加载当前代码）"
    # 有多个时取最早启动的那个：只要有任何一个在服务的进程代码是旧的，发布就不安全。
    pid, started = procs[0]
    extra = f"（检出 {len(procs)} 个后端进程，取最早的）" if len(procs) > 1 else ""
    newest = _newest_backend_code()
    if newest is None:
        return True, f"后端 PID {pid} 在跑{extra}，app/ 下没有 .py 文件可比对"
    code_mtime, code_path = newest
    started_s = time.strftime("%H:%M:%S", time.localtime(started))
    code_s = time.strftime("%H:%M:%S", time.localtime(code_mtime))
    if started >= code_mtime:
        return True, f"后端 PID {pid} 启动于 {started_s}{extra}，晚于最新后端代码 {code_s}"
    return False, (
        f"后端 PID {pid} 启动于 {started_s}{extra}，早于 {code_path.relative_to(ROOT)} 的 {code_s}——"
        f"后端还没加载当前代码，现在发前端就是新前端配旧后端。\n"
        f"    出路：按 PID 重启后端再发布（不要用 pkill -f，它会匹配到自己的命令行）：\n"
        f"      kill {pid} && sleep 6 && setsid nohup .venv/bin/python ./.venv/bin/uvicorn \\\n"
        f"        app.main:app --host 0.0.0.0 --port 8230 > logs/backend_$(date +%m%d_%H%M).log 2>&1 &"
    )


def _build() -> None:
    print("  构建中（npm run build）……")
    result = subprocess.run(
        ["npm", "run", "build"], cwd=FRONTEND, capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.stdout.write(result.stdout[-4000:])
        sys.stderr.write(result.stderr[-4000:])
        raise SystemExit(f"构建失败（退出码 {result.returncode}），旧产物原封不动")


def _atomic_swap() -> Path | None:
    """先留档再替换；第二步失败把留档改回去。同文件系统内 rename 是原子的。"""
    superseded: Path | None = None
    if LIVE.exists():
        superseded = FRONTEND / f"{SUPERSEDED_PREFIX}{time.strftime('%m%d_%H%M%S')}"
        os.rename(LIVE, superseded)
    try:
        os.rename(STAGING, LIVE)
    except OSError:
        if superseded is not None:
            os.rename(superseded, LIVE)
        raise
    return superseded


def _prune_superseded() -> None:
    """只保留最近几份留档。删除前逐个列出来，不静默清理。"""
    olds = sorted(
        (p for p in FRONTEND.iterdir() if p.is_dir() and p.name.startswith(SUPERSEDED_PREFIX)),
        key=lambda p: p.name,
        reverse=True,
    )
    for path in olds[KEEP_SUPERSEDED:]:
        print(f"  清理旧留档 {path.name}")
        shutil.rmtree(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build", action="store_true", help="用现成的 dist-staging")
    parser.add_argument("--check", action="store_true", help="只体检，不改任何东西")
    args = parser.parse_args()

    print("前端发布")
    if args.check:
        ok_skew, msg_skew = _skew_gate()
        ok_art, msg_art = _staging_ready()
        print(f"  版本偏斜闸门：{'通过' if ok_skew else '拦截'} — {msg_skew}")
        print(f"  构建产物：    {'就绪' if ok_art else '未就绪'} — {msg_art}")
        return 0 if ok_skew else 1

    # 偏斜闸门放在构建之前：拦得住就别白跑一次构建。
    ok, msg = _skew_gate()
    print(f"  版本偏斜闸门：{msg}")
    if not ok:
        return 1

    if not args.no_build:
        _build()
    ok, msg = _staging_ready()
    print(f"  产物校验：{msg}")
    if not ok:
        return 1

    superseded = _atomic_swap()
    print(f"  已发布 → {LIVE.relative_to(ROOT)}")
    if superseded is not None:
        print(f"  旧产物留档 → {superseded.relative_to(ROOT)}（回滚：改名换回 dist）")
    _prune_superseded()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
