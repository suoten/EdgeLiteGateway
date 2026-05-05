"""EdgeLite Gateway CLI入口"""

import argparse
import os

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="EdgeLite Gateway")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    parser.add_argument("--config", default="configs/config.yaml", help="配置文件路径")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    if args.config:
        os.environ["EDGELITE_CONFIG"] = args.config

    uvicorn.run(
        "edgelite.app:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=True,
    )


if __name__ == "__main__":
    main()
