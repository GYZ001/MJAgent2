"""Windows 前后端启停：无控制台弹窗，后台常驻。

用法：
  scripts\\dev.cmd [start|stop|status|restart]
  restart.vbs          # 双击无黑窗

后端默认不开启 uvicorn --reload，避免源码保存中断长时间媒体任务。
确需调试后端热重载时，可显式设置 MJ_BACKEND_RELOAD=1。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
BE_LOG = LOG_DIR / "backend-dev.out.log"
FE_LOG = LOG_DIR / "frontend-dev.out.log"
BE_ERR = LOG_DIR / "backend-dev.err.log"
UVICORN = ROOT / ".venv" / "Scripts" / "uvicorn.exe"
NODE = Path(os.environ.get("NODE_EXE", "")) if os.environ.get("NODE_EXE") else None
VITE_JS = ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
DIST = ROOT / "frontend" / "dist"

# 无控制台窗口 + 独立进程组（不随启动器退出）
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


def _startupinfo_hidden() -> subprocess.STARTUPINFO:
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def _find_node() -> str:
    if NODE and NODE.exists():
        return str(NODE)
    try:
        out = subprocess.check_output(
            ["where", "node"],
            text=True,
            errors="ignore",
            creationflags=CREATE_NO_WINDOW,
            startupinfo=_startupinfo_hidden(),
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit("找不到 node.exe（请先安装 Node.js 并加入 PATH）") from exc
    for line in out.splitlines():
        candidate = line.strip()
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("找不到 node.exe（请先安装 Node.js 并加入 PATH）")


def _pids_on_port(port: int) -> list[int]:
    out = subprocess.check_output(["netstat", "-ano"], text=True, errors="ignore")
    pids: set[int] = set()
    for line in out.splitlines():
        if "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[1]
        host_port = local.rsplit(":", 1)
        if len(host_port) != 2 or host_port[1] != str(port):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid > 0:
            pids.add(pid)
    return sorted(pids)


def _kill_port(port: int, name: str) -> bool:
    pids = _pids_on_port(port)
    if not pids:
        print(f"{name} (:{port}) 未在运行")
        return True
    for pid in pids:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            startupinfo=_startupinfo_hidden(),
        )
    for _ in range(50):
        if not _pids_on_port(port):
            break
        time.sleep(0.1)
    left = _pids_on_port(port)
    if left:
        print(f"{name} (:{port}) 仍未退出: {left}")
        return False
    print(f"已停止 {name}（:{port}，pid={pids}）")
    return True


def _dist_stale() -> bool:
    """产物缺失，或任一前端源文件比产物新。"""
    index = DIST / "index.html"
    if not index.exists():
        return True
    stamp = index.stat().st_mtime
    watched = [ROOT / "frontend" / "index.html", ROOT / "frontend" / "vite.config.ts"]
    watched.extend((ROOT / "frontend" / "src").rglob("*"))
    return any(f.is_file() and f.stat().st_mtime > stamp for f in watched)


def _ensure_dist(node: str) -> None:
    """:8230 服务的是构建产物；main.py 在导入期判断 dist 是否存在，必须先于后端就位。

    dev 服务器（:5230）不压缩、按需现编译，公网访问首次点开每个标签都要几百 KB
    未压缩 JS + 冷编译；构建产物走 gzip 且体积小一个数量级。
    """
    if os.environ.get("MJ_SKIP_BUILD", "").strip():
        print("已跳过构建检查（MJ_SKIP_BUILD）")
        return
    if not _dist_stale():
        print("构建产物已是最新，跳过构建")
        return
    print("构建前端产物（tsc -b && vite build，约 30s）...")
    npm_cli = ROOT / "frontend" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    cmd = ([node, str(npm_cli), "run", "build"] if npm_cli.exists()
           else ["npm.cmd", "run", "build"])
    try:
        rc = subprocess.call(
            cmd, cwd=str(ROOT / "frontend"),
            creationflags=CREATE_NO_WINDOW, startupinfo=_startupinfo_hidden(),
        )
    except OSError as exc:
        print(f"[警告] 无法执行构建（{exc}）；:8230 继续服务上一次的产物")
        return
    if rc == 0:
        print(f"构建完成：{DIST}")
    else:
        print("[警告] 前端构建失败；:8230 将继续服务上一次的产物，请修掉 TS 错误后重跑")


def _start() -> tuple[subprocess.Popen[bytes], subprocess.Popen[bytes]]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not UVICORN.exists():
        raise SystemExit(f"缺少 uvicorn: {UVICORN}（先创建 .venv 并安装依赖）")
    if not VITE_JS.exists():
        raise SystemExit(f"缺少 vite: {VITE_JS}（先在 frontend 执行 npm install）")

    node = _find_node()
    _ensure_dist(node)
    dn = open(os.devnull, "rb")
    be_out = open(BE_LOG, "ab")
    be_err = open(BE_ERR, "ab")
    fe = open(FE_LOG, "ab")
    si = _startupinfo_hidden()

    # 后端默认稳定运行；仅在显式 opt-in 时启用热重载。
    backend_args = [
        str(UVICORN),
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8230",
    ]
    backend_reload = os.environ.get("MJ_BACKEND_RELOAD", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if backend_reload:
        backend_args.extend(["--reload", "--reload-dir", "app"])
    b = subprocess.Popen(
        backend_args,
        cwd=str(ROOT),
        stdin=dn,
        stdout=be_out,
        stderr=be_err,
        creationflags=CREATE_FLAGS,
        startupinfo=si,
        close_fds=True,
    )
    # 前端：直接跑 vite（HMR），避免 npm.cmd 弹黑窗
    f = subprocess.Popen(
        [node, str(VITE_JS), "--host", "127.0.0.1", "--port", "5230"],
        cwd=str(ROOT / "frontend"),
        stdin=dn,
        stdout=fe,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_FLAGS,
        startupinfo=si,
        close_fds=True,
    )
    print(f"backend pid={b.pid}  frontend pid={f.pid}")
    backend_mode = "--reload 热重载（可能中断长任务）" if backend_reload else "稳定模式（不热重载）"
    print(f"后端 http://127.0.0.1:8230  （{backend_mode}）")
    print("前端 http://127.0.0.1:5230  （Vite HMR，改前端代码时用）")
    print("日常使用请走 :8230—— 它服务构建产物，切标签比 :5230 快一个数量级。")
    print(f"日志 {BE_LOG}")
    print(f"     {FE_LOG}")
    print("无控制台窗口；前端改动自动热刷新，后端改动后请执行 restart。")
    return b, f


def _wait_ready(
    backend_process: subprocess.Popen[bytes],
    frontend_process: subprocess.Popen[bytes],
    timeout: float = 45.0,
) -> None:
    deadline = time.time() + timeout
    be_ok = fe_ok = False
    while time.time() < deadline:
        backend_exit = backend_process.poll()
        if backend_exit is not None:
            raise SystemExit(
                f"后端启动失败（退出码 {backend_exit}），请检查日志 {BE_ERR}"
            )
        frontend_exit = frontend_process.poll()
        if frontend_exit is not None:
            raise SystemExit(
                f"前端启动失败（退出码 {frontend_exit}），请检查日志 {FE_LOG}"
            )
        if not be_ok:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8230/docs", timeout=2) as r:
                    be_ok = 200 <= r.status < 300
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
        if not fe_ok:
            try:
                with urllib.request.urlopen("http://127.0.0.1:5230/", timeout=2) as r:
                    fe_ok = 200 <= r.status < 300
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
        if be_ok and fe_ok:
            print("就绪：backend=200 frontend=200")
            return
        time.sleep(0.4)
    print(f"等待超时 backend_ok={be_ok} frontend_ok={fe_ok}")
    sys.exit(1)


def main() -> None:
    action = (sys.argv[1] if len(sys.argv) > 1 else "restart").lower()
    if action in {"stop", "restart"}:
        stopped_backend = _kill_port(8230, "后端")
        stopped_frontend = _kill_port(5230, "前端")
        if not (stopped_backend and stopped_frontend):
            raise SystemExit("旧服务未能完全停止，已中止启动，避免把旧服务误报为新服务。")
        time.sleep(0.8)
    if action in {"start", "restart"}:
        if _pids_on_port(8230) and not _kill_port(8230, "后端"):
            raise SystemExit("后端端口仍被占用，已中止启动。")
        if _pids_on_port(5230) and not _kill_port(5230, "前端"):
            raise SystemExit("前端端口仍被占用，已中止启动。")
        time.sleep(0.3)
        backend_process, frontend_process = _start()
        _wait_ready(backend_process, frontend_process)
    elif action == "status":
        print(f"后端 :8230 -> {_pids_on_port(8230) or '（停）'}")
        print(f"前端 :5230 -> {_pids_on_port(5230) or '（停）'}")
    elif action == "stop":
        pass
    else:
        raise SystemExit("用法: scripts\\dev.cmd [start|stop|status|restart]")


if __name__ == "__main__":
    main()
