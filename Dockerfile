# SnapUI Agent 服务镜像（uv 版）
# 构建上下文为项目根目录：docker build -t snapui-agent .
# 国内构建默认走清华镜像；海外部署可覆盖：
#   docker build --build-arg UV_INDEX_URL=https://pypi.org/simple -t snapui-agent .
FROM python:3.13-slim

# 从官方 uv 镜像拷入 uv 二进制（单文件，无需安装）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 镜像源默认清华（国内快），可用 build-arg 覆盖
ARG UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV UV_DEFAULT_INDEX=${UV_INDEX_URL} \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先复制依赖清单，利用 Docker 层缓存加速后续构建
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 复制应用代码（P0 重写后为 agent_service 包 + 根兼容入口；.env 由 compose 注入，不入镜像）
COPY agent_service ./agent_service
COPY run.py ./

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# 与本地一致的启动入口：按 AGENT_HOST/AGENT_PORT 监听
CMD ["python", "run.py"]
