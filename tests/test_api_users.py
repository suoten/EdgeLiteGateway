"""Tests for edgelite.api.users — user CRUD + role management endpoints.

Covers all five endpoints (list/create/get/update/delete) including:
- happy paths, error responses (404/409/403/400/500)
- role management (admin protection, last-admin guard, sensitive-field guard)
- password policy, token revocation, token-renewal cache invalidation
- audit logging success/failure isolation

Endpoints construct UserRepo/DeviceRepo/RuleRepo internally, so the repo
classes are patched at the module level and the endpoint async functions are
invoked directly with mocked dependencies (bypassing FastAPI DI).
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from edgelite.api.error_codes import AuthErrors, RepoErrors, UserErrors
from edgelite.api.users import create_user, delete_user, get_user, list_users, update_user
from edgelite.models.db import StaleDataError
from edgelite.models.user import UserCreate, UserUpdate


# ── helpers ──────────────────────────────────────────────────────────────────


def _user_dict(
    user_id: str = "u1",
    username: str = "alice",
    role: str = "operator",
    enabled: bool = True,
) -> dict:
    """Build a complete user dict matching UserResponse fields."""
    return {
        "user_id": user_id,
        "username": username,
        "role": role,
        "enabled": enabled,
        "must_change_password": False,
        "password_changed_at": None,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": None,
        "version": 1,
    }


class _AsyncCM:
    """Minimal async context manager yielding a fixed session."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def harness():
    """Provide mocked db/repos/audit plus patched lazy imports for all endpoints."""
    from edgelite.api import users as users_module

    session = MagicMock(name="session")
    db = MagicMock(name="db")
    db.write_lock = MagicMock(name="write_lock")
    db.get_session.return_value = _AsyncCM(session)

    user_repo = AsyncMock(name="user_repo")
    device_repo = AsyncMock(name="device_repo")
    rule_repo = AsyncMock(name="rule_repo")
    audit_svc = AsyncMock(name="audit_svc")
    audit_svc.log = AsyncMock(return_value=None)
    request = MagicMock(name="request")

    # Sensible defaults — tests override as needed.
    user_repo.get.return_value = _user_dict(role="operator")
    user_repo.list_all.return_value = ([_user_dict()], 1)
    user_repo.get_by_username.return_value = None
    user_repo.create.return_value = _user_dict(user_id="new-1", username="newuser")
    user_repo.update.return_value = _user_dict(role="operator")
    user_repo.delete.return_value = True
    user_repo.count_by_role.return_value = 2
    device_repo.list_device_ids_by_owner.return_value = []
    rule_repo.list_all.return_value = ([], 0)

    hash_pwd = MagicMock(return_value="hashed-pw")
    get_ip = MagicMock(return_value="127.0.0.1")
    invalidate_cache = MagicMock(return_value=None)
    revoke_tokens = AsyncMock(return_value=1)

    patches = [
        patch.object(users_module, "UserRepo", return_value=user_repo),
        patch.object(users_module, "DeviceRepo", return_value=device_repo),
        patch.object(users_module, "RuleRepo", return_value=rule_repo),
        patch.object(users_module, "hash_password", hash_pwd),
        patch("edgelite.api.auth._get_client_ip", get_ip),
        # _invalidate_user_cache does not exist in token_renewal module → create=True
        patch(
            "edgelite.middleware.token_renewal._invalidate_user_cache",
            invalidate_cache,
            create=True,
        ),
        patch(
            "edgelite.security.token_revocation.revoke_all_tokens_for_user",
            revoke_tokens,
        ),
    ]
    for p in patches:
        p.start()

    ns = SimpleNamespace(
        db=db,
        session=session,
        user_repo=user_repo,
        device_repo=device_repo,
        rule_repo=rule_repo,
        audit_svc=audit_svc,
        request=request,
        user={"user_id": "admin-1", "username": "admin", "role": "admin"},
        hash_pwd=hash_pwd,
        get_ip=get_ip,
        invalidate_cache=invalidate_cache,
        revoke_tokens=revoke_tokens,
    )
    try:
        yield ns
    finally:
        for p in patches:
            p.stop()


def _config(protected_roles=None):
    """Build a config object with optional protected_roles override."""
    if protected_roles is None:
        return None
    return SimpleNamespace(security=SimpleNamespace(protected_roles=protected_roles))


# ── list_users ───────────────────────────────────────────────────────────────


