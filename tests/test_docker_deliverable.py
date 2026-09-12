"""EP-06 一键交付物的结构性守卫（不依赖本机装了 docker——这台机器没有，见
派单）：Dockerfile/compose/.env.example 的静态不变量，能在 CI 里当轻量闸门用，
真正的构建/联调验证走人工 ``docker compose up``。

有意不引入 PyYAML：全仓运行时依赖只有 7 个、dev 依赖只有 3 个
（requirements-dev.txt），这是 CLAUDE.md 明确要保住的优点。docker-compose.yml
结构简单、缩进固定，用字符串切片按 "  <service>:" 这个顶格两格缩进的惯例切出
每个服务块就够验证，不值得为一个测试文件换一条新依赖。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _service_block(compose_text: str, service: str) -> str:
    """从 ``  <service>:`` 起，切到下一个同缩进级别的顶层 key 为止。"""
    match = re.search(rf"^  {re.escape(service)}:\n(.*?)(?=^  \S|\Z)", compose_text, re.M | re.S)
    assert match, f"docker-compose.yml 里找不到服务块 {service!r}"
    return match.group(1)


def test_dockerfile_exists_and_is_multi_stage():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert text.count("FROM ") >= 2, "应为多阶段构建（前端 + 运行时）"
    assert "EXPOSE 8230" in text
    assert "ENTRYPOINT" in text


def test_dockerfile_never_copies_data_or_env():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for forbidden in ("COPY data", "COPY .env", "COPY projects"):
        assert forbidden not in text, f"镜像不得包含数据/密钥：发现 {forbidden!r}"


def test_dockerignore_excludes_runtime_state():
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in ("data/", "projects/", ".env"):
        assert pattern in text, f".dockerignore 缺 {pattern!r}，构建上下文可能带进真实数据/密钥"


def test_compose_has_app_and_nginx_services_with_state_volumes():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^  app:\n", text, re.M)
    assert re.search(r"^  nginx:\n", text, re.M)

    app_block = _service_block(text, "app")
    assert not re.search(r"^\s+ports:", app_block, re.M), "app 不该直接对宿主发布端口，只有 nginx 面向公网"
    for target in ("/app/data", "/app/projects", "/app/logs"):
        assert target in app_block, f"app 服务缺少挂载 {target}"

    nginx_block = _service_block(text, "nginx")
    assert re.search(r'":80"', nginx_block) or re.search(r":80\b", nginx_block), "nginx 必须对外发布 80"


def test_compose_app_env_has_no_hardcoded_secret_values():
    """environment: 列表只能是 ``${VAR:-default}`` 形式的占位，不能有任何看起来
    像真实密钥的裸字符串（配置全部外置为环境变量，PRD §3）。"""
    app_block = _service_block((ROOT / "docker-compose.yml").read_text(encoding="utf-8"), "app")
    env_match = re.search(r"^\s+environment:\n(.*?)(?=^\s{4}\S|\Z)", app_block, re.M | re.S)
    assert env_match, "app 服务缺少 environment 块"
    env_lines = [line for line in env_match.group(1).splitlines() if line.strip().startswith("-")]
    assert env_lines, "environment 块里一条变量都没解析到，正则可能没对上缩进"
    for line in env_lines:
        assert "${" in line, f"environment 项必须引用变量，不能写死值：{line!r}"


def test_env_example_has_no_real_looking_secrets():
    """.env.example 每一个 *_API_KEY/*_PASSWORD 行必须留空——这是样例文件，
    不是真实配置（PRD §3「.env.example 不含任何真实密钥」）。"""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.endswith("_API_KEY") or key.endswith("_PASSWORD") or key == "MJ_METRICS_TOKEN":
            assert value == "", f".env.example 的 {key} 必须留空，发现非空值"


def test_docker_entrypoint_is_executable_and_bootstraps_admin():
    entrypoint = ROOT / "scripts" / "docker_entrypoint.sh"
    assert entrypoint.exists()
    mode = entrypoint.stat().st_mode
    assert mode & 0o111, "docker_entrypoint.sh 必须可执行（ENTRYPOINT 直接调用它）"
    text = entrypoint.read_text(encoding="utf-8")
    assert "create_admin.py --from-env" in text
    assert "uvicorn app.main:app" in text


def test_nginx_conf_proxies_to_app_service_not_hardcoded_host():
    text = (ROOT / "deploy" / "nginx" / "default.conf").read_text(encoding="utf-8")
    assert "server app:8230;" in text, "必须用 compose 服务名 app，不是写死 IP/127.0.0.1"
    assert "proxy_buffering off" in text, "SSE 长连接必须关缓冲，否则事件被攒批，见模块注释"
