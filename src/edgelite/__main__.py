"""EdgeLite Gateway CLI入口"""

import argparse
import os
import signal
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

# 在 CLI 参数解析之前加载 .env，确保 EDGELITE_SERVER__PORT 等环境变量可用
_env_path = Path(__file__).resolve().parents[2] / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path, override=False)

# FIXED-P0: Windows上aiomqtt需要SelectorEventLoop（ProactorEventLoop不支持add_writer/remove_writer）
# 必须在任何asyncio操作之前设置，否则RuntimeError: event loop is already running
if sys.platform == "win32":
    import asyncio
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:
        pass  # Python < 3.8 不支持此API，忽略


def main():
    """EdgeLite Gateway CLI 入口函数。

    解析命令行参数，注册信号处理器（SIGTERM 优雅关闭、SIGHUP 忽略），
    启动 uvicorn ASGI 服务器。
    """
    parser = argparse.ArgumentParser(description="EdgeLite Gateway")
    parser.add_argument(
        "--host",
        default=os.environ.get("EDGELITE_SERVER__HOST", "127.0.0.1"),
        help="监听地址",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("EDGELITE_SERVER__PORT", "8080")),
        help="监听端口",
    )
    parser.add_argument("--config", default="configs/config.yaml", help="配置文件路径")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    parser.add_argument("--reload-dir", action="append", dest="reload_dirs", help="热重载监控目录")  # FIXED: 原问题-docker-compose.dev.yml使用--reload-dir但argparse未定义
    args = parser.parse_args()

    if args.config:
        os.environ["EDGELITE_CONFIG"] = args.config

    # FIXED-P2: 原问题-未注册SIGTERM处理，Docker/K8s优雅关闭不可靠；注册SIGTERM→触发uvicorn优雅shutdown
    original_sigterm = signal.getsignal(signal.SIGTERM)

    def _sigterm_handler(signum, frame):
        import logging
        logging.getLogger(__name__).info("SIGTERM received, initiating graceful shutdown")
        if callable(original_sigterm):
            original_sigterm(signum, frame)
        else:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # #[AUDIT-FIX] 忽略 SIGHUP 防止 SSH/终端断开导致进程退出
    # 原问题：__main__.py 未处理 SIGHUP，SSH 会话断开时内核向前台进程发送 SIGHUP，
    # 默认行为是终止进程，导致应用"自动退出"且无任何日志（非优雅关闭）。
    # 忽略 SIGHUP 后，进程在终端断开后继续运行，与 nohup 效果一致。
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    uvicorn.run(
        "edgelite.app:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=args.reload_dirs or None,
        factory=True,
    )


if __name__ == "__main__":
    main()
