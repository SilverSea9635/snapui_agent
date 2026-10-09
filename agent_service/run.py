"""服务启动入口（PM2 指向本文件：`agent_service/run.py`）。

用法：
    uv run python -m agent_service.run
    uv run python agent_service/run.py
    python run.py                # 根目录兼容壳，等价
"""
from __future__ import annotations

import sys
from pathlib import Path

# 保证以脚本方式直接运行时也能正确导入包（PM2 用的是文件路径）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn  # noqa: E402

from agent_service.core.config import settings  # noqa: E402
from agent_service.core.logging import install_redaction  # noqa: E402


def main() -> None:
    # 必须在 uvicorn 之前安装：否则 uvicorn 会用自己的 formatter 覆盖 handler，
    # 导致日志脱敏失效（密钥可能被打进日志）。
    install_redaction(settings.LOG_LEVEL)
    uvicorn.run(
        "agent_service.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
        log_config=None,  # 保留我们装好的脱敏 handler
    )


if __name__ == "__main__":
    main()
