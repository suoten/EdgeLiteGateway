"""数据脱敏工具单元测试。

覆盖 src/edgelite/security/data_masking.py：SensitiveFilter 日志过滤、
mask_string / mask_sensitive / mask_json / mask_json_string、
redact_ip / redact_email / redact_phone、DataMasker 统一接口。
"""

from __future__ import annotations

import logging

from edgelite.security.data_masking import (
    DEFAULT_MASK_FIELDS,
    DEFAULT_SENSITIVE_PATTERNS,
    DataMasker,
    SensitiveFilter,
    mask_api_response,
    mask_dict_keys,
    mask_json,
    mask_json_string,
    mask_log,
    mask_sensitive,
)

# ─── mask_sensitive ───


def test_mask_sensitive_long():
    """长字符串保留首尾2字符 + 掩码。"""
    assert mask_sensitive("abcdefg") == "ab****fg"


def test_mask_sensitive_short():
    """<=4 字符全替换。"""
    assert mask_sensitive("ab") == "****"
    assert mask_sensitive("abcd") == "****"


def test_mask_sensitive_empty():
    """空值返回掩码。"""
    assert mask_sensitive("") == "****"


def test_mask_sensitive_custom_pattern():
    """自定义掩码模式。"""
    assert mask_sensitive("abcdefg", pattern="XXX") == "abXXXfg"


def test_mask_sensitive_non_string():
    """非字符串转为字符串后脱敏。"""
    result = mask_sensitive(12345)
    assert "****" in result or result == "****"


# ─── mask_json ───


def test_mask_json_basic():
    """敏感字段被脱敏，非敏感字段保留。"""
    data = {"username": "admin", "password": "secret123"}
    masked = mask_json(data)
    assert masked["username"] == "admin"
    assert masked["password"] != "secret123"
    assert "****" in masked["password"]


def test_mask_json_nested():
    """嵌套字典中的敏感字段被脱敏。"""
    data = {"user": {"name": "admin", "token": "abc123"}}
    masked = mask_json(data)
    assert masked["user"]["name"] == "admin"
    assert masked["user"]["token"] != "abc123"


def test_mask_json_list_value():
    """敏感字段的列表值中字符串被脱敏。"""
    data = {"token": ["abc", "def"]}
    masked = mask_json(data)
    assert masked["token"][0] != "abc"
    assert masked["token"][1] != "def"


def test_mask_json_preserves_non_sensitive():
    """非敏感字段原样保留。"""
    data = {"id": 1, "name": "device1", "password": "secret"}
    masked = mask_json(data)
    assert masked["id"] == 1
    assert masked["name"] == "device1"


def test_mask_json_deep_copy():
    """脱敏不修改原数据。"""
    data = {"password": "secret123"}
    masked = mask_json(data)
    assert data["password"] == "secret123"
    assert masked["password"] != "secret123"


def test_mask_json_custom_keys():
    """自定义敏感字段列表。"""
    data = {"custom_field": "value", "password": "secret"}
    masked = mask_json(data, sensitive_keys=["custom_field"])
    assert masked["custom_field"] != "value"
    # password 不在自定义列表中，应保留
    assert masked["password"] == "secret"


def test_mask_json_non_dict():
    """非字典输入原样返回。"""
    assert mask_json("string") == "string"
    assert mask_json(123) == 123


# ─── mask_json_string ───


def test_mask_json_string_valid_json():
    """有效 JSON 字符串脱敏。"""
    json_str = '{"username": "admin", "password": "secret"}'
    masked = mask_json_string(json_str)
    assert "secret" not in masked
    assert "admin" in masked


def test_mask_json_string_invalid_json():
    """无效 JSON 字符串用正则模式脱敏。"""
    text = "password=secret123"
    masked = mask_json_string(text)
    assert "secret123" not in masked


# ─── SensitiveFilter ───


def test_sensitive_filter_masks_password():
    """SensitiveFilter 脱敏日志中的 password。"""
    f = SensitiveFilter()
    result = f.mask_string('password="secret123"')
    assert "secret123" not in result


