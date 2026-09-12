#!/bin/sh
# 容器启动入口：建目录 -> 首次启动引导管理员（幂等，见 create_admin.py
# --from-env）-> 起 uvicorn。不在这里跑 scripts/preflight.py 硬拦：自检失败
# 项里有几条（比如"还没配任何模型 Key"）是合法的"先起服务、后台登录再配"
# 工作流，preflight 是给运维主动跑的诊断工具，不是启动闸门——见
# PRD/enterprise/EP-06 §5 与 scripts/preflight.py 模块文档。
set -e

mkdir -p /app/data /app/projects /app/logs

python scripts/create_admin.py --from-env

exec uvicorn app.main:app --host 0.0.0.0 --port 8230 \
    --timeout-keep-alive 330 --timeout-graceful-shutdown 30
