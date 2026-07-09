"""Command approval workflow service

Features:
- Command request and approval workflow
- Multi-level approval chains
- Approval notification
- Command history and audit trail
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from edgelite.services.notification import get_notification_manager

logger = logging.getLogger(__name__)


class ApprovalStatus(Enum):
    """Command approval status"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ApprovalLevel(Enum):
    """Approval level"""
    LEVEL_1 = 1  # Operator
    LEVEL_2 = 2  # Supervisor
    LEVEL_3 = 3  # Manager
    LEVEL_4 = 4  # Director


@dataclass
class ApprovalStep:
    """Single approval step"""
    level: int = 1
    role_required: str = ""
    approvers: list[str] = field(default_factory=list)
    timeout_minutes: int = 60


@dataclass
class ApprovalChain:
    """Multi-level approval chain configuration"""
    chain_id: str = ""
    name: str = ""
    severity: str = ""  # Which alarm severity requires this chain
    device_type: str = ""  # Which device type requires this chain
    command_type: str = ""  # Which command type requires this chain
    steps: list[ApprovalStep] = field(default_factory=list)
    escalation_minutes: int = 30  # Escalate to next level after this timeout
    enabled: bool = True


@dataclass
class CommandRequest:
    """Command request requiring approval"""
    request_id: str = ""
    device_id: str = ""
    device_name: str = ""
    command_type: str = ""  # write_point, write_points_batch, etc.
    command_params: dict = field(default_factory=dict)  # {point: value}
    requested_by: str = ""  # Username
    reason: str = ""
    priority: int = 5  # 1-10, higher = more urgent
    created_at: str = ""
    expires_at: str = ""
    # Approval tracking
    current_step: int = 0
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    approvals: list[dict] = field(default_factory=list)  # [{step, approver, status, comment, timestamp}]
    # Execution tracking
    executed_at: str = ""
    executed_by: str = ""
    execution_result: str = ""


