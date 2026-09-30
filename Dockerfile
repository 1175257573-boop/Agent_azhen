# syntax=docker/dockerfile:1
# =============================================================================
# Atlas 部署镜像 —— 一个镜像，三种角色
#
#   APP_MODULE=server.gateway:app      → API 网关（对外唯一入口）
#   APP_MODULE=server.agent_app:app    → Agent 执行服务
#   APP_MODULE=server.memory_app:app   → 记忆服务
#
# 三个角色共用同一份代码与依赖，只有启动模块和端口不同：
# 镜像只构建一次，compose 里靠环境变量切换角色。
#
# 两阶段构建：
#   1) frontend  node:22-alpine   把 web/ 编成静态产物
#   2) runtime   python:3.12-slim 只装运行时依赖，不带 node / 编译器
#      最终镜像里没有 npm、没有源码里的 node_modules，体积和攻击面都小一截。
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1 · 前端产物
# -----------------------------------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /build

# 先只拷清单再 npm ci：源码变了也能命中 Docker 的依赖层缓存
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY web/tsconfig.json web/vite.config.ts web/index.html ./
COPY web/public ./public
COPY web/src ./src

# build = tsc --noEmit && vite build —— 类型不过关镜像就构建失败，不会带病上线
RUN npm run build

# -----------------------------------------------------------------------------
# Stage 2 · 运行时
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 角色与端口由 compose 注入，默认值 = 网关。
# HEALTH_PATH 默认指 /api/info：那是网关本地应答的接口。
# 绝不能指 /api/health —— 它会级联探测下游，下游一挂就把自己标成不健康，
# 编排器接着重启网关，一次局部故障被放大成整体抖动。
ENV ATLAS_HOME=/var/lib/atlas \
    APP_MODULE=server.gateway:app \
    PORT=8000 \
    HEALTH_PATH=/api/info

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY agent_kit ./agent_kit
COPY server ./server
COPY static ./static
COPY main.py ui.py ./
COPY deploy/healthcheck.py ./healthcheck.py
COPY --from=frontend /build/dist ./web/dist

# 非 root 运行：镜像里不留可写的代码目录，能写的地方只有 ATLAS_HOME
RUN useradd --system --uid 10001 --create-home atlas \
    && mkdir -p "$ATLAS_HOME" \
    && chown -R atlas:atlas /app "$ATLAS_HOME"

VOLUME ["/var/lib/atlas"]
USER atlas
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD ["python", "healthcheck.py"]

CMD ["sh", "-c", "exec uvicorn \"${APP_MODULE}\" --host 0.0.0.0 --port \"${PORT}\""]
