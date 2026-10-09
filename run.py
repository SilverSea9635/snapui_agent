"""服务启动入口（兼容壳）。

真实入口在 `agent_service/run.py`（PM2 指向那里）。
本文件保留是为了让现有文档里的 `uv run python run.py` 继续可用。

用法：
    uv run python run.py                    # 等价于 python -m agent_service.run
    AGENT_PORT=8001 uv run python run.py    # 进程环境优先，可用于蓝绿换端口
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_service.run import main  # noqa: E402

if __name__ == "__main__":
    main()
