# 一键交付物（PRD/enterprise/EP-06 §3）。两阶段构建：
#   1. frontend  —— node 编译 frontend/dist-staging，产物按 scripts/publish_frontend.py
#      同样的手法搬到 frontend/dist（后端 SpaStaticFiles 固定挂载这个路径，见 app/main.py）
#   2. runtime   —— 只装 requirements.txt 这 7 个依赖，不装 node/前端源码
#
# 镜像内不含数据：data/、projects/、logs/ 一律靠 compose 的具名卷挂载，见
# docker-compose.yml；镜像本身可以随便丢弃重建，状态全部在卷里。
#
# 与现有 systemd 部署路径（scripts/deploy_to_b.sh + B 上的 mjagent2-backend.
# service）互不影响：那条路径不读这个文件、不读 docker-compose.yml，生产在 B
# 上原样跑它的 .venv + systemd，这里新增的容器化路径是给新客户的交付形态。

# ---------------------------------------------------------------------------
# 阶段一：前端构建
# ---------------------------------------------------------------------------
FROM node:20-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
# vite outDir 固定是 dist-staging（2026-08-30 起构建本身无副作用，见
# scripts/publish_frontend.py 模块文档）；这里手动搬到 dist，效果等同于该脚本
# 的"原子替换"一步，只是发生在镜像构建期而不是宿主机上，无需那份脚本本身
# （它的版本偏斜闸门是给"边跑边发布"的宿主机场景设计的，全新镜像构建不存在
# "后端已经用旧代码跑起来"这个前提）。
RUN npm run build && rm -rf dist && mv dist-staging dist

# ---------------------------------------------------------------------------
# 阶段二：运行时
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime
WORKDIR /app

# ffmpeg：分镜/成片阶段的编码依赖；fonts-wqy-zenhei：final_edit 字幕渲染用的
# 中文字体（与 B 上 CJK 字体的用途一致，见 docs/deploy-two-servers.md）。两者
# 都不是本次"一集文本阶段"验收路径必需，但完整交付形态应该装齐，避免客户
# 跑到视频阶段才发现镜像缺东西。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-wqy-zenhei ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY --from=frontend /src/frontend/dist ./frontend/dist

# data/、projects/、logs/ 由 compose 具名卷在运行期挂载到这几个路径；镜像里
# 只建空目录占位，不写入任何数据（PRD"镜像内不含数据"）。
RUN mkdir -p /app/data /app/projects /app/logs

EXPOSE 8230
ENTRYPOINT ["/app/scripts/docker_entrypoint.sh"]
