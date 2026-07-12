"""命令审批工作流测试 - 多级审批/超时/执行

覆盖 services/command_approval.py：
- ApprovalStatus / ApprovalLevel / ApprovalStep / ApprovalChain / CommandRequest 数据类
- CommandApprovalService: 审批链管理、命令提交/审批/拒绝/取消
- _find_approval_chain: 自动匹配审批链
- cleanup_expired: 过期清理
- record_write_intent / get_write_intents: 写入意图记录
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from edgelite.services.command_approval import (
    ApprovalChain,
    ApprovalLevel,
    ApprovalStatus,
    ApprovalStep,
    CommandApprovalService,
    CommandRequest,
    get_approval_service,
    get_write_intents,
    record_write_intent,
)


@pytest.fixture
def svc(monkeypatch):
    """创建 CommandApprovalService 实例（mock notification_manager + _execute_command）。

    _execute_command 默认 mock 为空操作：自动批准的命令调用它后状态保持 APPROVED，
    避免因缺少 driver_manager 而被标记为 FAILED。需要测试真实执行逻辑时可在用例内
    用 patch.object(svc, "_execute_command", ...) 覆盖。
    """
    service = CommandApprovalService()
    service._notification_manager = AsyncMock()
    service._notification_manager.send_notification = AsyncMock(return_value={"email": True})
    service._execute_command = AsyncMock()
    return service


class TestApprovalStatus:
    def test_status_values(self):
        """审批状态值"""
        assert ApprovalStatus.PENDING.value == "pending"
        assert ApprovalStatus.APPROVED.value == "approved"
        assert ApprovalStatus.REJECTED.value == "rejected"
        assert ApprovalStatus.EXECUTED.value == "executed"
        assert ApprovalStatus.FAILED.value == "failed"
        assert ApprovalStatus.CANCELLED.value == "cancelled"
        assert ApprovalStatus.EXPIRED.value == "expired"


class TestApprovalLevel:
    def test_level_values(self):
        """审批级别值"""
        assert ApprovalLevel.LEVEL_1.value == 1
        assert ApprovalLevel.LEVEL_2.value == 2
        assert ApprovalLevel.LEVEL_3.value == 3
        assert ApprovalLevel.LEVEL_4.value == 4


class TestDataclasses:
    def test_approval_step_defaults(self):
        """ApprovalStep 默认值"""
        step = ApprovalStep()
        assert step.level == 1
        assert step.role_required == ""
        assert step.approvers == []
        assert step.timeout_minutes == 60

    def test_approval_chain_defaults(self):
        """ApprovalChain 默认值"""
        chain = ApprovalChain()
        assert chain.chain_id == ""
        assert chain.steps == []
        assert chain.enabled is True
        assert chain.escalation_minutes == 30

    def test_command_request_defaults(self):
        """CommandRequest 默认值"""
        req = CommandRequest()
        assert req.approval_status == ApprovalStatus.PENDING
        assert req.priority == 5
        assert req.command_params == {}


class TestApprovalChainManagement:
    def test_create_approval_chain(self, svc):
        """应能创建审批链"""
        chain = ApprovalChain(name="test", command_type="write_point")
        created = svc.create_approval_chain(chain)
        assert created.chain_id != ""
        assert svc.get_approval_chain(created.chain_id) is not None

    def test_create_approval_chain_with_explicit_id(self, svc):
        """带显式 ID 的审批链不应被覆盖"""
        chain = ApprovalChain(chain_id="custom_id", name="test")
        svc.create_approval_chain(chain)
        assert chain.chain_id == "custom_id"

    def test_list_approval_chains(self, svc):
        """应能列出所有审批链"""
        svc.create_approval_chain(ApprovalChain(name="chain1"))
        svc.create_approval_chain(ApprovalChain(name="chain2"))
        chains = svc.list_approval_chains()
        assert len(chains) == 2

    def test_update_approval_chain(self, svc):
        """应能更新审批链"""
        chain = svc.create_approval_chain(ApprovalChain(name="test"))
        updated = svc.update_approval_chain(chain.chain_id, {"name": "updated", "enabled": False})
        assert updated.name == "updated"
        assert updated.enabled is False

    def test_update_nonexistent_chain(self, svc):
        """更新不存在的审批链应返回 None"""
        assert svc.update_approval_chain("nonexistent", {}) is None

    def test_delete_approval_chain(self, svc):
        """应能删除审批链"""
        chain = svc.create_approval_chain(ApprovalChain(name="test"))
        assert svc.delete_approval_chain(chain.chain_id) is True
        assert svc.get_approval_chain(chain.chain_id) is None

    def test_delete_nonexistent_chain(self, svc):
        """删除不存在的审批链应返回 False"""
        assert svc.delete_approval_chain("nonexistent") is False


class TestUserRoleManagement:
    def test_set_and_get_user_role(self, svc):
        """应能设置和获取用户角色"""
        svc.set_user_role("alice", "operator")
        assert svc.get_user_role("alice") == "operator"

    def test_get_nonexistent_user_role(self, svc):
        """获取不存在用户角色应返回 None"""
        assert svc.get_user_role("nobody") is None

    def test_get_users_by_role(self, svc):
        """应能按角色查询用户"""
        svc.set_user_role("alice", "operator")
        svc.set_user_role("bob", "operator")
        svc.set_user_role("carol", "admin")
        operators = svc.get_users_by_role("operator")
        assert set(operators) == {"alice", "bob"}


class TestSubmitCommand:
    @pytest.mark.asyncio
    async def test_submit_auto_approve_no_chain(self, svc):
        """无审批链时应自动批准"""
        request = await svc.submit_command(
            device_id="dev1",
            device_name="Device1",
            command_type="write_point",
            command_params={"point1": 1},
            requested_by="alice",
        )
        assert request.approval_status == ApprovalStatus.APPROVED
        assert request.current_step == -1  # 自动批准标记

    @pytest.mark.asyncio
    async def test_submit_with_chain_pending(self, svc):
        """有审批链时应为 PENDING 状态"""
        chain = ApprovalChain(
            chain_id="chain1",
            name="test",
            command_type="write_point",
            steps=[ApprovalStep(level=1, role_required="supervisor")],
        )
        svc.create_approval_chain(chain)
        request = await svc.submit_command(
            device_id="dev1",
            device_name="Device1",
            command_type="write_point",
            command_params={"point1": 1},
            requested_by="alice",
            approval_chain_id="chain1",
            timeout_minutes=60,
        )
        assert request.approval_status == ApprovalStatus.PENDING
        assert len(request.approvals) == 1
        assert request.approvals[0]["role_required"] == "supervisor"

    @pytest.mark.asyncio
    async def test_submit_generates_request_id(self, svc):
        """应生成唯一 request_id"""
        req1 = await svc.submit_command("dev1", "D1", "write_point", {}, "alice")
        req2 = await svc.submit_command("dev1", "D1", "write_point", {}, "alice")
        assert req1.request_id != req2.request_id

    @pytest.mark.asyncio
    async def test_submit_sets_expiry(self, svc):
        """应设置过期时间"""
        req = await svc.submit_command(
            "dev1", "D1", "write_point", {}, "alice", timeout_minutes=30
        )
        assert req.expires_at != ""
        # 过期时间应大约在 30 分钟后
        expires = datetime.fromisoformat(req.expires_at)
        created = datetime.fromisoformat(req.created_at)
        delta = expires - created
        assert 29 <= delta.total_seconds() / 60 <= 31


class TestApproveCommand:
    @pytest.mark.asyncio
    async def test_approve_nonexistent_request(self, svc):
        """审批不存在的请求应返回 None"""
        result = await svc.approve_command("nonexistent", "alice")
        assert result is None

    @pytest.mark.asyncio
    async def test_approve_single_step(self, svc):
        """单步审批链通过后应为 APPROVED"""
        chain = ApprovalChain(
            chain_id="chain1",
            name="test",
            command_type="write_point",
            steps=[ApprovalStep(level=1, role_required="supervisor")],
        )
        svc.create_approval_chain(chain)
        request = await svc.submit_command(
            "dev1", "D1", "write_point", {"p": 1}, "alice",
            approval_chain_id="chain1",
        )
        # Mock _execute_command 避免实际执行
        with patch.object(svc, "_execute_command", new=AsyncMock()):
            result = await svc.approve_command(request.request_id, "supervisor1")
        assert result.approval_status == ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_approve_multi_step_advances(self, svc):
        """多步审批链应推进到下一步"""
        chain = ApprovalChain(
            chain_id="chain1",
            name="test",
            command_type="write_point",
            steps=[
                ApprovalStep(level=1, role_required="operator"),
                ApprovalStep(level=2, role_required="supervisor"),
            ],
        )
        svc.create_approval_chain(chain)
        request = await svc.submit_command(
            "dev1", "D1", "write_point", {"p": 1}, "alice",
            approval_chain_id="chain1",
        )
        # 第一步审批
        result = await svc.approve_command(request.request_id, "operator1")
        assert result.approval_status == ApprovalStatus.PENDING  # 仍需第二步
        assert result.current_step == 1  # 推进到第二步

    @pytest.mark.asyncio
    async def test_approve_already_approved(self, svc):
        """已审批的请求再次审批应返回 None"""
        chain = ApprovalChain(
            chain_id="chain1",
            name="test",
            steps=[ApprovalStep(level=1, role_required="supervisor")],
        )
        svc.create_approval_chain(chain)
        request = await svc.submit_command(
            "dev1", "D1", "write_point", {"p": 1}, "alice",
            approval_chain_id="chain1",
        )
        with patch.object(svc, "_execute_command", new=AsyncMock()):
            await svc.approve_command(request.request_id, "supervisor1")
        # 再次审批应返回 None
        result = await svc.approve_command(request.request_id, "supervisor1")
        assert result is None


class TestRejectCommand:
    @pytest.mark.asyncio
    async def test_reject_nonexistent(self, svc):
        """拒绝不存在的请求应返回 None"""
        assert await svc.reject_command("nonexistent", "alice") is None

    @pytest.mark.asyncio
    async def test_reject_pending_request(self, svc):
        """拒绝 PENDING 请求应为 REJECTED"""
        chain = ApprovalChain(
            chain_id="chain1",
            name="test",
            steps=[ApprovalStep(level=1, role_required="supervisor")],
        )
        svc.create_approval_chain(chain)
        request = await svc.submit_command(
            "dev1", "D1", "write_point", {"p": 1}, "alice",
            approval_chain_id="chain1",
        )
        result = await svc.reject_command(request.request_id, "supervisor1", "bad command")
        assert result.approval_status == ApprovalStatus.REJECTED

    @pytest.mark.asyncio
    async def test_reject_already_approved(self, svc):
        """已审批的请求不能拒绝"""
        chain = ApprovalChain(
            chain_id="chain1",
            name="test",
            steps=[ApprovalStep(level=1, role_required="supervisor")],
        )
        svc.create_approval_chain(chain)
        request = await svc.submit_command(
            "dev1", "D1", "write_point", {"p": 1}, "alice",
            approval_chain_id="chain1",
        )
        with patch.object(svc, "_execute_command", new=AsyncMock()):
            await svc.approve_command(request.request_id, "supervisor1")
        result = await svc.reject_command(request.request_id, "supervisor1")
        assert result is None


class TestCancelCommand:
    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self, svc):
        """取消不存在的请求应返回 None"""
        assert await svc.cancel_command("nonexistent", "alice") is None

    @pytest.mark.asyncio
    async def test_cancel_pending(self, svc):
        """取消 PENDING 请求应为 CANCELLED"""
        chain = ApprovalChain(
            chain_id="chain1",
            name="test",
            steps=[ApprovalStep(level=1, role_required="supervisor")],
        )
        svc.create_approval_chain(chain)
        request = await svc.submit_command(
            "dev1", "D1", "write_point", {"p": 1}, "alice",
            approval_chain_id="chain1",
        )
        result = await svc.cancel_command(request.request_id, "alice")
        assert result.approval_status == ApprovalStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_non_pending(self, svc):
        """非 PENDING 请求不能取消"""
        req = await svc.submit_command("dev1", "D1", "write_point", {}, "alice")
        # 自动批准的请求不能取消
        result = await svc.cancel_command(req.request_id, "alice")
        assert result is None


class TestQueryRequests:
    @pytest.mark.asyncio
    async def test_get_request(self, svc):
        """应能按 ID 获取请求"""
        req = await svc.submit_command("dev1", "D1", "write_point", {}, "alice")
        assert svc.get_request(req.request_id) is not None

    @pytest.mark.asyncio
    async def test_get_nonexistent_request(self, svc):
        """获取不存在的请求应返回 None"""
        assert svc.get_request("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_pending_requests(self, svc):
        """应能列出待审批请求"""
        chain = ApprovalChain(
            chain_id="chain1",
            name="test",
            steps=[ApprovalStep(level=1, role_required="supervisor")],
        )
        svc.create_approval_chain(chain)
        await svc.submit_command("dev1", "D1", "write_point", {}, "alice", approval_chain_id="chain1")
        pending = svc.list_pending_requests()
        assert len(pending) == 1

    @pytest.mark.asyncio
    async def test_list_my_requests(self, svc):
        """应能按用户列出请求"""
        await svc.submit_command("dev1", "D1", "write_point", {}, "alice")
        await svc.submit_command("dev2", "D2", "write_point", {}, "bob")
        alice_requests = svc.list_my_requests("alice")
        assert len(alice_requests) == 1
        assert alice_requests[0].requested_by == "alice"

    @pytest.mark.asyncio
    async def test_list_requests_with_filters(self, svc):
        """应能按状态/设备过滤"""
        await svc.submit_command("dev1", "D1", "write_point", {}, "alice")
        await svc.submit_command("dev2", "D2", "write_point", {}, "bob")
        # 按设备过滤
        results = svc.list_requests(device_id="dev1")
        assert len(results) == 1
        assert results[0].device_id == "dev1"
        # 按状态过滤
        results = svc.list_requests(status=ApprovalStatus.APPROVED)
        assert len(results) == 2  # 两个都是自动批准


class TestCleanupExpired:
    @pytest.mark.asyncio
    async def test_cleanup_removes_old_records(self, svc):
        """应清理 30 天前的已完成/已取消请求"""
        # 创建一个旧请求
        req = await svc.submit_command("dev1", "D1", "write_point", {}, "alice")
        # 手动修改 created_at 为 40 天前
        req.created_at = (datetime.now(UTC) - timedelta(days=40)).isoformat()
        req.approval_status = ApprovalStatus.CANCELLED

        deleted = await svc.cleanup_expired()
        assert deleted == 1
        assert svc.get_request(req.request_id) is None

    @pytest.mark.asyncio
    async def test_cleanup_keeps_recent(self, svc):
        """不应清理近期请求"""
        await svc.submit_command("dev1", "D1", "write_point", {}, "alice")
        deleted = await svc.cleanup_expired()
        assert deleted == 0


class TestWriteIntentLog:
    def test_record_and_get_write_intent(self):
        """应能记录和查询写入意图"""
        record_write_intent("dev1", "point1", 42, "u1", "alice")
        intents = get_write_intents(device_id="dev1")
        assert len(intents) >= 1
        assert intents[-1]["device_id"] == "dev1"
        assert intents[-1]["point"] == "point1"
        assert intents[-1]["value"] == 42

    def test_get_write_intents_all_devices(self):
        """不指定设备应返回所有意图"""
        record_write_intent("dev_all", "p", 1, "u", "user")
        intents = get_write_intents()
        assert len(intents) >= 1

    def test_get_write_intents_limit(self):
        """应支持 limit 参数"""
        for i in range(10):
            record_write_intent("dev_lim", f"p{i}", i, "u", "user")
        intents = get_write_intents(device_id="dev_lim", limit=5)
        assert len(intents) <= 5


class TestGetApprovalService:
    def test_returns_instance(self):
        """get_approval_service 应返回实例"""
        s = get_approval_service()
        assert isinstance(s, CommandApprovalService)


class TestExecutedCallback:
    @pytest.mark.asyncio
    async def test_register_and_unregister_callback(self, svc):
        """应能注册和注销执行回调"""
        called = []

        def callback(req):
            called.append(req.request_id)

        svc.register_executed_callback(callback)
        assert callback in svc._executed_callbacks
        svc.unregister_executed_callback(callback)
        assert callback not in svc._executed_callbacks

    @pytest.mark.asyncio
    async def test_register_async_callback(self, svc):
        """应能注册异步回调"""

        async def callback(req):
            pass

        svc.register_executed_callback(callback)
        assert callback in svc._executed_callbacks