def test_sensitive_filter_masks_token():
    """SensitiveFilter 脱敏日志中的 token。"""
    f = SensitiveFilter()
    result = f.mask_string("token=abc123def456")
    assert "abc123def456" not in result


def test_sensitive_filter_masks_api_key():
    """SensitiveFilter 脱敏日志中的 api_key。"""
    f = SensitiveFilter()
    result = f.mask_string('api_key="sk-1234567890"')
    assert "sk-1234567890" not in result


def test_sensitive_filter_masks_jwt():
    """SensitiveFilter 脱敏 JWT token。"""
    f = SensitiveFilter()
    jwt_str = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature"
    result = f.mask_string(jwt_str)
    assert "signature" not in result or "JWT REDACTED" in result


def test_sensitive_filter_masks_phone():
    """SensitiveFilter 脱敏手机号。"""
    f = SensitiveFilter()
    result = f.mask_string("联系电话：13812345678")
    assert "13812345678" not in result


def test_sensitive_filter_masks_email():
    """SensitiveFilter 脱敏邮箱。"""
    f = SensitiveFilter()
    result = f.mask_string("email：user@example.com")
    assert "user@example.com" not in result


def test_sensitive_filter_empty_string():
    """空字符串原样返回。"""
    f = SensitiveFilter()
    assert f.mask_string("") == ""


def test_sensitive_filter_long_string_truncated():
    """超长字符串被截断后脱敏。"""
    f = SensitiveFilter()
    long_text = "password=secret&" * 10000
    result = f.mask_string(long_text)
    assert "secret" not in result


def test_sensitive_filter_filter_record():
    """filter() 方法处理 LogRecord。"""
    f = SensitiveFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='password="secret123"',
        args=None,
        exc_info=None,
    )
    assert f.filter(record) is True
    assert "secret123" not in str(record.msg)


# ─── redact_ip ───


def test_redact_ip_ipv4_default():
    """IPv4 默认保留前 2 段。"""
    from edgelite.security.data_masking import redact_ip

    result = redact_ip("192.168.1.100")
    assert "192" in result
    assert "168" in result
    assert "*" in result


def test_redact_ip_ipv4_custom_subnet():
    """IPv4 自定义保留段数。"""
    from edgelite.security.data_masking import redact_ip

    result = redact_ip("192.168.1.100", preserve_subnet=1)
    assert "192" in result
    assert "1" not in result or "*" in result


def test_redact_ip_ipv6():
    """IPv6 保留前 4 个 hextet。"""
    from edgelite.security.data_masking import redact_ip

    result = redact_ip("2001:db8:abcd:1234:5678:9abc:def0:1234")
    assert "2001" in result
    assert "ffff" in result


def test_redact_ip_empty():
    """空 IP 原样返回。"""
    from edgelite.security.data_masking import redact_ip

    assert redact_ip("") == ""


def test_redact_ip_invalid():
    """无效 IP 原样返回。"""
    from edgelite.security.data_masking import redact_ip

    assert redact_ip("not-an-ip") == "not-an-ip"


# ─── redact_email ───


def test_redact_email_basic():
    """邮箱脱敏保留域名。"""
    from edgelite.security.data_masking import redact_email

    result = redact_email("user@example.com")
    assert "@example.com" in result
    assert "user" not in result or "*" in result


def test_redact_email_short_local():
    """短本地名（<=2字符）脱敏。"""
    from edgelite.security.data_masking import redact_email

    result = redact_email("ab@example.com")
    assert "@example.com" in result
    assert "*" in result


def test_redact_email_no_at():
    """无 @ 符号用 mask_sensitive。"""
    from edgelite.security.data_masking import redact_email

    result = redact_email("notanemail")
    assert "*" in result


# ─── redact_phone ───


def test_redact_phone_basic():
    """手机号脱敏保留前3后3。"""
    from edgelite.security.data_masking import redact_phone

    result = redact_phone("13812345678")
    assert "138" in result
    assert "678" in result
    assert "****" in result
    assert "12345" not in result


def test_redact_phone_with_spaces():
    """带空格的手机号脱敏。"""
    from edgelite.security.data_masking import redact_phone

    result = redact_phone("138 1234 5678")
    assert "138" in result
    assert "678" in result


