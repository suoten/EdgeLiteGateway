"""SQLite PRAGMA 辅助函数 — 为原生 sqlite3 连接统一应用标准 PRAGMA 配置。

FIXED: [P0 缺失模块] lifecycle.py:11 导入 `from edgelite.storage.sqlite_pragmas
import apply_standard_pragmas, check_and_convert_to_wal`，但该模块不存在，
导致 bootstrap_engine() 调用 DeviceLifecycleManager 时抛 ImportError，设备
生命周期管理（在线/离线状态持久化）完全失效 [2026-06-30]

设计依据（项目硬约束）:
- 所有 SQLite 连接（含重建）必须配置 WAL 模式、5000ms busy_timeout 和 synchronous=NORMAL
- 与 storage/database.py:_register_sqlite_pragmas 保持一致（该方法是 SQLAlchemy 引擎
  通过 connect 事件注册 PRAGMA；本模块面向原生 sqlite3.connect 返回的连接）
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# 标准超时（毫秒）—— 与 database.py 的 busy_timeout 一致
_BUSY_TIMEOUT_MS = 5000


def apply_standard_pragmas(conn: sqlite3.Connection) -> None:
    """为原生 sqlite3 连接应用标准 PRAGMA 配置。

    必须在连接创建后、执行任何业务 SQL 之前调用。
    幂等：重复调用不会产生副作用。

    配置项（与 database.py:_register_sqlite_pragmas 对齐）:
        - foreign_keys=ON              启用外键约束
        - ignore_check_constraints=OFF 强制执行 CHECK 约束
        - journal_mode=WAL             WAL 模式提升并发读写性能
        - busy_timeout=5000            5 秒锁等待超时，避免 "database is locked"
        - synchronous=NORMAL           WAL 模式下的推荐同步级别（兼顾性能与安全）

    Args:
        conn: sqlite3.connect() 返回的连接。
    """
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA ignore_check_constraints=OFF")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()
    except sqlite3.Error as e:
        # FIXED: [P0 异常静默吞没] PRAGMA 失败需可观测，否则会在后续写入时
        # 以 "database is locked" 等模糊错误暴露，难以定位 [2026-06-30]
        logger.error("Failed to apply standard SQLite PRAGMAs: %s", e)
        raise


def check_and_convert_to_wal(db_path: str) -> None:
    """检查数据库 journal_mode，若非 WAL 则转换为 WAL。

    用于新建/既有数据库的首次连接前校验。WAL 模式必须在没有任何事务持锁时
    才能切换，因此本函数应在连接建立后、业务操作前调用。

    转换流程:
        1. 若数据库文件不存在，跳过（将由后续 connect + apply_standard_pragmas 创建）
        2. 连接数据库，读取当前 journal_mode
        3. 若非 wal，执行 PRAGMA journal_mode=WAL 切换
        4. 关闭连接

    Args:
        db_path: SQLite 数据库文件路径。
    """
    path = Path(db_path)
    if not path.exists():
        # 新数据库：由 apply_standard_pragmas 在首次连接时设置 WAL，此处无需处理
        return

    try:
        conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000.0)
        try:
            cursor = conn.cursor()
            try:
                cursor.execute("PRAGMA journal_mode")
                row = cursor.fetchone()
                current_mode = (row[0] if row else "").lower()
                if current_mode != "wal":
                    logger.info(
                        "Converting SQLite DB %s from %s journal_mode to WAL",
                        db_path,
                        current_mode or "unknown",
                    )
                    cursor.execute("PRAGMA journal_mode=WAL")
                    # WAL 切换需在无活跃事务时生效，commit 确保
                    conn.commit()
            finally:
                cursor.close()
        finally:
            conn.close()
    except sqlite3.Error as e:
        # 转换失败不应阻塞主流程——apply_standard_pragmas 会在新连接上再次尝试设置 WAL
        logger.warning("check_and_convert_to_wal failed for %s: %s", db_path, e)
