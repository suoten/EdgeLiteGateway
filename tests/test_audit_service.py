"""审计日志服务测试 - 防篡改哈希链 + 敏感字段脱敏 + 异常登录检测

覆盖 services/audit_service.py：
- AuditAction 枚举完整性
- AuditService: initialize / log / query / verify_integrity / export_csv / cleanup
- _mask_sensitive: 敏感字段递归脱敏
- _compute_record_hash: 哈希链计算
- _check_login_anomaly: 异常登录检测 + 持久化
- append-only 触发器: 阻止 UPDATE/DELETE
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from edgelite.services.audit_service import AuditAction, AuditService


@pytest.fixture
async def audit_svc(tmp_path):
    """创建临时 AuditService 实例"""
    db_path = str(tmp_path / "audit.db")
    svc = AuditService(db_path=db_path, tamper_proof=True)
    await svc.initialize()
    yield svc
    await svc.close()


@pytest.fixture
async def audit_svc_no_tamper(tmp_path):
    """创建无防篡改的 AuditService 实例"""
    db_path = str(tmp_path / "audit_no_tamper.db")
    svc = AuditService(db_path=db_path, tamper_proof=False)
    await svc.initialize()
    yield svc
    await svc.close()


class TestAuditActionEnum:
    def test_login_actions_exist(self):
        """登录相关审计动作应存在"""
        assert AuditAction.LOGIN.value == "login"
        assert AuditAction.LOGOUT.value == "logout"
        assert AuditAction.LOGIN_FAILED.value == "login_failed"
        assert AuditAction.TOKEN_REFRESH.value == "token_refresh"

    def test_device_actions_exist(self):
        """设备相关审计动作应存在"""
        assert AuditAction.DEVICE_CREATE.value == "device_create"
        assert AuditAction.DEVICE_DELETE.value == "device_delete"
        assert AuditAction.DEVICE_WRITE_POINT.value == "device_write_point"

    def test_ota_actions_exist(self):
        """OTA 相关审计动作应存在"""
        assert AuditAction.OTA_START.value == "ota_start"
        assert AuditAction.OTA_COMPLETED.value == "ota_completed"
        assert AuditAction.OTA_FAILED.value == "ota_failed"
        assert AuditAction.OTA_ROLLBACK.value == "ota_rollback"

    def test_password_reset_actions_exist(self):
        """密码重置相关审计动作应存在"""
        assert AuditAction.PASSWORD_RESET_USED.value == "password_reset_used"
        assert AuditAction.PASSWORD_RESET_REUSED.value == "password_reset_reused"
        assert AuditAction.FORGOT_PASSWORD_RATE_LIMITED.value == "forgot_password_rate_limited"

    def test_account_lockout_actions_exist(self):
        """账户锁定相关审计动作应存在"""
        assert AuditAction.ACCOUNT_LOCKED.value == "account_locked"
        assert AuditAction.ACCOUNT_UNLOCKED.value == "account_unlocked"


class TestAuditServiceInit:
    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self, tmp_path):
        """initialize 应创建 audit_logs 表"""
        svc = AuditService(db_path=str(tmp_path / "audit.db"))
        await svc.initialize()
        assert svc._initialized is True
        assert svc._conn is not None
        await svc.close()

    @pytest.mark.asyncio
    async def test_initialize_loads_last_hash(self, tmp_path):
        """防篡改模式应加载最后一条记录的 hash"""
        db_path = str(tmp_path / "audit.db")
        svc1 = AuditService(db_path=db_path, tamper_proof=True)
        await svc1.initialize()
        await svc1.log(AuditAction.LOGIN, user_id="u1", username="admin")
        assert svc1._last_hash != ""
        await svc1.close()

        # 重新初始化应加载 last_hash
        svc2 = AuditService(db_path=db_path, tamper_proof=True)
        await svc2.initialize()
        assert svc2._last_hash == svc1._last_hash
        await svc2.close()

    @pytest.mark.asyncio
    async def test_close_resets_state(self, audit_svc):
        """close 应重置初始化状态"""
        await audit_svc.close()
        assert audit_svc._initialized is False
        assert audit_svc._conn is None


class TestMaskSensitive:
    def test_masks_password_field(self):
        """password 字段应被脱敏"""
        data = {"username": "admin", "password": "secret123"}
        masked = AuditService._mask_sensitive(data)
        assert masked["username"] == "admin"
        assert masked["password"] == "***REDACTED***"

    def test_masks_token_field(self):
        """token 字段应被脱敏"""
        data = {"access_token": "abc.def.ghi"}
        masked = AuditService._mask_sensitive(data)
        assert masked["access_token"] == "***REDACTED***"

    def test_masks_nested_dict(self):
        """嵌套字典中的敏感字段应被脱敏"""
        data = {"user": {"name": "admin", "password": "secret"}}
        masked = AuditService._mask_sensitive(data)
        assert masked["user"]["name"] == "admin"
        assert masked["user"]["password"] == "***REDACTED***"

    def test_masks_list_items(self):
        """列表中的敏感字段应被脱敏"""
        data = [{"password": "p1"}, {"name": "ok"}]
        masked = AuditService._mask_sensitive(data)
        assert masked[0]["password"] == "***REDACTED***"
        assert masked[1]["name"] == "ok"

    def test_non_dict_returns_as_is(self):
        """非字典/列表应原样返回"""
        assert AuditService._mask_sensitive("string") == "string"
        assert AuditService._mask_sensitive(123) == 123

    def test_masks_api_key_field(self):
        """api_key 字段应被脱敏"""
        data = {"api_key": "sk-12345"}
        masked = AuditService._mask_sensitive(data)
        assert masked["api_key"] == "***REDACTED***"

    def test_masks_credential_variants(self):
        """credential/credentials 字段应被脱敏"""
        data = {"credential": "c1", "credentials": "c2", "client_secret": "s1"}
        masked = AuditService._mask_sensitive(data)
        assert masked["credential"] == "***REDACTED***"
        assert masked["credentials"] == "***REDACTED***"
        assert masked["client_secret"] == "***REDACTED***"


class TestComputeRecordHash:
    def test_hash_includes_all_fields(self):
        """哈希应包含所有关键字段"""
        svc = AuditService(tamper_proof=False)
        record = {
            "created_at": "2024-01-01T00:00:00",
            "user_id": "u1",
            "username": "admin",
            "action": "login",
            "resource_type": "",
            "resource_id": "",
            "ip_address": "1.2.3.4",
            "status": "success",
            "user_agent": "",
            "details_json": "{}",
            "error_message": "",
            "before_value_json": "",
            "after_value_json": "",
        }
        h1 = svc._compute_record_hash(record, "")
        h2 = svc._compute_record_hash(record, "")
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex

    def test_hash_changes_with_prev_hash(self):
        """不同 prev_hash 应产生不同 record_hash"""
        svc = AuditService(tamper_proof=False)
        record = {
            "created_at": "2024-01-01",
            "user_id": "u1",
            "username": "admin",
            "action": "login",
            "resource_type": "",
            "resource_id": "",
            "ip_address": "",
            "status": "success",
            "user_agent": "",
            "details_json": "",
            "error_message": "",
            "before_value_json": "",
            "after_value_json": "",
        }
        h1 = svc._compute_record_hash(record, "prev1")
        h2 = svc._compute_record_hash(record, "prev2")
        assert h1 != h2


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_log_creates_record(self, audit_svc):
        """log 应创建审计记录"""
        await audit_svc.log(
            AuditAction.LOGIN,
            user_id="u1",
            username="admin",
            ip_address="1.2.3.4",
        )
        rows, total = await audit_svc.query()
        assert total == 1
        assert rows[0]["action"] == "login"
        assert rows[0]["user_id"] == "u1"

    @pytest.mark.asyncio
    async def test_log_with_details(self, audit_svc):
        """log 应存储 details JSON"""
        await audit_svc.log(
            AuditAction.DEVICE_CREATE,
            user_id="u1",
            username="admin",
            details={"device_id": "dev1", "name": "Sensor1"},
        )
        rows, _ = await audit_svc.query()
        assert rows[0]["action"] == "device_create"
        details = json.loads(rows[0]["details"])
        assert details["device_id"] == "dev1"

    @pytest.mark.asyncio
    async def test_log_masks_sensitive_in_details(self, audit_svc):
        """details 中的敏感字段应被脱敏"""
        await audit_svc.log(
            AuditAction.USER_CREATE,
            user_id="u1",
            username="admin",
            details={"username": "newuser", "password": "plaintext_secret"},
        )
        rows, _ = await audit_svc.query()
        details = json.loads(rows[0]["details"])
        assert details["username"] == "newuser"
        assert details["password"] == "***REDACTED***"

    @pytest.mark.asyncio
    async def test_log_with_before_after_values(self, audit_svc):
        """log 应存储 before_value/after_value"""
        await audit_svc.log(
            AuditAction.CONFIG_UPDATE,
            user_id="u1",
            username="admin",
            before_value={"setting": "old"},
            after_value={"setting": "new"},
        )
        rows, _ = await audit_svc.query()
        before = json.loads(rows[0]["before_value"])
        after = json.loads(rows[0]["after_value"])
        assert before["setting"] == "old"
        assert after["setting"] == "new"

    @pytest.mark.asyncio
    async def test_log_failed_status(self, audit_svc):
        """log 应支持 failed 状态"""
        await audit_svc.log(
            AuditAction.LOGIN_FAILED,
            username="baduser",
            ip_address="1.2.3.4",
            status="failed",
        )
        rows, _ = await audit_svc.query()
        assert rows[0]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_log_no_tamper_proof(self, audit_svc_no_tamper):
        """无防篡改模式不应计算 hash"""
        await audit_svc_no_tamper.log(AuditAction.LOGIN, user_id="u1")
        rows, _ = await audit_svc_no_tamper.query()
        assert len(rows) == 1


class TestAuditQuery:
    @pytest.mark.asyncio
    async def test_query_filter_by_action(self, audit_svc):
        """应支持按 action 过滤"""
        await audit_svc.log(AuditAction.LOGIN, user_id="u1")
        await audit_svc.log(AuditAction.LOGOUT, user_id="u1")
        rows, total = await audit_svc.query(action=AuditAction.LOGIN)
        assert total == 1
        assert rows[0]["action"] == "login"

    @pytest.mark.asyncio
    async def test_query_filter_by_user_id(self, audit_svc):
        """应支持按 user_id 过滤"""
        await audit_svc.log(AuditAction.LOGIN, user_id="u1")
        await audit_svc.log(AuditAction.LOGIN, user_id="u2")
        rows, total = await audit_svc.query(user_id="u2")
        assert total == 1
        assert rows[0]["user_id"] == "u2"

    @pytest.mark.asyncio
    async def test_query_pagination(self, audit_svc):
        """应支持分页"""
        for i in range(10):
            await audit_svc.log(AuditAction.LOGIN, user_id=f"u{i}")
        rows_page1, total = await audit_svc.query(page=1, size=5)
        assert total == 10
        assert len(rows_page1) == 5
        rows_page2, _ = await audit_svc.query(page=2, size=5)
        assert len(rows_page2) == 5

    @pytest.mark.asyncio
    async def test_query_empty(self, audit_svc):
        """空查询应返回空列表"""
        rows, total = await audit_svc.query()
        assert rows == []
        assert total == 0


class TestAuditVerifyIntegrity:
    @pytest.mark.asyncio
    async def test_verify_integrity_valid(self, audit_svc):
        """完整哈希链应通过校验"""
        for i in range(5):
            await audit_svc.log(AuditAction.LOGIN, user_id=f"u{i}")
        result = await audit_svc.verify_integrity()
        assert result["valid"] is True
        assert result["total"] == 5
        assert result["broken_at"] == []

    @pytest.mark.asyncio
    async def test_verify_integrity_empty(self, audit_svc):
        """空表应通过校验"""
        result = await audit_svc.verify_integrity()
        assert result["valid"] is True
        assert result["total"] == 0


class TestAuditExportCsv:
    @pytest.mark.asyncio
    async def test_export_csv_contains_headers(self, audit_svc):
        """CSV 导出应包含表头"""
        await audit_svc.log(AuditAction.LOGIN, user_id="u1", username="admin")
        csv_text = await audit_svc.export_csv()
        reader = csv.reader(io.StringIO(csv_text))
        headers = next(reader)
        assert "action" in headers
        assert "user_id" in headers

    @pytest.mark.asyncio
    async def test_export_csv_contains_rows(self, audit_svc):
        """CSV 导出应包含数据行"""
        await audit_svc.log(AuditAction.LOGIN, user_id="u1")
        await audit_svc.log(AuditAction.LOGOUT, user_id="u1")
        csv_text = await audit_svc.export_csv()
        reader = csv.reader(io.StringIO(csv_text))
        next(reader)  # skip header
        rows = list(reader)
        assert len(rows) == 2


class TestAuditAppendOnly:
    @pytest.mark.asyncio
    async def test_update_blocked_by_trigger(self, audit_svc):
        """append-only 触发器应阻止 UPDATE"""
        await audit_svc.log(AuditAction.LOGIN, user_id="u1")
        # 直接尝试 UPDATE 应被触发器阻止
        with pytest.raises(sqlite3.IntegrityError):
            cursor = audit_svc._conn.cursor()
            cursor.execute("UPDATE audit_logs SET user_id = 'hacked' WHERE id = 1")
            audit_svc._conn.commit()

    @pytest.mark.asyncio
    async def test_delete_blocked_by_trigger(self, audit_svc):
        """append-only 触发器应阻止 DELETE"""
        await audit_svc.log(AuditAction.LOGIN, user_id="u1")
        with pytest.raises(sqlite3.IntegrityError):
            cursor = audit_svc._conn.cursor()
            cursor.execute("DELETE FROM audit_logs WHERE id = 1")
            audit_svc._conn.commit()


class TestAuditCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_removes_old_records(self, tmp_path):
        """cleanup 应删除超过保留期的记录"""
        svc = AuditService(db_path=str(tmp_path / "audit.db"))
        await svc.initialize()
        # 插入一条 100 天前的记录
        old_time = (datetime.now(UTC) - timedelta(days=100)).isoformat()
        await svc.log(AuditAction.LOGIN, user_id="u1")
        # 手动修改 created_at（通过临时禁用触发器）
        conn = svc._conn
        conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
        conn.execute("UPDATE audit_logs SET created_at = ? WHERE id = 1", (old_time,))
        conn.execute(
            "CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_logs FOR EACH ROW "
            "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only: UPDATE not allowed'); END"
        )
        conn.commit()

        deleted = await svc.cleanup(retention_days=30)
        assert deleted == 1
        rows, total = await svc.query()
        assert total == 0
        await svc.close()


class TestLoginAnomaly:
    @pytest.mark.asyncio
    async def test_login_failures_trigger_alert(self, tmp_path):
        """连续登录失败达到阈值应触发告警"""
        svc = AuditService(db_path=str(tmp_path / "audit.db"))
        await svc.initialize()
        alerts = []

        async def alert_callback(info):
            alerts.append(info)

        svc.set_alert_callback(alert_callback)
        svc._login_fail_threshold = 3

        for _ in range(3):
            await svc._check_login_anomaly("1.2.3.4", "admin")

        assert len(alerts) == 1
        assert alerts[0]["type"] == "login_anomaly"
        assert alerts[0]["ip_address"] == "1.2.3.4"
        await svc.close()

    @pytest.mark.asyncio
    async def test_login_failures_below_threshold_no_alert(self, tmp_path):
        """未达阈值不应触发告警"""
        svc = AuditService(db_path=str(tmp_path / "audit.db"))
        await svc.initialize()
        alerts = []

        async def alert_callback(info):
            alerts.append(info)

        svc.set_alert_callback(alert_callback)
        svc._login_fail_threshold = 5

        for _ in range(3):
            await svc._check_login_anomaly("1.2.3.4", "admin")

        assert len(alerts) == 0
        await svc.close()

    @pytest.mark.asyncio
    async def test_login_failures_reset_after_alert(self, tmp_path):
        """告警后计数器应重置"""
        svc = AuditService(db_path=str(tmp_path / "audit.db"))
        await svc.initialize()
        alerts = []

        async def alert_callback(info):
            alerts.append(info)

        svc.set_alert_callback(alert_callback)
        svc._login_fail_threshold = 3

        # 第一次达到阈值，触发告警
        for _ in range(3):
            await svc._check_login_anomaly("1.2.3.4", "admin")
        assert len(alerts) == 1

        # 再达到阈值，再次触发告警
        for _ in range(3):
            await svc._check_login_anomaly("1.2.3.4", "admin")
        assert len(alerts) == 2
        await svc.close()
