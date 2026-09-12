"""安装自检：磁盘、密钥文件权限、时钟偏移、模型连通性、端口误绑、必需环境变量。

一次跑完出完整报告——**不在第一条失败后停下**，PRD/enterprise/EP-06 §8 明确
要求"故意制造的错误环境下逐条报出问题"，不能只报一句"检查失败"就退出。

用法：
    .venv/bin/python scripts/preflight.py
    .venv/bin/python scripts/preflight.py --json   # 机器可读，供 CI/容器健康检查复用

退出码：0=全部通过或只有 warn；1=存在至少一条 fail。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# app.config 在 import 时会把 RUNTIME_ROOT/.env 的键值 setdefault 进
# os.environ（见该模块 _load_env()）——只读 shell 环境会漏掉只写在 .env 里、
# 没有 export 过的供应商 Key，那正是 docker-compose/systemd 两条部署路径的
# 真实配置来源，必须触发这个副作用才能如实检查。
import app.config  # noqa: E402,F401

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class CheckResult:
    name: str
    level: str
    message: str


def check_disk_space(data_dir: Path, *, warn_gb: float = 2.0, fail_gb: float = 0.5) -> CheckResult:
    try:
        usage = shutil.disk_usage(data_dir if data_dir.exists() else data_dir.parent)
    except OSError as exc:
        return CheckResult("磁盘空间", FAIL, f"无法读取磁盘用量：{exc}")
    free_gb = usage.free / (1024 ** 3)
    if free_gb < fail_gb:
        return CheckResult("磁盘空间", FAIL, f"剩余 {free_gb:.2f}GB < 下限 {fail_gb}GB")
    if free_gb < warn_gb:
        return CheckResult("磁盘空间", WARN, f"剩余 {free_gb:.2f}GB < 建议值 {warn_gb}GB")
    return CheckResult("磁盘空间", OK, f"剩余 {free_gb:.2f}GB")


# data/master.key（EP-05 主密钥）+ 其余明文密钥/令牌文件，统一要求 0600——
# 见 PRD/enterprise/EP-06 §6 安全基线表「密钥文件」一行。
SECRET_FILES: tuple[str, ...] = (
    "master.key", "local_session_secret.txt", "mcp_bootstrap_token.txt",
)


def check_secret_file_permissions(data_dir: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name in SECRET_FILES:
        path = data_dir / name
        if not path.exists():
            results.append(CheckResult(f"密钥文件权限 {name}", WARN, "尚未生成（首次启动后请重新运行本自检）"))
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            results.append(CheckResult(
                f"密钥文件权限 {name}", FAIL,
                f"当前权限 {oct(mode)}，必须是 0600（chmod 600 {path}）",
            ))
        else:
            results.append(CheckResult(f"密钥文件权限 {name}", OK, "0600"))
    return results


def check_clock_skew(
    *, reference_url: str = "https://hia.volcenginepaas.com/",
    warn_s: float = 60.0, fail_s: float = 300.0, timeout_s: float = 4.0,
) -> CheckResult:
    """拿一个可达 HTTPS 服务的响应 ``Date`` 头当参照，跟本机时间比对。

    不依赖任何新增三方库：用已有依赖 httpx；网络不可达时退化到读
    ``timedatectl``（不少企业内网环境会挡住出站 HTTPS，但本机的 NTP 同步状态
    仍然是一个比"完全不检查"更有用的信号）。
    """
    try:
        import httpx
        from email.utils import parsedate_to_datetime
        sent_at = time.time()
        resp = httpx.get(reference_url, timeout=timeout_s)
        received_at = time.time()
        remote_dt = parsedate_to_datetime(resp.headers["date"])
        local_mid = (sent_at + received_at) / 2
        skew_s = abs(local_mid - remote_dt.timestamp())
        return _clock_skew_verdict(skew_s, warn_s, fail_s)
    except Exception as exc:  # noqa: BLE001 -- 网络/解析失败都退化到本地信号
        return _clock_skew_fallback(str(exc))


def _clock_skew_verdict(skew_s: float, warn_s: float, fail_s: float) -> CheckResult:
    if skew_s >= fail_s:
        return CheckResult("时钟偏移", FAIL, f"与参照时间相差 {skew_s:.1f}s ≥ {fail_s}s，请核对 NTP/时区")
    if skew_s >= warn_s:
        return CheckResult("时钟偏移", WARN, f"与参照时间相差 {skew_s:.1f}s ≥ {warn_s}s")
    return CheckResult("时钟偏移", OK, f"与参照时间相差 {skew_s:.1f}s")


def _clock_skew_fallback(reason: str) -> CheckResult:
    import subprocess
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if out == "yes":
            return CheckResult("时钟偏移", OK, f"网络探测失败（{reason}），但 timedatectl 报告 NTP 已同步")
        return CheckResult("时钟偏移", WARN, f"网络探测失败（{reason}），且 timedatectl 报告 NTP 未同步")
    except Exception:  # noqa: BLE001 -- 连 timedatectl 都没有，如实报告查不清
        return CheckResult("时钟偏移", WARN, f"无法探测（{reason}），且本机没有 timedatectl 可退化查询")


# label + app.config 里已经算好默认值/回退链的两个属性名——不能直接读
# os.environ：BASE_URL 类配置在 app/config.py 里是"环境变量缺失就用内置默认值"
# （默认值只活在 Python 里，从不回写 os.environ），直接读 os.environ 会把
# 每一个用默认值、没有显式配 xxx_BASE_URL 的部署都误判成"未配置"（实测：
# MINIMAX_H3_API_KEY 已配置但 MINIMAX_H3_BASE_URL 走内置默认值时就复现过这个
# 假阴性）。API_KEY 一侧同理用 app.config 取值——BAILIAN_API_KEY 还有
# DASHSCOPE_API_KEY 兜底那条链，同一个理由。
PROVIDER_ENV: tuple[tuple[str, str, str], ...] = (
    ("HiAgent", "HIAGENT_BASE_URL", "HIAGENT_API_KEY"),
    ("OpenRouter", "OPENROUTER_BASE_URL", "OPENROUTER_API_KEY"),
    ("百炼", "BAILIAN_BASE_URL", "BAILIAN_API_KEY"),
    ("DeepSeek", "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY"),
    ("智谱", "ZHIPU_BASE_URL", "ZHIPU_API_KEY"),
    ("MiniMax-H3", "MINIMAX_H3_BASE_URL", "MINIMAX_H3_API_KEY"),
)


def check_model_connectivity(*, timeout_s: float = 3.0) -> list[CheckResult]:
    """只做 TCP/TLS 建连探测，不发真实业务请求——文本免费但视频有额度上限
    （见 memory），自检不该消耗任何供应商额度。"""
    results: list[CheckResult] = []
    configured_any = False
    for label, url_attr, key_attr in PROVIDER_ENV:
        base_url = str(getattr(app.config, url_attr, "") or "").strip()
        api_key = str(getattr(app.config, key_attr, "") or "").strip()
        if not api_key or not base_url:
            results.append(CheckResult(f"模型连通性 {label}", WARN, "未配置，跳过"))
            continue
        configured_any = True
        results.append(_probe_provider(label, base_url, timeout_s))
    if not configured_any:
        results.append(CheckResult("模型连通性", FAIL, "一个供应商都没配置 API Key，至少需要一个可用文本模型"))
    return results


def _probe_provider(label: str, base_url: str, timeout_s: float) -> CheckResult:
    parsed = urlsplit(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return CheckResult(f"模型连通性 {label}", FAIL, f"base_url 解析不出主机名：{base_url!r}")
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return CheckResult(f"模型连通性 {label}", OK, f"{host}:{port} 可达")
    except OSError as exc:
        return CheckResult(f"模型连通性 {label}", FAIL, f"{host}:{port} 连接失败：{exc}")


def check_port_binding(
    *, host_env: str = "MJ_BACKEND_HOST", port: int = 8230,
    dockerenv_path: Path = Path("/.dockerenv"),
) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(_check_bind_host(host_env, dockerenv_path))
    results.append(_check_port_free(port))
    return results


def _check_bind_host(host_env: str, dockerenv_path: Path) -> CheckResult:
    if dockerenv_path.exists():
        return CheckResult(
            "端口绑定地址", OK,
            "容器化部署，uvicorn 在 compose 内部网络绑定 0.0.0.0 是预期行为——"
            "公网收口由 nginx 服务负责，本项检查不适用",
        )
    host = os.environ.get(host_env, "").strip()
    if host in ("0.0.0.0", "::", ""):
        return CheckResult(
            "端口绑定地址", FAIL if host in ("0.0.0.0", "::") else WARN,
            f"{host_env}={host or '(未设置，uvicorn 默认监听全部网卡)'}，"
            "裸机部署必须收口到 127.0.0.1，由 nginx/隧道对外",
        )
    return CheckResult("端口绑定地址", OK, f"{host_env}={host}")


def _check_port_free(port: int) -> CheckResult:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.bind(("127.0.0.1", port))
        return CheckResult(f"端口 {port} 占用", OK, "未被占用")
    except OSError as exc:
        return CheckResult(f"端口 {port} 占用", FAIL, f"绑定失败（已被占用或权限不足）：{exc}")
    finally:
        probe.close()


REQUIRED_ENV_HINTS: tuple[str, ...] = ("HIAGENT_API_KEY", "OPENROUTER_API_KEY", "BAILIAN_API_KEY",
                                        "DEEPSEEK_API_KEY", "ZHIPU_API_KEY", "MINIMAX_H3_API_KEY")


def check_required_env_vars() -> CheckResult:
    # 走 app.config 属性而不是裸 os.environ：BAILIAN_API_KEY 有 DASHSCOPE_API_KEY
    # 兜底那条链（见 app/config.py），裸读环境变量名会漏掉这种情况。
    present = [k for k in REQUIRED_ENV_HINTS if str(getattr(app.config, k, "") or "").strip()]
    if not present:
        return CheckResult(
            "必需环境变量", FAIL,
            f"{'/'.join(REQUIRED_ENV_HINTS)} 一个都没设置，至少需要一个供应商 Key",
        )
    return CheckResult("必需环境变量", OK, f"已配置：{', '.join(present)}")


def run_all(data_dir: Path) -> list[CheckResult]:
    results: list[CheckResult] = [check_disk_space(data_dir)]
    results.extend(check_secret_file_permissions(data_dir))
    results.append(check_clock_skew())
    results.extend(check_model_connectivity())
    results.extend(check_port_binding())
    results.append(check_required_env_vars())
    return results


def _print_report(results: list[CheckResult]) -> None:
    icon = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
    for r in results:
        print(f"[{icon[r.level]}] {r.name}: {r.message}")
    fails = sum(1 for r in results if r.level == FAIL)
    warns = sum(1 for r in results if r.level == WARN)
    print(f"\n共 {len(results)} 项：{fails} 个 FAIL，{warns} 个 WARN。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data", help="data/ 目录路径（默认 <repo>/data）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON 而不是文本报告")
    args = parser.parse_args(argv)

    results = run_all(args.data_dir)
    if args.json:
        print(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2))
    else:
        _print_report(results)
    return 1 if any(r.level == FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