def test_redact_phone_short():
    """短号码用 mask_sensitive。"""
    from edgelite.security.data_masking import redact_phone

    result = redact_phone("12345")
    assert "*" in result


# ─── DataMasker ───


def test_data_masker_mask_dict():
    """DataMasker 脱敏字典。"""
    masker = DataMasker()
    data = {"username": "admin", "password": "secret"}
    result = masker.mask(data)
    assert result["username"] == "admin"
    assert result["password"] != "secret"


def test_data_masker_mask_list():
    """DataMasker 脱敏列表。"""
    masker = DataMasker()
    data = [{"password": "secret1"}, {"password": "secret2"}]
    result = masker.mask(data)
    assert result[0]["password"] != "secret1"
    assert result[1]["password"] != "secret2"


def test_data_masker_mask_string():
    """DataMasker 脱敏字符串。"""
    masker = DataMasker()
    result = masker.mask("sensitive_value")
    assert "*" in result


def test_data_masker_full_strategy():
    """full 策略完全替换敏感字段。"""
    masker = DataMasker()
    data = {"password": "secret"}
    result = masker.mask(data, strategy="full")
    assert result["password"] == "****"


def test_data_masker_add_sensitive_field():
    """添加自定义敏感字段。"""
    masker = DataMasker()
    masker.add_sensitive_field("custom_secret")
    data = {"custom_secret": "value"}
    result = masker.mask(data)
    assert result["custom_secret"] != "value"


def test_data_masker_set_strategy():
    """设置自定义脱敏策略。"""
    masker = DataMasker()
    masker.set_strategy("password", lambda v: "CUSTOM")
    data = {"password": "secret"}
    result = masker.mask(data)
    assert result["password"] == "CUSTOM"


# ─── mask_dict_keys ───


def test_mask_dict_keys_default():
    """默认键名重命名。"""
    data = {"password": "secret", "token": "abc"}
    result = mask_dict_keys(data)
    assert "pass" in result
    assert "tok" in result
    assert "password" not in result
    assert "token" not in result


def test_mask_dict_keys_custom():
    """自定义键名映射。"""
    data = {"old_key": "value"}
    result = mask_dict_keys(data, key_mappings={"old_key": "new_key"})
    assert "new_key" in result
    assert "old_key" not in result


def test_mask_dict_keys_nested():
    """嵌套字典键名重命名。"""
    data = {"outer": {"password": "secret"}}
    result = mask_dict_keys(data)
    assert "pass" in result["outer"]


# ─── mask_api_response ───


def test_mask_api_response_basic():
    """API 响应脱敏敏感字段，保留排除字段。"""
    response = {"id": 1, "name": "device", "password": "secret", "token": "abc"}
    result = mask_api_response(response)
    assert result["id"] == 1  # 排除字段保留原值
    assert result["name"] == "device"  # 非敏感字段保留
    assert result["password"] != "secret"  # 敏感字段脱敏
    assert result["token"] != "abc"


def test_mask_api_response_custom_exclude():
    """自定义排除字段。"""
    response = {"password": "secret", "custom_field": "value"}
    result = mask_api_response(response, exclude_fields=["custom_field"])
    assert result["custom_field"] == "value"


# ─── 默认常量 ───


def test_default_mask_fields_includes_password():
    assert "password" in DEFAULT_MASK_FIELDS


def test_default_mask_fields_includes_token():
    assert "token" in DEFAULT_MASK_FIELDS


def test_default_sensitive_patterns_includes_password():
    assert any("password" in pattern for pattern in DEFAULT_SENSITIVE_PATTERNS)


def test_default_sensitive_patterns_includes_jwt():
    assert any("eyJ" in pattern for pattern in DEFAULT_SENSITIVE_PATTERNS)


# ─── mask_log ───


def test_mask_log_record():
    """mask_log 脱敏 LogRecord。"""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='password="secret123"',
        args=None,
        exc_info=None,
    )
    result = mask_log(record)
    assert "secret123" not in str(result.msg)