class TestListUsers:
    async def test_success(self, harness):
        users = [_user_dict(user_id="u1"), _user_dict(user_id="u2")]
        harness.user_repo.list_all.return_value = (users, 2)
        pagination = SimpleNamespace(page=1, size=20)

        result = await list_users(harness.db, harness.user, pagination)

        assert result.total == 2
        assert result.page == 1
        assert result.size == 20
        assert len(result.data) == 2
        harness.user_repo.list_all.assert_awaited_once_with(1, 20)

    async def test_generic_error_returns_500(self, harness):
        harness.user_repo.list_all.side_effect = RuntimeError("db down")
        pagination = SimpleNamespace(page=2, size=10)

        with pytest.raises(HTTPException) as exc:
            await list_users(harness.db, harness.user, pagination)

        assert exc.value.status_code == 500
        assert exc.value.detail == UserErrors.LIST_FAILED


# ── create_user ──────────────────────────────────────────────────────────────


class TestCreateUser:
    async def test_success_hashes_password_and_audits(self, harness):
        body = UserCreate(username="newuser", password="Str0ng!pass", role="operator")
        harness.user_repo.create.return_value = _user_dict(
            user_id="new-1", username="newuser", role="operator"
        )

        result = await create_user(
            body, harness.db, harness.user, harness.request, harness.audit_svc
        )

        assert result.code == 0
        assert result.data["username"] == "newuser"
        harness.user_repo.get_by_username.assert_awaited_once_with("newuser")
        harness.hash_pwd.assert_called_once_with("Str0ng!pass")
        created_data = harness.user_repo.create.call_args.args[0]
        assert created_data["password"] == "hashed-pw"
        harness.audit_svc.log.assert_awaited_once()

    async def test_username_exists_returns_409(self, harness):
        body = UserCreate(username="newuser", password="Str0ng!pass", role="operator")
        harness.user_repo.get_by_username.return_value = _user_dict(username="newuser")

        with pytest.raises(HTTPException) as exc:
            await create_user(
                body, harness.db, harness.user, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 409
        assert exc.value.detail == UserErrors.USERNAME_EXISTS
        harness.user_repo.create.assert_not_awaited()

    async def test_value_error_from_repo_returns_409(self, harness):
        body = UserCreate(username="newuser", password="Str0ng!pass", role="operator")
        harness.user_repo.create.side_effect = ValueError("duplicate key")

        with pytest.raises(HTTPException) as exc:
            await create_user(
                body, harness.db, harness.user, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 409
        detail = exc.value.detail
        assert detail["error_code"] == UserErrors.CREATE_FAILED
        assert "duplicate key" in detail["errors"]

    async def test_generic_error_returns_500(self, harness):
        body = UserCreate(username="newuser", password="Str0ng!pass", role="operator")
        harness.user_repo.get_by_username.side_effect = RuntimeError("boom")

        with pytest.raises(HTTPException) as exc:
            await create_user(
                body, harness.db, harness.user, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 500
        assert exc.value.detail == UserErrors.CREATE_FAILED

    async def test_audit_failure_does_not_break_response(self, harness):
        body = UserCreate(username="newuser", password="Str0ng!pass", role="operator")
        harness.audit_svc.log.side_effect = RuntimeError("audit down")

        result = await create_user(
            body, harness.db, harness.user, harness.request, harness.audit_svc
        )

        assert result.data["username"] == "newuser"

    async def test_no_request_uses_empty_client_ip(self, harness):
        body = UserCreate(username="newuser", password="Str0ng!pass", role="operator")

        result = await create_user(body, harness.db, harness.user, None, harness.audit_svc)

        assert result.data["username"] == "newuser"
        harness.get_ip.assert_not_called()


# ── get_user ─────────────────────────────────────────────────────────────────


class TestGetUser:
    async def test_success(self, harness):
        target = _user_dict(user_id="u2", username="bob")
        harness.user_repo.get.return_value = target

        result = await get_user("u2", harness.db, harness.user)

        assert result.data["username"] == "bob"
        harness.user_repo.get.assert_awaited_once_with("u2")

    async def test_not_found_returns_404(self, harness):
        harness.user_repo.get.return_value = None

        with pytest.raises(HTTPException) as exc:
            await get_user("missing", harness.db, harness.user)

        assert exc.value.status_code == 404
        assert exc.value.detail == UserErrors.USER_NOT_FOUND

    async def test_generic_error_returns_500(self, harness):
        harness.user_repo.get.side_effect = RuntimeError("fail")

        with pytest.raises(HTTPException) as exc:
            await get_user("u2", harness.db, harness.user)

        assert exc.value.status_code == 500
        assert exc.value.detail == UserErrors.LIST_FAILED


# ── update_user ──────────────────────────────────────────────────────────────


class TestUpdateUser:
    async def test_role_change_success_invalidates_cache(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="operator", username="alice"
        )
        body = UserUpdate(role="viewer")

        result = await update_user(
            "u2", body, harness.db, harness.user, None, harness.request, harness.audit_svc
        )

        assert result.data is not None
        harness.user_repo.update.assert_awaited_once()
        harness.invalidate_cache.assert_called_once_with("alice")
        harness.audit_svc.log.assert_awaited_once()

    async def test_sensitive_field_on_admin_denied(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="admin", username="root"
        )
        body = UserUpdate(enabled=False)

        with pytest.raises(HTTPException) as exc:
            await update_user(
                "u2", body, harness.db, harness.user, None, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 403
        assert exc.value.detail == UserErrors.SENSITIVE_FIELD_DENIED
        harness.user_repo.update.assert_not_awaited()

    async def test_weak_password_rejected(self, harness):
        # Non-admin target so the sensitive-field guard is skipped.
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="operator", username="alice"
        )
        # Bypass pydantic validation — no weak password passes the model validator.
        body = UserUpdate.model_construct(password="password")

        with pytest.raises(HTTPException) as exc:
            await update_user(
                "u2", body, harness.db, harness.user, None, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 400
        assert exc.value.detail == AuthErrors.PASSWORD_POLICY
        harness.user_repo.update.assert_not_awaited()

    async def test_role_to_admin_by_non_admin_denied(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="operator", username="alice"
        )
        harness.user["role"] = "operator"
        body = UserUpdate(role="admin")

        with pytest.raises(HTTPException) as exc:
            await update_user(
                "u2", body, harness.db, harness.user, None, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 403
        assert exc.value.detail == AuthErrors.PERMISSION_DENIED

    async def test_demote_last_admin_denied(self, harness):
        # protected_roles must exclude "admin" to reach the count guard.
        config = _config(protected_roles=[])
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="admin", username="root"
        )
        harness.user_repo.count_by_role.return_value = 1
        body = UserUpdate(role="operator")

        with pytest.raises(HTTPException) as exc:
            await update_user(
                "u2", body, harness.db, harness.user, config, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 403
        assert exc.value.detail == UserErrors.CANNOT_REMOVE_LAST_ADMIN
        harness.user_repo.count_by_role.assert_awaited_once_with("admin")

    async def test_password_change_revokes_tokens(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="operator", username="alice"
        )
        body = UserUpdate(password="Str0ng!newp")

        result = await update_user(
            "u2", body, harness.db, harness.user, None, harness.request, harness.audit_svc
        )

        harness.hash_pwd.assert_called_once_with("Str0ng!newp")
        harness.revoke_tokens.assert_awaited_once_with("u2")
        # role/enabled not in data → no cache invalidation
        harness.invalidate_cache.assert_not_called()
        assert result.data is not None

    async def test_disable_user_revokes_tokens_and_invalidates_cache(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="operator", username="alice", enabled=True
        )
        body = UserUpdate(enabled=False)

        result = await update_user(
            "u2", body, harness.db, harness.user, None, harness.request, harness.audit_svc
        )

        harness.invalidate_cache.assert_called_once_with("alice")
        harness.revoke_tokens.assert_awaited_once_with("u2")
        assert result.data is not None

    async def test_not_found_returns_404(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="operator", username="alice"
        )
        harness.user_repo.update.return_value = None
        body = UserUpdate(role="viewer")

        with pytest.raises(HTTPException) as exc:
            await update_user(
                "u2", body, harness.db, harness.user, None, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 404
        assert exc.value.detail == UserErrors.USER_NOT_FOUND

    async def test_stale_data_returns_409(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="operator", username="alice"
        )
        harness.user_repo.update.side_effect = StaleDataError("version conflict")
        body = UserUpdate(role="viewer")

        with pytest.raises(HTTPException) as exc:
            await update_user(
                "u2", body, harness.db, harness.user, None, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 409
        assert exc.value.detail == RepoErrors.STALE_DATA_ERROR

    async def test_generic_error_returns_500(self, harness):
        harness.user_repo.get.side_effect = RuntimeError("boom")
        body = UserUpdate(role="viewer")

        with pytest.raises(HTTPException) as exc:
            await update_user(
                "u2", body, harness.db, harness.user, None, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 500
        assert exc.value.detail == UserErrors.UPDATE_FAILED

    async def test_custom_protected_roles_from_config(self, harness):
        config = _config(protected_roles=["superadmin"])
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="superadmin", username="root"
        )
        body = UserUpdate(password="Str0ng!newp")

        with pytest.raises(HTTPException) as exc:
            await update_user(
                "u2", body, harness.db, harness.user, config, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 403
        assert exc.value.detail == UserErrors.SENSITIVE_FIELD_DENIED

    async def test_audit_failure_does_not_break_response(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="operator", username="alice"
        )
        harness.audit_svc.log.side_effect = RuntimeError("audit down")
        body = UserUpdate(role="viewer")

        result = await update_user(
            "u2", body, harness.db, harness.user, None, harness.request, harness.audit_svc
        )

        assert result.data is not None

    async def test_token_revoke_failure_does_not_break_response(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="operator", username="alice"
        )
        harness.revoke_tokens.side_effect = RuntimeError("revoke fail")
        body = UserUpdate(password="Str0ng!newp")

        result = await update_user(
            "u2", body, harness.db, harness.user, None, harness.request, harness.audit_svc
        )

        assert result.data is not None


# ── delete_user ──────────────────────────────────────────────────────────────


class TestDeleteUser:
    async def test_success_revokes_tokens_and_audits(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="operator", username="bob"
        )

        result = await delete_user(
            "u2", harness.db, harness.user, None, harness.request, harness.audit_svc
        )

        assert result.code == 0
        harness.user_repo.delete.assert_awaited_once_with("u2")
        harness.invalidate_cache.assert_called_once_with("bob")
        harness.revoke_tokens.assert_awaited_once_with("u2")
        harness.audit_svc.log.assert_awaited_once()

    async def test_delete_self_denied(self, harness):
        with pytest.raises(HTTPException) as exc:
            await delete_user(
                "admin-1", harness.db, harness.user, None, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 400
        assert exc.value.detail == UserErrors.CANNOT_DELETE_SELF
        harness.user_repo.delete.assert_not_awaited()

    async def test_delete_admin_protected_role_denied(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="admin", username="root"
        )

        with pytest.raises(HTTPException) as exc:
            await delete_user(
                "u2", harness.db, harness.user, None, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 403
        assert exc.value.detail == UserErrors.CANNOT_DELETE_ADMIN

    async def test_delete_last_admin_denied(self, harness):
        # protected_roles must exclude "admin" to reach the count guard.
        config = _config(protected_roles=[])
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="admin", username="root"
        )
        harness.user_repo.count_by_role.return_value = 1

        with pytest.raises(HTTPException) as exc:
            await delete_user(
                "u2", harness.db, harness.user, config, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 403
        assert exc.value.detail == UserErrors.CANNOT_DELETE_LAST_ADMIN

    async def test_delete_user_with_devices_denied(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="operator", username="bob"
        )
        harness.device_repo.list_device_ids_by_owner.return_value = ["dev-1"]

        with pytest.raises(HTTPException) as exc:
            await delete_user(
                "u2", harness.db, harness.user, None, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 409
        assert exc.value.detail == UserErrors.HAS_RESOURCES
        harness.user_repo.delete.assert_not_awaited()

    async def test_delete_user_with_rules_denied(self, harness):
        harness.user_repo.get.return_value = _user_dict(
            user_id="u2", role="operator", username="bob"
        )
        harness.rule_repo.list_all.return_value = ([], 5)

        with pytest.raises(HTTPException) as exc:
            await delete_user(
                "u2", harness.db, harness.user, None, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 409
        assert exc.value.detail == UserErrors.HAS_RESOURCES

    async def test_not_found_returns_404(self, harness):
        harness.user_repo.get.return_value = None
        harness.user_repo.delete.return_value = False

        with pytest.raises(HTTPException) as exc:
            await delete_user(
                "u2", harness.db, harness.user, None, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 404
        assert exc.value.detail == UserErrors.USER_NOT_FOUND

    async def test_generic_error_returns_500(self, harness):
        harness.user_repo.get.side_effect = RuntimeError("boom")

        with pytest.raises(HTTPException) as exc:
            await delete_user(
                "u2", harness.db, harness.user, None, harness.request, harness.audit_svc
            )

        assert exc.value.status_code == 500
        assert exc.value.detail == UserErrors.DELETE_FAILED