class CommandApprovalService:
    """Service for managing command approval workflows"""

    def __init__(self):
        self._requests: dict[str, CommandRequest] = {}  # request_id -> CommandRequest
        self._approval_chains: dict[str, ApprovalChain] = {}  # chain_id -> ApprovalChain
        self._user_roles: dict[str, str] = {}  # username -> role
        self._pending_tasks: dict[str, asyncio.Task] = {}  # request_id -> task
        self._executed_callbacks: list[callable] = []
        self._notification_manager = get_notification_manager()
        self._lock = asyncio.Lock()  # FIXED(安全): 保护 _requests 状态的并发访问

    # ============== Approval Chain Management ==============

    def create_approval_chain(self, chain: ApprovalChain) -> ApprovalChain:
        """Create a new approval chain"""
        if not chain.chain_id:
            chain.chain_id = f"chain_{uuid.uuid4().hex[:8]}"
        self._approval_chains[chain.chain_id] = chain
        logger.info("Approval chain created: %s (%s)", chain.chain_id, chain.name)
        return chain

    def get_approval_chain(self, chain_id: str) -> ApprovalChain | None:
        """Get an approval chain by ID"""
        return self._approval_chains.get(chain_id)

    def list_approval_chains(self) -> list[ApprovalChain]:
        """List all approval chains"""
        return list(self._approval_chains.values())

    def update_approval_chain(self, chain_id: str, data: dict) -> ApprovalChain | None:
        """Update an approval chain"""
        chain = self._approval_chains.get(chain_id)
        if not chain:
            return None

        for key, value in data.items():
            if hasattr(chain, key):
                setattr(chain, key, value)

        logger.info("Approval chain updated: %s", chain_id)
        return chain

    def delete_approval_chain(self, chain_id: str) -> bool:
        """Delete an approval chain"""
        if chain_id in self._approval_chains:
            del self._approval_chains[chain_id]
            logger.info("Approval chain deleted: %s", chain_id)
            return True
        return False

    def set_user_role(self, username: str, role: str) -> None:
        """Set user role for approval"""
        self._user_roles[username] = role

    def get_user_role(self, username: str) -> str | None:
        """Get user role"""
        return self._user_roles.get(username)

    def get_users_by_role(self, role: str) -> list[str]:
        """Get all users with a specific role"""
        return [user for user, r in self._user_roles.items() if r == role]

    # ============== Command Request Management ==============

    async def submit_command(
        self,
        device_id: str,
        device_name: str,
        command_type: str,
        command_params: dict,
        requested_by: str,
        reason: str = "",
        priority: int = 5,
        approval_chain_id: str | None = None,
        timeout_minutes: int = 60,
    ) -> CommandRequest | None:
        """Submit a command for approval"""
        request_id = f"cmd_{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)

        # Check if approval is required
        chain = None
        if approval_chain_id:
            chain = self._approval_chains.get(approval_chain_id)
        else:
            # Auto-select chain based on command type
            chain = self._find_approval_chain(command_type)

        request = CommandRequest(
            request_id=request_id,
            device_id=device_id,
            device_name=device_name,
            command_type=command_type,
            command_params=command_params,
            requested_by=requested_by,
            reason=reason,
            priority=priority,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=timeout_minutes)).isoformat(),
            approval_status=ApprovalStatus.PENDING,
        )

        if chain and chain.enabled:
            # Set approval chain
            request.current_step = 0
            request.approvals = [
                {
                    "step": step.level,
                    "role_required": step.role_required,
                    "approvers": list(step.approvers),
                    "status": ApprovalStatus.PENDING.value,
                    "comment": "",
                    "approved_by": "",
                    "timestamp": "",
                }
                for step in chain.steps
            ]
            logger.info(
                "Command %s requires %d-level approval (chain: %s)",
                request_id, len(chain.steps), chain.chain_id
            )
        else:
            # No approval needed, auto-approve
            request.approval_status = ApprovalStatus.APPROVED
            request.current_step = -1  # Special value for auto-approved
            request.executed_at = now.isoformat()
            request.executed_by = "system"

        # R8-S-05 修复(严重): 原代码无锁修改 _requests 和 _pending_tasks，
        # 与 cleanup_expired/approve_command/cancel_command 并发时可能引发竞态。
        # 将状态写入放入 _lock 内，耗时通知保持在锁外。
        async with self._lock:
            self._requests[request_id] = request
            # Schedule expiration check if needed
            if request.approval_status == ApprovalStatus.PENDING:
                task = asyncio.create_task(
                    self._check_expiration(request_id, timeout_minutes * 60),
                    name=f"approval-expire-{request_id}",
                )
                self._pending_tasks[request_id] = task

        # Notify approvers (锁外执行，避免持锁等待网络 IO)
        if request.approval_status == ApprovalStatus.PENDING:
            await self._notify_approvers(request, chain)
        elif request.approval_status == ApprovalStatus.APPROVED:
            # FIX-P1: 自动审批的命令状态设为 APPROVED 但 _execute_command
            # 从未被调用，命令实际未执行。检测到自动审批时在锁外执行命令。
            await self._execute_command(request)

        return request

    def _find_approval_chain(self, command_type: str) -> ApprovalChain | None:
        """Find applicable approval chain for command type"""
        for chain in self._approval_chains.values():
            if chain.enabled and chain.command_type == command_type:
                return chain
        return None

    async def _notify_approvers(self, request: CommandRequest, chain: ApprovalChain | None) -> None:
        """Send notification to potential approvers"""
        if not chain or not chain.steps:
            return

        step = chain.steps[0]
        approvers = self.get_users_by_role(step.role_required)

        # Add explicit approvers
        approvers = list(set(approvers + step.approvers))

        # Build notification message
        message = f"""
New command requires approval:

Request ID: {request.request_id}
Device: {request.device_name} ({request.device_id})
Command: {request.command_type}
Parameters: {request.command_params}
Requested by: {request.requested_by}
Reason: {request.reason}
Priority: {request.priority}/10
"""

        # Send notification to approvers
        from edgelite.services.notification import AlarmNotification

        notification = AlarmNotification(
            alarm_id=request.request_id,
            rule_id="approval",
            rule_name="Command Approval Required",
            device_id=request.device_id,
            device_name=request.device_name,
            severity="warning",
            action="firing",
            message=message,
            trigger_value=request.command_params,
        )

        await self._notification_manager.send_notification(notification)

    async def _check_expiration(self, request_id: str, timeout_seconds: int) -> None:
        """Check if approval request has expired"""
        try:
            await asyncio.sleep(timeout_seconds)
            request = None
            need_notify = False
            # FIX-P0: 原 _check_expiration 直接修改 request.approval_status 和
            # _pending_tasks 全程无 self._lock 保护，与 approve_command/cancel_command
            # 并发时产生竞态（R8修复引入的回归）。改为在锁内检查并修改状态，
            # 耗时的通知操作在锁外执行，避免持锁等待 IO。
            async with self._lock:
                request = self._requests.get(request_id)
                if request and request.approval_status == ApprovalStatus.PENDING:
                    request.approval_status = ApprovalStatus.EXPIRED
                    # 锁内移除待办任务，避免与 cancel_command 竞态
                    self._pending_tasks.pop(request_id, None)
                    need_notify = True
                    logger.warning("Approval request expired: %s", request_id)
            # 锁外执行通知，避免持锁等待网络 IO
            if need_notify and request:
                await self._notify_request_expired(request)
        except asyncio.CancelledError:
            pass
        finally:
            # 兜底清理：正常流程已在锁内移除，此处仅清理异常路径残留
            self._pending_tasks.pop(request_id, None)

    async def _notify_request_expired(self, request: CommandRequest) -> None:
        """Notify requester that approval expired"""
        from edgelite.services.notification import AlarmNotification

        notification = AlarmNotification(
            alarm_id=request.request_id,
            rule_id="approval",
            rule_name="Approval Expired",
            device_id=request.device_id,
            device_name=request.device_name,
            severity="warning",
            action="firing",
            message=f"Command approval request {request.request_id} has expired",
        )

        await self._notification_manager.send_notification(notification)

    async def approve_command(
        self,
        request_id: str,
        approver: str,
        comment: str = "",
    ) -> CommandRequest | None:
        """Approve a command request"""
        request = self._requests.get(request_id)
        if not request:
            logger.warning("Approval request not found: %s", request_id)
            return None

        need_notify_next = False
        need_execute = False
        # FIXED(安全): 使用锁保护状态检查+修改，防止并发审批导致状态不一致
        async with self._lock:
            if request.approval_status != ApprovalStatus.PENDING:
                logger.warning("Request %s is not pending approval", request_id)
                return None

            # FIXED-P0: 原问题-未校验current_step边界，approvals为空或current_step越界时IndexError；
            # 改为：校验approvals非空且current_step在有效范围内
            if not request.approvals or request.current_step < 0 or request.current_step >= len(request.approvals):
                logger.error(
                    "Request %s has invalid approval state: current_step=%d, approvals_count=%d",
                    request_id, request.current_step, len(request.approvals),
                )
                return None

            # Record approval
            request.approvals[request.current_step]["status"] = ApprovalStatus.APPROVED.value
            request.approvals[request.current_step]["approved_by"] = approver
            request.approvals[request.current_step]["comment"] = comment
            request.approvals[request.current_step]["timestamp"] = datetime.now(UTC).isoformat()

            # Check if more approvals needed
            if request.current_step < len(request.approvals) - 1:
                request.current_step += 1
                need_notify_next = True
            else:
                # All approvals complete
                request.approval_status = ApprovalStatus.APPROVED
                need_execute = True

        # 在锁外执行耗时操作（通知/执行命令），避免长时间持锁
        if need_notify_next:
            await self._notify_next_approver(request)
        elif need_execute:
            await self._execute_command(request)

        logger.info("Command %s approved by %s", request_id, approver)
        return request

    async def reject_command(
        self,
        request_id: str,
        approver: str,
        reason: str = "",
    ) -> CommandRequest | None:
        """Reject a command request"""
        request = self._requests.get(request_id)
        if not request:
            return None

        need_notify = False
        # R5-F-08 修复(致命): reject_command 缺失锁保护，与 approve_command 并发会导致
        # 状态机不一致（如 reject 已 approve 的步骤）。使用与 approve_command 相同的
        # self._lock 保护状态读-改-写流程；耗时通知放锁外避免长持锁。
        async with self._lock:
            if request.approval_status != ApprovalStatus.PENDING:
                return None

            request.approval_status = ApprovalStatus.REJECTED
            # FIXED-P1: 原问题-未校验current_step边界，approvals为空或越界时IndexError
            if request.approvals and 0 <= request.current_step < len(request.approvals):
                request.approvals[request.current_step]["status"] = ApprovalStatus.REJECTED.value
                request.approvals[request.current_step]["approved_by"] = approver
                request.approvals[request.current_step]["comment"] = reason

            # Cancel expiration task
            task = self._pending_tasks.pop(request_id, None)
            need_notify = True

        # 锁外执行耗时操作
        if task:
            task.cancel()
        if need_notify:
            await self._notify_request_rejected(request, approver, reason)

        logger.info("Command %s rejected by %s: %s", request_id, approver, reason)
        return request

    async def cancel_command(self, request_id: str, cancelled_by: str) -> CommandRequest | None:
        """Cancel a pending command request"""
        request = self._requests.get(request_id)
        if not request:
            return None

        need_cancel_task = False
        # R6-S-02 修复(严重): cancel_command 缺失 self._lock 保护，与 approve/reject 并发
        # 会导致状态机不一致。使用同一把锁保护状态读-改-写，耗时操作在锁外。
        async with self._lock:
            if request.approval_status != ApprovalStatus.PENDING:
                return None

            request.approval_status = ApprovalStatus.CANCELLED
            # Cancel expiration task
            task = self._pending_tasks.pop(request_id, None)
            need_cancel_task = True

        # 锁外执行耗时操作
        if need_cancel_task and task:
            task.cancel()

        logger.info("Command %s cancelled by %s", request_id, cancelled_by)
        return request

    async def _notify_next_approver(self, request: CommandRequest) -> None:
        """Notify next level approvers

        FIX-P1: 原方法为空实现(pass)，导致多级审批链在第一级通过后断裂，
        下一级审批人无法收到通知。参考 _notify_approvers 的逻辑，从
        request.approvals[request.current_step] 获取下一级审批人并发送通知。
        """
        # 校验当前步骤索引有效，避免越界
        if not request.approvals or request.current_step < 0 or request.current_step >= len(request.approvals):
            logger.warning(
                "Cannot notify next approver: invalid step %d for request %s",
                request.current_step, request.request_id,
            )
            return

        step_info = request.approvals[request.current_step]
        role_required = step_info.get("role_required", "")
        explicit_approvers = step_info.get("approvers", []) or []

        # 按角色查询审批人，并合并显式指定的审批人
        approvers = self.get_users_by_role(role_required) if role_required else []
        approvers = list(set(approvers + list(explicit_approvers)))

        # 构建下一级审批通知消息
        message = f"""
Next-level approval required:

Request ID: {request.request_id}
Device: {request.device_name} ({request.device_id})
Command: {request.command_type}
Parameters: {request.command_params}
Requested by: {request.requested_by}
Reason: {request.reason}
Priority: {request.priority}/10
Approval step: {request.current_step + 1}/{len(request.approvals)}
Approvers: {", ".join(approvers) if approvers else "none"}
"""

        from edgelite.services.notification import AlarmNotification

        notification = AlarmNotification(
            alarm_id=request.request_id,
            rule_id="approval",
            rule_name="Command Approval Required (Next Level)",
            device_id=request.device_id,
            device_name=request.device_name,
            severity="warning",
            action="firing",
            message=message,
            trigger_value=request.command_params,
        )

        await self._notification_manager.send_notification(notification)

    async def _notify_request_rejected(
        self,
        request: CommandRequest,
        approver: str,
        reason: str,
    ) -> None:
        """Notify requester of rejection"""
        from edgelite.services.notification import AlarmNotification

        notification = AlarmNotification(
            alarm_id=request.request_id,
            rule_id="approval",
            rule_name="Command Rejected",
            device_id=request.device_id,
            device_name=request.device_name,
            severity="info",
            action="firing",
            message=f"Command rejected by {approver}: {reason}",
        )

        await self._notification_manager.send_notification(notification)

    async def _execute_command(self, request: CommandRequest) -> None:
        """Execute approved command"""
        try:
            # Get driver and execute command
            from edgelite.app import _app_state

            driver_manager = getattr(_app_state, "driver_manager", None)
            if not driver_manager:
                request.approval_status = ApprovalStatus.FAILED
                request.execution_result = "Driver manager not available"
                return

            # SEC-FIX(修复7): 审批执行路径必须经审计服务留痕，原问题-直接调用 driver_manager.write_point 绕过 API 层审计
            audit_svc = getattr(_app_state, "audit_service", None)

            # Execute based on command type
            if request.command_type == "write_point":
                # R8-G-02 修复(一般): 原代码直接 [0] 索引，command_params 为空时抛 IndexError
                if not request.command_params:
                    request.approval_status = ApprovalStatus.FAILED
                    request.execution_result = "command_params is empty"
                    request.executed_at = datetime.now(UTC).isoformat()
                    request.executed_by = "system"
                    return
                point = list(request.command_params.keys())[0]
                value = list(request.command_params.values())[0]

                # SEC-FIX: 审批通过后执行写入前，重新校验写保护策略
                # 防止审批期间写保护策略被修改（如管理员禁用写入或调整白名单）
                # 通过 device_service 获取 driver 实例并调用 check_write_allowed
                write_blocked_reason: str | None = None
                try:
                    device_service = getattr(_app_state, "device_service", None)
                    driver_instance = None
                    if device_service is not None:
                        driver_instance = await device_service.get_driver_instance(request.device_id)
                    if driver_instance is None and hasattr(driver_manager, "get_driver_instance"):
                        driver_instance = await driver_manager.get_driver_instance(request.device_id)
                    if driver_instance is not None and hasattr(driver_instance, "check_write_allowed"):
                        allowed = driver_instance.check_write_allowed(request.device_id, point)
                        if not allowed:
                            write_blocked_reason = "check_write_allowed returned False"
                except Exception as check_e:
                    logger.warning(
                        "[command_approval] check_write_allowed failed for request %s: %s",
                        request.request_id, check_e,
                    )
                    write_blocked_reason = f"check_write_allowed raised: {check_e}"

                if write_blocked_reason is not None:
                    request.approval_status = ApprovalStatus.FAILED
                    request.execution_result = (
                        f"Write blocked by write-protection policy at execution time: "
                        f"{write_blocked_reason}"
                    )
                    request.executed_at = datetime.now(UTC).isoformat()
                    request.executed_by = request.requested_by
                    logger.warning(
                        "[command_approval] 审批执行时写保护策略已变更，写入被拒绝 request=%s device=%s point=%s",
                        request.request_id, request.device_id, point,
                    )
                    if audit_svc is not None:
                        try:
                            from edgelite.services.audit_service import AuditAction
                            await audit_svc.log(
                                AuditAction.DEVICE_WRITE_POINT,
                                username=request.requested_by,
                                resource_type="device",
                                resource_id=request.device_id,
                                status="failed",
                                error_message="审批执行时写保护策略已变更，写入被拒绝",
                                details={
                                    "point": point,
                                    "value": value,
                                    "request_id": request.request_id,
                                    "reason": write_blocked_reason,
                                },
                            )
                        except Exception as audit_e:
                            logger.warning(
                                "[command_approval] audit log failed for blocked request %s: %s",
                                request.request_id, audit_e,
                            )
                    return

                success = await driver_manager.write_point(
                    request.device_id, point, value
                )
                request.execution_result = "Success" if success else "Failed"
                request.approval_status = ApprovalStatus.EXECUTED if success else ApprovalStatus.FAILED

                # SEC-FIX(修复7): 写入结果记录到审计日志，确保审批执行路径可追溯
                if audit_svc is not None:
                    try:
                        from edgelite.services.audit_service import AuditAction
                        await audit_svc.log(
                            AuditAction.DEVICE_WRITE_POINT,
                            username=request.requested_by,
                            resource_type="device",
                            resource_id=request.device_id,
                            status="success" if success else "failed",
                            after_value={
                                "point": point,
                                "value": value,
                                "approved_by": request.executed_by or request.requested_by,
                                "approval_chain": request.approvals,
                                "request_id": request.request_id,
                                "command_type": request.command_type,
                            },
                        )
                    except Exception as audit_e:
                        logger.warning(
                            "[command_approval] audit log failed for request %s: %s",
                            request.request_id, audit_e,
                        )
            else:
                request.execution_result = f"Unknown command type: {request.command_type}"
                request.approval_status = ApprovalStatus.FAILED

            request.executed_at = datetime.now(UTC).isoformat()
            request.executed_by = request.requested_by

            logger.info(
                "Command %s executed: %s",
                request.request_id,
                request.execution_result,
            )

            # Execute callbacks
            for callback in self._executed_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(request)
                    else:
                        callback(request)
                except Exception as e:
                    logger.debug("Command executed callback error: %s", e)

        except Exception as e:
            request.approval_status = ApprovalStatus.FAILED
            request.execution_result = str(e)
            logger.error("Command execution failed %s: %s", request.request_id, e)

    # ============== Query Methods ==============

    def get_request(self, request_id: str) -> CommandRequest | None:
        """Get a command request by ID"""
        return self._requests.get(request_id)

    def list_pending_requests(self, username: str | None = None) -> list[CommandRequest]:
        """List pending approval requests"""
        results = []
        for request in self._requests.values():
            if request.approval_status != ApprovalStatus.PENDING:
                continue
            if username:
                # Check if user can approve
                user_role = self.get_user_role(username)
                if not user_role:
                    continue
                current_step = request.current_step
                if current_step >= 0 and current_step < len(request.approvals):
                    step = request.approvals[current_step]
                    if user_role != step.get("role_required"):
                        continue
            results.append(request)
        return results

    def list_my_requests(self, username: str) -> list[CommandRequest]:
        """List command requests by user"""
        return [
            r for r in self._requests.values()
            if r.requested_by == username
        ]

    def list_requests(
        self,
        status: ApprovalStatus | None = None,
        device_id: str | None = None,
        limit: int = 100,
    ) -> list[CommandRequest]:
        """List all command requests with filters"""
        results = []
        for request in self._requests.values():
            if status and request.approval_status != status:
                continue
            if device_id and request.device_id != device_id:
                continue
            results.append(request)

        # Sort by created_at descending
        results.sort(key=lambda r: r.created_at, reverse=True)
        return results[:limit]

    def register_executed_callback(self, callback: callable) -> None:
        """Register callback for command execution"""
        self._executed_callbacks.append(callback)

    def unregister_executed_callback(self, callback: callable) -> None:
        """Unregister command execution callback"""
        if callback in self._executed_callbacks:
            self._executed_callbacks.remove(callback)

    async def cleanup_expired(self) -> int:
        """Clean up old completed/cancelled requests"""
        now = datetime.now(UTC)
        to_remove = []

        # R8-C-02 修复(致命): 原代码无锁迭代 _requests.items() 并删除，
        # 与 submit_command/approve_command/cancel_command 并发时会引发
        # "dictionary changed size during iteration" RuntimeError。
        # 改为在 _lock 内快照迭代 + 删除，确保线程安全。
        async with self._lock:
            for request_id, request in list(self._requests.items()):
                # FIXED-P1: 原问题-datetime.fromisoformat可能抛ValueError；改为try/except保护
                try:
                    created = datetime.fromisoformat(request.created_at)
                except (ValueError, TypeError):
                    logger.warning("Request %s has invalid created_at format: %s", request_id, request.created_at)
                    continue
                # Remove requests older than 30 days
                if (now - created).days > 30 and request.approval_status not in [
                    ApprovalStatus.PENDING,
                    ApprovalStatus.EXECUTED,
                ]:
                    to_remove.append(request_id)

            for request_id in to_remove:
                self._requests.pop(request_id, None)
                # R8-C-02: 同步清理可能残留的过期任务引用
                task = self._pending_tasks.pop(request_id, None)
                if task and not task.done():
                    task.cancel()

        logger.info("Cleaned up %d old approval requests", len(to_remove))
        return len(to_remove)


