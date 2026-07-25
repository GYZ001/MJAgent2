"""Windows 前后端启停：无控制台弹窗，后台常驻 + 热刷新。

用法：
  scripts\\dev.cmd [start|stop|status|restart]
  restart.vbs          # 双击无黑窗
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


def _kill_port(port: int, name: str) -> None:
    pids = _pids_on_port(port)
    if not pids:
        print(f"{name} (:{port}) 未在运行")
        return
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
    else:
        print(f"已停止 {name}（:{port}，pid={pids}）")


def _start() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not UVICORN.exists():
        raise SystemExit(f"缺少 uvicorn: {UVICORN}（先创建 .venv 并安装依赖）")
    if not VITE_JS.exists():
        raise SystemExit(f"缺少 vite: {VITE_JS}（先在 frontend 执行 npm install）")

    node = _find_node()
    dn = open(os.devnull, "rb")
    be_out = open(BE_LOG, "ab")
    be_err = open(BE_ERR, "ab")
    fe = open(FE_LOG, "ab")
    si = _startupinfo_hidden()

    # 后端：uvicorn --reload（改 app/ 自动热重载）
    b = subprocess.Popen(
        [
            str(UVICORN),
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8230",
            "--reload",
            "--reload-dir",
            "app",
        ],
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
    print("后端 http://127.0.0.1:8230  （--reload 热重载）")
    print("前端 http://127.0.0.1:5230  （Vite HMR）")
    print(f"日志 {BE_LOG}")
    print(f"     {FE_LOG}")
    print("无控制台窗口；改代码会自动热刷新，一般无需重启。")


def _wait_ready(timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    be_ok = fe_ok = False
    while time.time() < deadline:
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
        _kill_port(8230, "后端")
        _kill_port(5230, "前端")
        time.sleep(0.8)
    if action in {"start", "restart"}:
        if _pids_on_port(8230):
            _kill_port(8230, "后端")
        if _pids_on_port(5230):
            _kill_port(5230, "前端")
        time.sleep(0.3)
        _start()
        _wait_ready()
    elif action == "status":
        print(f"后端 :8230 -> {_pids_on_port(8230) or '（停）'}")
        print(f"前端 :5230 -> {_pids_on_port(5230) or '（停）'}")
    elif action == "stop":
        pass
    else:
        raise SystemExit("用法: scripts\\dev.cmd [start|stop|status|restart]")


if __name__ == "__main__":
    main()
