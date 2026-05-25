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
from dataclasses import dataclass, field
from datetime import UTC, datetime
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

        self._requests[request_id] = request

        # Schedule expiration check if needed
        if request.approval_status == ApprovalStatus.PENDING:
            task = asyncio.create_task(
                self._check_expiration(request_id, timeout_minutes * 60),
                name=f"approval-expire-{request_id}",
            )
            self._pending_tasks[request_id] = task

        # Notify approvers
        if request.approval_status == ApprovalStatus.PENDING:
            await self._notify_approvers(request, chain)

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
            request = self._requests.get(request_id)
            if request and request.approval_status == ApprovalStatus.PENDING:
                request.approval_status = ApprovalStatus.EXPIRED
                logger.warning("Approval request expired: %s", request_id)
                # Notify requester
                await self._notify_request_expired(request)
        except asyncio.CancelledError:
            pass
        finally:
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

        if request.approval_status != ApprovalStatus.PENDING:
            logger.warning("Request %s is not pending approval", request_id)
            return None

        # Record approval
        request.approvals[request.current_step]["status"] = ApprovalStatus.APPROVED.value
        request.approvals[request.current_step]["approved_by"] = approver
        request.approvals[request.current_step]["comment"] = comment
        request.approvals[request.current_step]["timestamp"] = datetime.now(UTC).isoformat()

        # Check if more approvals needed
        if request.current_step < len(request.approvals) - 1:
            request.current_step += 1
            # Notify next level approvers
            await self._notify_next_approver(request)
        else:
            # All approvals complete
            request.approval_status = ApprovalStatus.APPROVED
            # Execute the command
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

        if request.approval_status != ApprovalStatus.PENDING:
            return None

        request.approval_status = ApprovalStatus.REJECTED
        request.approvals[request.current_step]["status"] = ApprovalStatus.REJECTED.value
        request.approvals[request.current_step]["approved_by"] = approver
        request.approvals[request.current_step]["comment"] = reason

        # Cancel expiration task
        task = self._pending_tasks.pop(request_id, None)
        if task:
            task.cancel()

        # Notify requester
        await self._notify_request_rejected(request, approver, reason)

        logger.info("Command %s rejected by %s: %s", request_id, approver, reason)
        return request

    async def cancel_command(self, request_id: str, cancelled_by: str) -> CommandRequest | None:
        """Cancel a pending command request"""
        request = self._requests.get(request_id)
        if not request:
            return None

        if request.approval_status != ApprovalStatus.PENDING:
            return None

        request.approval_status = ApprovalStatus.CANCELLED

        # Cancel expiration task
        task = self._pending_tasks.pop(request_id, None)
        if task:
            task.cancel()

        logger.info("Command %s cancelled by %s", request_id, cancelled_by)
        return request

    async def _notify_next_approver(self, request: CommandRequest) -> None:
        """Notify next level approvers"""
        # Implementation similar to _notify_approvers
        pass

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

            # Execute based on command type
            if request.command_type == "write_point":
                point = list(request.command_params.keys())[0]
                value = list(request.command_params.values())[0]
                success = await driver_manager.write_point(
                    request.device_id, point, value
                )
                request.execution_result = "Success" if success else "Failed"
                request.approval_status = ApprovalStatus.EXECUTED if success else ApprovalStatus.FAILED
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

        for request_id, request in self._requests.items():
            created = datetime.fromisoformat(request.created_at)
            # Remove requests older than 30 days
            if (now - created).days > 30:
                if request.approval_status not in [
                    ApprovalStatus.PENDING,
                    ApprovalStatus.EXECUTED,
                ]:
                    to_remove.append(request_id)

        for request_id in to_remove:
            del self._requests[request_id]

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
