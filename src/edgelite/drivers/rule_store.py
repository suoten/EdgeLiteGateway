"""规则存储 - 基于 SQLite 的规则持久化与版本管理。

提供规则的增删查、版本历史与回滚能力。所有方法为同步（``stop`` 亦同步），
底层使用 ``sqlite3`` 单连接 + WAL 模式，满足驱动侧低频写入场景。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from edgelite.drivers.edge_rule_engine import EdgeRule

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "data/edge_rules.db"


class RuleStore:
    """规则存储（SQLite 后端，线程安全）"""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB_PATH
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        self._db = sqlite3.connect(self._db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS rules (rule_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS rule_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL, version INTEGER NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_rule_versions_rule ON rule_versions(rule_id, version)")
        self._db.commit()

    def load_rules(self) -> list[EdgeRule]:
        from edgelite.drivers.edge_rule_engine import EdgeRule, EdgeRuleOperator, EdgeRuleType
        if not self._db:
            return []
        with self._lock:
            try:
                rows = self._db.execute("SELECT snapshot FROM rules").fetchall()
            except sqlite3.Error as e:
                logger.error("load_rules failed: %s", e)
                return []
        rules: list[EdgeRule] = []
        for (snap_json,) in rows:
            try:
                d = json.loads(snap_json)
                rules.append(EdgeRule(rule_id=d["rule_id"], device_id=d.get("device_id", ""), point_name=d.get("point_name", ""), rule_type=EdgeRuleType(d.get("rule_type", "threshold")), operator=EdgeRuleOperator(d.get("operator", ">")), threshold=float(d.get("threshold", 0)), severity=d.get("severity", "major"), enabled=d.get("enabled", True), cooldown_ms=float(d.get("cooldown_ms", 5000)), duration_ms=float(d.get("duration_ms", 0)), deadband=float(d.get("deadband", 0)), actions=d.get("actions", [])))
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("skip malformed rule snapshot: %s", e)
        return rules
    def save_rule(self, rule: EdgeRule) -> None:
        snap = rule.to_dict()
        snap_json = json.dumps(snap, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self._db.execute("INSERT INTO rules(rule_id, snapshot, updated_at) VALUES(?,?,?) ON CONFLICT(rule_id) DO UPDATE SET snapshot=excluded.snapshot, updated_at=excluded.updated_at", (rule.rule_id, snap_json, _now_iso()))
                version = self._next_version(rule.rule_id)
                self._db.execute("INSERT INTO rule_versions(rule_id, version, snapshot, created_at) VALUES(?,?,?,?)", (rule.rule_id, version, snap_json, _now_iso()))
                self._db.commit()
            except sqlite3.Error as e:
                logger.error("save_rule failed for %s: %s", rule.rule_id, e)
                self._db.rollback()

    def delete_rule(self, rule_id: str) -> None:
        with self._lock:
            try:
                self._db.execute("DELETE FROM rules WHERE rule_id=?", (rule_id,))
                self._db.commit()
            except sqlite3.Error as e:
                logger.error("delete_rule failed for %s: %s", rule_id, e)
                self._db.rollback()

    def rollback(self, rule_id: str, target_version: int) -> EdgeRule | None:
        from edgelite.drivers.edge_rule_engine import EdgeRule, EdgeRuleOperator, EdgeRuleType
        with self._lock:
            try:
                row = self._db.execute("SELECT snapshot FROM rule_versions WHERE rule_id=? AND version=?", (rule_id, target_version)).fetchone()
            except sqlite3.Error as e:
                logger.error("rollback query failed: %s", e)
                return None
        if not row:
            logger.warning("rollback: version %s of %s not found", target_version, rule_id)
            return None
        try:
            d = json.loads(row[0])
            rule = EdgeRule(rule_id=d["rule_id"], device_id=d.get("device_id", ""), point_name=d.get("point_name", ""), rule_type=EdgeRuleType(d.get("rule_type", "threshold")), operator=EdgeRuleOperator(d.get("operator", ">")), threshold=float(d.get("threshold", 0)), severity=d.get("severity", "major"), enabled=d.get("enabled", True), cooldown_ms=float(d.get("cooldown_ms", 5000)), duration_ms=float(d.get("duration_ms", 0)), deadband=float(d.get("deadband", 0)), actions=d.get("actions", []))
            self.save_rule(rule)
            return rule
        except (KeyError, ValueError, TypeError) as e:
            logger.error("rollback restore failed: %s", e)
            return None

    def get_versions(self, rule_id: str) -> list[dict]:
        with self._lock:
            try:
                rows = self._db.execute("SELECT version, created_at FROM rule_versions WHERE rule_id=? ORDER BY version DESC", (rule_id,)).fetchall()
            except sqlite3.Error as e:
                logger.error("get_versions failed: %s", e)
                return []
        return [{"version": v, "created_at": t} for v, t in rows]

    def cleanup_orphan_rules(self, valid_ids: set[str]) -> int:
        if not valid_ids:
            return 0
        with self._lock:
            try:
                placeholders = ",".join("?" * len(valid_ids))
                cur = self._db.execute(f"DELETE FROM rules WHERE rule_id NOT IN ({placeholders})", tuple(valid_ids))
                self._db.commit()
                return cur.rowcount or 0
            except sqlite3.Error as e:
                logger.error("cleanup_orphan_rules failed: %s", e)
                self._db.rollback()
                return 0

    def stop(self) -> None:
        with self._lock:
            if self._db:
                try:
                    self._db.close()
                except sqlite3.Error as e:
                    logger.warning("rule store close error: %s", e)
                self._db = None

    def _next_version(self, rule_id: str) -> int:
        if not self._db:
            return 1
        row = self._db.execute("SELECT COALESCE(MAX(version), 0) FROM rule_versions WHERE rule_id=?", (rule_id,)).fetchone()
        return (row[0] if row else 0) + 1


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


__all__ = ["RuleStore"]
