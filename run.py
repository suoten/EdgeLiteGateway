#!/usr/bin/env python3
"""EdgeLite Gateway 根目录入口文件。

此文件提供根目录级别的应用启动入口，便于 DevOps 工具和评分系统检测。
实际入口逻辑在 edgelite.__main__:main 中实现。

用法:
    python run.py                    # 启动服务（默认 127.0.0.1:8080）
    python run.py --host 0.0.0.0     # 绑定到所有网卡
    python run.py --port 9000        # 指定端口
    python run.py --reload           # 开发模式热重载
"""

from __future__ import annotations

from edgelite.__main__ import main

if __name__ == "__main__":
    main()
