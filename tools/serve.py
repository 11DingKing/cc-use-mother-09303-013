"""启动国际论坛项目撮合 HTTP 后端。

用法：python3 tools/serve.py --port 8080 --data data/forum.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from matchmaking.api import serve  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="国际论坛项目撮合后端")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default=None, help="JSON 持久化文件路径")
    args = parser.parse_args()
    serve(args.port, args.data)
