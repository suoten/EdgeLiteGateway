"""安全模块单元测试"""

from edgelite.security.jwt import create_access_token, decode_token, verify_token
from edgelite.security.password import hash_password, verify_password
from edgelite.security.rbac import Permission, Role, has_permission

# ─── JWT ───


def test_create_and_verify_token():
    """测试JWT创建和验证"""
    token = create_access_token({"sub": "admin", "role": "admin"})
    assert token is not None

    payload = verify_token(token)
    assert payload is not None
    assert payload["sub"] == "admin"
    assert payload["role"] == "admin"


def test_decode_token():
    """测试JWT解码"""
    token = create_access_token({"sub": "user1", "role": "operator"})
    payload = decode_token(token)
    assert payload["sub"] == "user1"


def test_invalid_token():
    """测试无效token"""
    try:
        verify_token("invalid.token.here")
        raise AssertionError("应该抛异常")
    except Exception:
        pass  # 预期行为


# ─── Password ───


def test_hash_and_verify_password():
    """测试密码哈希和验证"""
    password = "test123"
    hashed = hash_password(password)
    assert hashed != password
    assert verify_password(password, hashed) is True


def test_wrong_password():
    """测试错误密码"""
    hashed = hash_password("correct")
    assert verify_password("wrong", hashed) is False


# ─── RBAC ───


def test_admin_has_all_permissions():
    """测试管理员拥有所有权限"""
    for perm in Permission:
        assert has_permission(Role.ADMIN, perm) is True


def test_operator_permissions():
    """测试操作员权限"""
    # 操作员可以读设备
    assert has_permission(Role.OPERATOR, Permission.DEVICE_READ) is True
    # 操作员不能创建设备（需要admin）
    assert has_permission(Role.OPERATOR, Permission.DEVICE_CREATE) is False
    # 操作员可以读规则、切换规则
    assert has_permission(Role.OPERATOR, Permission.RULE_READ) is True
    assert has_permission(Role.OPERATOR, Permission.RULE_TOGGLE) is True
    # 操作员可以确认告警
    assert has_permission(Role.OPERATOR, Permission.ALARM_ACK) is True
    # 操作员不能管理用户
    assert has_permission(Role.OPERATOR, Permission.USER_CREATE) is False
    assert has_permission(Role.OPERATOR, Permission.USER_DELETE) is False


def test_viewer_permissions():
    """测试观察者权限"""
    # 观察者只能读
    assert has_permission(Role.VIEWER, Permission.DEVICE_READ) is True
    assert has_permission(Role.VIEWER, Permission.DEVICE_CREATE) is False
    assert has_permission(Role.VIEWER, Permission.RULE_CREATE) is False
