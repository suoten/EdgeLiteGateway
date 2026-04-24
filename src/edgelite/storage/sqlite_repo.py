"""SQLite仓储实现"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex[:16]


# ─── Device Repository ───


class DeviceRepo:
    """设备数据访问"""

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def create(self, data: dict) -> dict:
        now = _now()
        await self.conn.execute(
            "INSERT INTO devices (device_id, name, protocol, status, config, points, collect_interval, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["device_id"],
                data["name"],
                data["protocol"],
                data.get("status", "offline"),
                json.dumps(data.get("config", {}), ensure_ascii=False),
                json.dumps(data.get("points", []), ensure_ascii=False),
                data.get("collect_interval", 5),
                now,
                now,
            ),
        )
        await self.conn.commit()
        return await self.get(data["device_id"])

    async def get(self, device_id: str) -> dict | None:
        cursor = await self.conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_device(row)

    async def list_all(
        self, page: int = 1, size: int = 20, status: str | None = None, protocol: str | None = None
    ) -> tuple[list[dict], int]:
        where_clauses = []
        params: list[Any] = []
        if status:
            where_clauses.append("status = ?")
            params.append(status)
        if protocol:
            where_clauses.append("protocol = ?")
            params.append(protocol)

        where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # 总数
        cursor = await self.conn.execute(f"SELECT COUNT(*) FROM devices {where}", params)
        total = (await cursor.fetchone())[0]

        # 分页
        offset = (page - 1) * size
        cursor = await self.conn.execute(
            f"SELECT * FROM devices {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [size, offset],
        )
        rows = await cursor.fetchall()
        return [_row_to_device(r) for r in rows], total

    async def update(self, device_id: str, data: dict) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        for key in ("name", "collect_interval"):
            if key in data:
                sets.append(f"{key} = ?")
                params.append(data[key])
        if "config" in data:
            sets.append("config = ?")
            params.append(json.dumps(data["config"], ensure_ascii=False))
        if "points" in data:
            sets.append("points = ?")
            params.append(json.dumps(data["points"], ensure_ascii=False))
        if "status" in data:
            sets.append("status = ?")
            params.append(data["status"])

        if not sets:
            return await self.get(device_id)

        sets.append("updated_at = ?")
        params.append(_now())
        params.append(device_id)

        await self.conn.execute(f"UPDATE devices SET {', '.join(sets)} WHERE device_id = ?", params)
        await self.conn.commit()
        return await self.get(device_id)

    async def update_status(self, device_id: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE devices SET status = ?, updated_at = ? WHERE device_id = ?",
            (status, _now(), device_id),
        )
        await self.conn.commit()

    async def delete(self, device_id: str) -> bool:
        cursor = await self.conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
        await self.conn.commit()
        return cursor.rowcount > 0

    async def list_by_protocol(self, protocol: str) -> list[dict]:
        cursor = await self.conn.execute("SELECT * FROM devices WHERE protocol = ?", (protocol,))
        rows = await cursor.fetchall()
        return [_row_to_device(r) for r in rows]


# ─── Rule Repository ───


class RuleRepo:
    """规则数据访问"""

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def create(self, data: dict) -> dict:
        rule_id = _uuid()
        now = _now()
        await self.conn.execute(
            "INSERT INTO rules (rule_id, name, device_id, conditions, logic, duration, severity, enabled, notify_channels, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rule_id,
                data["name"],
                data["device_id"],
                json.dumps(data["conditions"], ensure_ascii=False),
                data.get("logic", "AND"),
                data.get("duration", 0),
                data["severity"],
                1 if data.get("enabled", True) else 0,
                json.dumps(data.get("notify_channels", []), ensure_ascii=False),
                now,
            ),
        )
        await self.conn.commit()
        return await self.get(rule_id)

    async def get(self, rule_id: str) -> dict | None:
        cursor = await self.conn.execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_rule(row)

    async def list_all(self, page: int = 1, size: int = 20, device_id: str | None = None) -> tuple[list[dict], int]:
        where = "WHERE device_id = ?" if device_id else ""
        params: list[Any] = [device_id] if device_id else []

        cursor = await self.conn.execute(f"SELECT COUNT(*) FROM rules {where}", params)
        total = (await cursor.fetchone())[0]

        offset = (page - 1) * size
        cursor = await self.conn.execute(
            f"SELECT * FROM rules {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [size, offset],
        )
        rows = await cursor.fetchall()
        return [_row_to_rule(r) for r in rows], total

    async def update(self, rule_id: str, data: dict) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        for key in ("name", "logic", "duration", "severity"):
            if key in data:
                sets.append(f"{key} = ?")
                params.append(data[key])
        if "conditions" in data:
            sets.append("conditions = ?")
            params.append(json.dumps(data["conditions"], ensure_ascii=False))
        if "notify_channels" in data:
            sets.append("notify_channels = ?")
            params.append(json.dumps(data["notify_channels"], ensure_ascii=False))
        if "enabled" in data:
            sets.append("enabled = ?")
            params.append(1 if data["enabled"] else 0)

        if not sets:
            return await self.get(rule_id)

        params.append(rule_id)
        await self.conn.execute(f"UPDATE rules SET {', '.join(sets)} WHERE rule_id = ?", params)
        await self.conn.commit()
        return await self.get(rule_id)

    async def toggle(self, rule_id: str, enabled: bool) -> dict | None:
        return await self.update(rule_id, {"enabled": enabled})

    async def delete(self, rule_id: str) -> bool:
        cursor = await self.conn.execute("DELETE FROM rules WHERE rule_id = ?", (rule_id,))
        await self.conn.commit()
        return cursor.rowcount > 0

    async def list_by_device(self, device_id: str) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM rules WHERE device_id = ? AND enabled = 1", (device_id,)
        )
        rows = await cursor.fetchall()
        return [_row_to_rule(r) for r in rows]

    async def list_enabled_by_point(self, device_id: str, point_name: str) -> list[dict]:
        """查找关联指定设备+测点的所有启用规则"""
        cursor = await self.conn.execute(
            "SELECT * FROM rules WHERE device_id = ? AND enabled = 1", (device_id,)
        )
        rows = await cursor.fetchall()
        rules = [_row_to_rule(r) for r in rows]
        # 过滤包含指定测点的规则
        return [r for r in rules if any(c["point"] == point_name for c in r["conditions"])]


# ─── Alarm Repository ───


class AlarmRepo:
    """告警数据访问"""

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def create(self, data: dict) -> dict:
        alarm_id = _uuid()
        now = _now()
        await self.conn.execute(
            "INSERT INTO alarms (alarm_id, rule_id, device_id, severity, status, trigger_value, trigger_count, fired_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                alarm_id,
                data["rule_id"],
                data["device_id"],
                data["severity"],
                "firing",
                json.dumps(data.get("trigger_value", {}), ensure_ascii=False),
                1,
                now,
            ),
        )
        await self.conn.commit()
        return await self.get(alarm_id)

    async def get(self, alarm_id: str) -> dict | None:
        cursor = await self.conn.execute("SELECT * FROM alarms WHERE alarm_id = ?", (alarm_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_alarm(row)

    async def list_all(
        self,
        page: int = 1,
        size: int = 20,
        status: str | None = None,
        severity: str | None = None,
        device_id: str | None = None,
    ) -> tuple[list[dict], int]:
        where_clauses = []
        params: list[Any] = []
        if status:
            where_clauses.append("status = ?")
            params.append(status)
        if severity:
            where_clauses.append("severity = ?")
            params.append(severity)
        if device_id:
            where_clauses.append("device_id = ?")
            params.append(device_id)

        where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        cursor = await self.conn.execute(f"SELECT COUNT(*) FROM alarms {where}", params)
        total = (await cursor.fetchone())[0]

        offset = (page - 1) * size
        cursor = await self.conn.execute(
            f"SELECT * FROM alarms {where} ORDER BY fired_at DESC LIMIT ? OFFSET ?",
            params + [size, offset],
        )
        rows = await cursor.fetchall()
        return [_row_to_alarm(r) for r in rows], total

    async def ack(self, alarm_id: str, ack_by: str) -> dict | None:
        now = _now()
        await self.conn.execute(
            "UPDATE alarms SET status = 'acknowledged', acknowledged_at = ?, acknowledged_by = ? WHERE alarm_id = ?",
            (now, ack_by, alarm_id),
        )
        await self.conn.commit()
        return await self.get(alarm_id)

    async def recover(self, alarm_id: str) -> dict | None:
        now = _now()
        await self.conn.execute(
            "UPDATE alarms SET status = 'recovered', recovered_at = ? WHERE alarm_id = ?",
            (now, alarm_id),
        )
        await self.conn.commit()
        return await self.get(alarm_id)

    async def update_trigger_count(self, alarm_id: str, trigger_value: dict) -> None:
        await self.conn.execute(
            "UPDATE alarms SET trigger_count = trigger_count + 1, trigger_value = ? WHERE alarm_id = ?",
            (json.dumps(trigger_value, ensure_ascii=False), alarm_id),
        )
        await self.conn.commit()

    async def get_firing_by_rule_device(self, rule_id: str, device_id: str) -> dict | None:
        """获取指定规则+设备的firing告警"""
        cursor = await self.conn.execute(
            "SELECT * FROM alarms WHERE rule_id = ? AND device_id = ? AND status = 'firing'",
            (rule_id, device_id),
        )
        row = await cursor.fetchone()
        return _row_to_alarm(row) if row else None


# ─── User Repository ───


class UserRepo:
    """用户数据访问"""

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def create(self, data: dict) -> dict:
        user_id = _uuid()
        now = _now()
        await self.conn.execute(
            "INSERT INTO users (user_id, username, password, role, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, data["username"], data["password"], data["role"], 1, now),
        )
        await self.conn.commit()
        return await self.get(user_id)

    async def get(self, user_id: str) -> dict | None:
        cursor = await self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return _row_to_user(row) if row else None

    async def get_by_username(self, username: str) -> dict | None:
        cursor = await self.conn.execute("SELECT * FROM users WHERE username = ?", (username,))
        row = await cursor.fetchone()
        return _row_to_user(row) if row else None

    async def list_all(self, page: int = 1, size: int = 20) -> tuple[list[dict], int]:
        cursor = await self.conn.execute("SELECT COUNT(*) FROM users")
        total = (await cursor.fetchone())[0]

        offset = (page - 1) * size
        cursor = await self.conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?", (size, offset)
        )
        rows = await cursor.fetchall()
        return [_row_to_user(r) for r in rows], total

    async def update(self, user_id: str, data: dict) -> dict | None:
        sets: list[str] = []
        params: list[Any] = []
        for key in ("password", "role"):
            if key in data:
                sets.append(f"{key} = ?")
                params.append(data[key])
        if "enabled" in data:
            sets.append("enabled = ?")
            params.append(1 if data["enabled"] else 0)

        if not sets:
            return await self.get(user_id)

        params.append(user_id)
        await self.conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?", params)
        await self.conn.commit()
        return await self.get(user_id)

    async def delete(self, user_id: str) -> bool:
        cursor = await self.conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await self.conn.commit()
        return cursor.rowcount > 0


# ─── Audit Repository ───


class AuditRepo:
    """审计日志数据访问"""

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def create(self, data: dict) -> None:
        await self.conn.execute(
            "INSERT INTO audit_logs (user_id, action, resource, resource_id, detail, result, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                data["user_id"],
                data["action"],
                data["resource"],
                data.get("resource_id"),
                json.dumps(data.get("detail"), ensure_ascii=False) if data.get("detail") else None,
                data["result"],
                _now(),
            ),
        )
        await self.conn.commit()


# ─── Row转换辅助 ───


def _row_to_device(row: aiosqlite.Row) -> dict:
    return {
        "device_id": row["device_id"],
        "name": row["name"],
        "protocol": row["protocol"],
        "status": row["status"],
        "config": json.loads(row["config"]),
        "points": json.loads(row["points"]),
        "collect_interval": row["collect_interval"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_rule(row: aiosqlite.Row) -> dict:
    return {
        "rule_id": row["rule_id"],
        "name": row["name"],
        "device_id": row["device_id"],
        "conditions": json.loads(row["conditions"]),
        "logic": row["logic"],
        "duration": row["duration"],
        "severity": row["severity"],
        "enabled": bool(row["enabled"]),
        "notify_channels": json.loads(row["notify_channels"]),
        "created_at": row["created_at"],
    }


def _row_to_alarm(row: aiosqlite.Row) -> dict:
    return {
        "alarm_id": row["alarm_id"],
        "rule_id": row["rule_id"],
        "device_id": row["device_id"],
        "severity": row["severity"],
        "status": row["status"],
        "trigger_value": json.loads(row["trigger_value"]),
        "trigger_count": row["trigger_count"],
        "fired_at": row["fired_at"],
        "acknowledged_at": row["acknowledged_at"],
        "acknowledged_by": row["acknowledged_by"],
        "recovered_at": row["recovered_at"],
    }


def _row_to_user(row: aiosqlite.Row) -> dict:
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "password": row["password"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }
