"""Modbus 配置版本管理模块

为 modbus_tcp.py 和 modbus_rtu.py 提供设备配置的版本快照、回滚、对比与导入导出能力。
基于 SQLite（sqlite3，同步）持久化存储，启用 WAL 模式以支持并发读取。

数据模型：
- config_versions: 配置版本快照主表（id 自增即版本号）
- config_audit_trail: 配置变更审计流水

兼容性说明：
- TCP 调用 snapshot_device_config(device_id, config) 不传 operator、不用返回值；
  RTU 调用 snapshot_device_config(device_id, config, operator=operator) 使用返回的版本号。
- rollback 返回统一 dict：{"device_id": ..., "config": {...}, "version": ...}；
  TCP 的 rollback_config_version 直接 return 该 dict；
  RTU 解包 device_id/config 字段后再使用。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_CREATE_CONFIG_VERSIONS_SQL = """
CREATE TABLE IF NOT EXISTS config_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    config_json TEXT NOT NULL,
    change_summary TEXT,
    operator TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL
)
"""

_CREATE_AUDIT_TRAIL_SQL = """
CREATE TABLE IF NOT EXISTS config_audit_trail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER,
    device_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    operator TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL
)
"""

_CREATE_INDEX_DEVICE_SQL = """
CREATE INDEX IF NOT EXISTS idx_config_versions_device
ON config_versions (device_id, id)
"""


class ModbusConfigVersion:
    """Modbus 配置版本管理器（基于 SQLite，全部同步方法）"""

    def __init__(self) -> None:
        self._db_path = "data/modbus_config_versions.db"
        # 确保数据目录存在
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False 允许跨线程使用（配合 _lock 保护）
        self._conn = sqlite3.connect(
            self._db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        # WAL 模式 + 高并发友好配置
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """建表与索引（幂等）"""
        with self._lock:
            self._conn.execute(_CREATE_CONFIG_VERSIONS_SQL)
            self._conn.execute(_CREATE_AUDIT_TRAIL_SQL)
            self._conn.execute(_CREATE_INDEX_DEVICE_SQL)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    def snapshot_device_config(
        self,
        device_id: str,
        config: dict[str, Any],
        operator: str = "system",
    ) -> int:
        """保存配置快照，返回版本号（自增 id）

        TCP 调用：snapshot_device_config(device_id, config)  # 不传 operator、不用返回值
        RTU 调用：snapshot_device_config(device_id, config, operator=operator)  # 使用返回值
        """
        config_json = json.dumps(config, ensure_ascii=False, default=str)
        now = self._now_iso()
        # change_summary 简要记录顶层 key
        try:
            summary = ",".join(list(config.keys())[:20])
        except Exception:
            summary = ""
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO config_versions
                   (device_id, config_json, change_summary, operator, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (device_id, config_json, summary, operator, now),
            )
            version_id = cursor.lastrowid or 0
            # 写审计流水
            self._conn.execute(
                """INSERT INTO config_audit_trail
                   (version_id, device_id, action, detail, operator, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (version_id, device_id, "snapshot", summary, operator, now),
            )
        logger.debug(
            "[modbus_config_version] snapshot device=%s version=%d operator=%s",
            device_id, version_id, operator,
        )
        return version_id
    # ------------------------------------------------------------------
    # 深度对比
    # ------------------------------------------------------------------
    def _deep_diff_keys(self, old: dict[str, Any], new: dict[str, Any]) -> list[str]:
        """深度对比两个 config dict，返回变更的 key 列表

        嵌套 dict 以 "parent.child" 路径表示；仅 RTU 调用。
        """
        changed: list[str] = []

        def _walk(o: Any, n: Any, prefix: str) -> None:
            if isinstance(o, dict) and isinstance(n, dict):
                all_keys = set(o.keys()) | set(n.keys())
                for k in all_keys:
                    path = f"{prefix}.{k}" if prefix else str(k)
                    ov = o.get(k)
                    nv = n.get(k) if k in n else None
                    if k not in o or k not in n:
                        changed.append(path)
                    elif isinstance(ov, dict) and isinstance(nv, dict):
                        _walk(ov, nv, path)
                    elif ov != nv:
                        changed.append(path)
            else:
                if o != n:
                    if prefix:
                        changed.append(prefix)

        _walk(old or {}, new or {}, "")
        return changed

    # ------------------------------------------------------------------
    # 回滚与查询
    # ------------------------------------------------------------------
    def rollback(self, version: int) -> dict[str, Any] | None:
        """回滚到指定版本，返回统一 dict 格式

        返回：{"device_id": ..., "config": {...}, "version": ...}
        - TCP 的 rollback_config_version 直接 return 该 dict
        - RTU 解包 device_id/config 字段后再使用
        不存在则返回 None。
        """
        with self._lock:
            row = self._conn.execute(
                """SELECT id, device_id, config_json, operator, created_at
                   FROM config_versions WHERE id = ?""",
                (version,),
            ).fetchone()
        if row is None:
            logger.warning("[modbus_config_version] rollback 版本 %s 不存在", version)
            return None
        try:
            config = json.loads(row["config_json"])
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("[modbus_config_version] 版本 %s config 解析失败: %s", version, e)
            return None
        # 记录回滚审计
        now = self._now_iso()
        with self._lock:
            self._conn.execute(
                """INSERT INTO config_audit_trail
                   (version_id, device_id, action, detail, operator, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (version, row["device_id"], "rollback", f"to v{version}", row["operator"], now),
            )
        return {
            "device_id": row["device_id"],
            "config": config,
            "version": row["id"],
        }

    def list_versions(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """列出版本（按 id 倒序，不含完整 config_json）"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, device_id, change_summary, operator, created_at
                   FROM config_versions
                   ORDER BY id DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [
            {
                "version": r["id"],
                "device_id": r["device_id"],
                "change_summary": r["change_summary"],
                "operator": r["operator"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def get_version(self, version: int) -> dict[str, Any] | None:
        """获取指定版本完整信息（含 config）"""
        with self._lock:
            row = self._conn.execute(
                """SELECT id, device_id, config_json, change_summary, operator, created_at
                   FROM config_versions WHERE id = ?""",
                (version,),
            ).fetchone()
        if row is None:
            return None
        try:
            config = json.loads(row["config_json"])
        except (json.JSONDecodeError, TypeError):
            config = {}
        return {
            "version": row["id"],
            "device_id": row["device_id"],
            "config": config,
            "change_summary": row["change_summary"],
            "operator": row["operator"],
            "created_at": row["created_at"],
        }
    # ------------------------------------------------------------------
    # 版本对比
    # ------------------------------------------------------------------
    def diff_versions(self, v1: int, v2: int) -> dict[str, Any] | None:
        """对比两个版本的 config 差异"""
        c1 = self.get_version(v1)
        c2 = self.get_version(v2)
        if c1 is None or c2 is None:
            return None
        cfg1 = c1.get("config", {})
        cfg2 = c2.get("config", {})
        all_keys = set(cfg1.keys()) | set(cfg2.keys())
        added = sorted(k for k in all_keys if k not in cfg1)
        removed = sorted(k for k in all_keys if k not in cfg2)
        changed = sorted(k for k in all_keys if k in cfg1 and k in cfg2 and cfg1[k] != cfg2[k])
        return {
            "v1": v1,
            "v2": v2,
            "added": added,
            "removed": removed,
            "changed": changed,
            "v1_config": cfg1,
            "v2_config": cfg2,
        }

    # ------------------------------------------------------------------
    # 导入导出
    # ------------------------------------------------------------------
    def export_json(self) -> str:
        """导出全部版本为 JSON 字符串"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, device_id, config_json, change_summary, operator, created_at
                   FROM config_versions ORDER BY id ASC"""
            ).fetchall()
        versions = []
        for r in rows:
            try:
                config = json.loads(r["config_json"])
            except (json.JSONDecodeError, TypeError):
                config = {}
            versions.append({
                "version": r["id"],
                "device_id": r["device_id"],
                "config": config,
                "change_summary": r["change_summary"],
                "operator": r["operator"],
                "created_at": r["created_at"],
            })
        return json.dumps(
            {"versions": versions, "exported_at": self._now_iso()},
            ensure_ascii=False,
            default=str,
        )

    def export_yaml(self) -> str:
        """导出全部版本为 YAML 字符串（仅 TCP 调用）

        优先使用 PyYAML，不可用时回退到 JSON。
        """
        json_str = self.export_json()
        try:
            import yaml  # type: ignore
            data = json.loads(json_str)
            return yaml.safe_dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
        except Exception as e:
            logger.warning("[modbus_config_version] export_yaml 回退到 JSON: %s", e)
            return json_str

    def import_json(self, data: str) -> bool:
        """从 JSON 字符串导入版本记录

        data 为 export_json 的输出格式。导入时重新写入快照（不覆盖已有 id）。
        """
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("[modbus_config_version] import_json 解析失败: %s", e)
            return False
        versions = payload.get("versions") if isinstance(payload, dict) else payload
        if not isinstance(versions, list):
            logger.error("[modbus_config_version] import_json 格式无效")
            return False
        now = self._now_iso()
        with self._lock:
            for v in versions:
                if not isinstance(v, dict):
                    continue
                config = v.get("config", {})
                config_json = json.dumps(config, ensure_ascii=False, default=str)
                device_id = v.get("device_id", "")
                operator = v.get("operator", "system")
                summary = v.get("change_summary", "")
                self._conn.execute(
                    """INSERT INTO config_versions
                       (device_id, config_json, change_summary, operator, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (device_id, config_json, summary, operator, now),
                )
        logger.info("[modbus_config_version] import_json 导入 %d 条版本", len(versions))
        return True

    # ------------------------------------------------------------------
    # 完整性校验
    # ------------------------------------------------------------------
    def verify_integrity(self, version: int) -> bool:
        """验证指定版本完整性（仅 RTU 调用）

        检查：版本存在、config_json 可解析为 dict、含至少一个字段。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT config_json FROM config_versions WHERE id = ?",
                (version,),
            ).fetchone()
        if row is None:
            return False
        try:
            config = json.loads(row["config_json"])
        except (json.JSONDecodeError, TypeError):
            return False
        return isinstance(config, dict) and len(config) >= 1

    def close(self) -> None:
        """关闭数据库连接"""
        with self._lock:
            try:
                self._conn.close()
            except Exception as e:
                logger.warning("[modbus_config_version] 关闭连接失败: %s", e)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