# Global instance
_approval_service: CommandApprovalService | None = None


def get_approval_service() -> CommandApprovalService:
    """Get the global command approval service"""
    global _approval_service
    if _approval_service is None:
        _approval_service = CommandApprovalService()
    return _approval_service


# SEC-FIX-V01: 轻量级写入意图记录，用于"审计即审批"模式
# 在完整多级审批链接入前，先确保所有写入操作有留痕
import threading as _threading

_intent_log: deque = deque(maxlen=10000)
_intent_lock = _threading.Lock()


def record_write_intent(device_id: str, point: str, value: Any, user_id: str, username: str) -> None:
    """记录写入意图到全局意图日志（线程安全）"""
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "device_id": device_id,
        "point": point,
        "value": value,
        "user_id": user_id,
        "username": username,
    }
    with _intent_lock:
        _intent_log.append(entry)


def get_write_intents(device_id: str | None = None, limit: int = 100) -> list[dict]:
    """查询写入意图日志"""
    with _intent_lock:
        entries = list(_intent_log)
    if device_id:
        entries = [e for e in entries if e.get("device_id") == device_id]
    return entries[-limit:]


# 为 CommandApprovalService 添加 record_intent 方法，便于从服务实例调用
def _service_record_intent(self, device_id: str, point: str, value: Any, user_id: str, username: str) -> None:
    """实例方法：记录写入意图"""
    record_write_intent(device_id, point, value, user_id, username)


CommandApprovalService.record_intent = _service_record_intent  # type: ignore[attr-defined]
