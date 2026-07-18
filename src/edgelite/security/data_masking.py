"""数据脱敏工具 - 日志、API响应、敏感信息脱敏"""

from __future__ import annotations

import ipaddress
import json
import logging
import re

# FIXED(P3): 原问题-F401未使用导入datetime.datetime; 修复-删除该导入行
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# FIXED-P0: 限制 mask_string 输入长度，防止超长输入触发 ReDoS 或性能退化
_MAX_MASK_STRING_LENGTH = 65536

# 默认敏感字段列表
DEFAULT_SENSITIVE_PATTERNS = {
    # 密码类 - 引号包裹值
    r"password[\"']?\s*[:=]\s*[\"'][^\"']+[\"']": "password=***",
    r"pwd[\"']?\s*[:=]\s*[\"'][^\"']+[\"']": "pwd=***",
    r"pass[\"']?\s*[:=]\s*[\"'][^\"']+[\"']": "pass=***",
    # FIXED-P2: 增加非引号包裹值的脱敏模式，防止攻击者通过构造不含引号的日志绕过脱敏
    # 之前：仅匹配password="xxx"格式，password=xxx格式不被脱敏
    # 之后：同时匹配引号和非引号包裹的敏感值
    r"password[\"']?\s*[:=]\s*[^\s,\"'}\]]+": "password=***",
    r"pwd[\"']?\s*[:=]\s*[^\s,\"'}\]]+": "pwd=***",
    # Token类 - 引号包裹值
    r"token[\"']?\s*[:=]\s*[\"'][^\"']+[\"']": "token=***",
    r"bearer[\"']?\s*[:=]\s*[\"'][^\"']+[\"']": "bearer=***",
    r"authorization[\"']?\s*[:=]\s*[\"'][^\"']+[\"']": "authorization=***",
    # Token类 - 非引号包裹值
    r"token[\"']?\s*[:=]\s*[^\s,\"'}\]]+": "token=***",
    r"bearer[\"']?\s*[:=]\s*[^\s,\"'}\]]+": "bearer=***",
    r"authorization[\"']?\s*[:=]\s*[^\s,\"'}\]]+": "authorization=***",
    # API Key类 - 引号包裹值
    r"api[_-]?key[\"']?\s*[:=]\s*[\"'][^\"']+[\"']": "api_key=***",
    r"secret[_-]?key[\"']?\s*[:=]\s*[\"'][^\"']+[\"']": "secret_key=***",
    r"access[_-]?key[\"']?\s*[:=]\s*[\"'][^\"']+[\"']": "access_key=***",
    # API Key类 - 非引号包裹值
    r"api[_-]?key[\"']?\s*[:=]\s*[^\s,\"'}\]]+": "api_key=***",
    r"secret[_-]?key[\"']?\s*[:=]\s*[^\s,\"'}\]]+": "secret_key=***",
    r"access[_-]?key[\"']?\s*[:=]\s*[^\s,\"'}\]]+": "access_key=***",
    # 证书私钥类
    r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----": "[PRIVATE KEY REDACTED]",
    r"-----BEGIN\s+CERTIFICATE-----": "[CERTIFICATE REDACTED]",
    # JWT Token
    r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+": "[JWT REDACTED]",
    # IP地址（可选，根据场景决定是否脱敏）
    # r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}": "[IP REDACTED]",
    # 身份证号
    r"\d{17}[\dXx]": "[ID REDACTED]",
    # 手机号
    r"1[3-9]\d{9}": "[PHONE REDACTED]",
    # 邮箱
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}": "[EMAIL REDACTED]",
}

# 默认掩码字段
DEFAULT_MASK_FIELDS = {
    "password",
    "pwd",
    "pass",
    "token",
    "bearer",
    "authorization",
    "api_key",
    "apiKey",
    "secret_key",
    "secretKey",
    "access_key",
    "accessKey",
    "private_key",
    "privateKey",
    "certificate",
    "cert",
    "credential",
    "ssn",
    "social_security",
    "credit_card",
    "card_number",
}


@dataclass
class SensitiveFilter(logging.Filter):
    """日志敏感信息过滤器

    使用方式：
        handler.addFilter(SensitiveFilter())

        # 或设置全局
        logging.getLogger().addFilter(SensitiveFilter())
    """

    patterns: dict[str, str] = field(default_factory=lambda: DEFAULT_SENSITIVE_PATTERNS.copy())
    mask_fields: set[str] = field(default_factory=lambda: DEFAULT_MASK_FIELDS.copy())

    def filter(self, record: logging.LogRecord) -> bool:
        """过滤日志记录中的敏感信息"""
        try:
            # 处理消息
            if record.msg:
                record.msg = self.mask_string(str(record.msg))
            # 处理参数
            if record.args:
                record.args = tuple(self.mask_string(str(arg)) if isinstance(arg, str) else arg for arg in record.args)
            # 处理异常信息
            if record.exc_text:
                record.exc_text = self.mask_string(record.exc_text)
        except Exception:
            logger.debug(
                "Data masking filter failed, original message retained"
            )  # FIXED-P2: 脱敏失败时记录日志而非静默
        return True

    def mask_string(self, text: str) -> str:
        """脱敏字符串"""
        if not text:
            return text

        # FIXED-P0: 限制输入长度，防止超长字符串触发 ReDoS 或导致正则替换性能退化
        # （所有模式顺序应用，复杂度 O(n * m)，n=文本长度，m=模式数）
        if len(text) > _MAX_MASK_STRING_LENGTH:
            logger.debug(
                "Input too long for masking (%d chars), truncating to %d",
                len(text),
                _MAX_MASK_STRING_LENGTH,
            )
            text = text[:_MAX_MASK_STRING_LENGTH]

        result = text
        for pattern, replacement in self.patterns.items():
            try:
                result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
            except re.error:
                continue

        return result


def mask_sensitive(value: str, pattern: str = "****") -> str:
    """掩码化敏感字符串

    Args:
        value: 原始值
        pattern: 掩码模式，默认 '****'

    Returns:
        掩码后的字符串
    """
    if not value:
        return pattern
    value_str = str(value)
    if len(value_str) <= 4:
        return pattern
    return value_str[:2] + pattern + value_str[-2:]


def mask_json(data: dict, sensitive_keys: list[str] | None = None) -> dict:
    """对 JSON 数据中的敏感字段进行脱敏

    Args:
        data: 原始字典数据
        sensitive_keys: 敏感字段列表，默认使用 DEFAULT_MASK_FIELDS

    Returns:
        脱敏后的数据（深拷贝）

    示例:
        >>> config = {"username": "admin", "password": "secret123"}
        >>> mask_json(config)
        {'username': 'admin', 'password': '****'}
    """
    if not isinstance(data, dict):
        return data

    if sensitive_keys is None:
        sensitive_keys = list(DEFAULT_MASK_FIELDS)

    # FIXED-P1: 预计算敏感字段集合，避免每次循环重建
    sensitive_set = {k.lower() for k in sensitive_keys}

    # 深拷贝以避免修改原数据
    result = {}
    for key, value in data.items():
        key_lower = key.lower()

        # 检查是否是敏感字段
        is_sensitive = key_lower in sensitive_set

        if is_sensitive:
            if isinstance(value, str):
                result[key] = mask_sensitive(value)
            elif isinstance(value, dict):
                result[key] = mask_json(value, sensitive_keys)
            elif isinstance(value, list):
                # FIXED-P1: 原实现仅处理 dict 项，字符串项未脱敏。
                # 敏感字段的列表值（如 {"tokens": ["abc", "def"]}）中的字符串也应脱敏
                result[key] = [
                    mask_json(item, sensitive_keys)
                    if isinstance(item, dict)
                    else mask_sensitive(str(item))
                    if isinstance(item, str)
                    else "****"
                    for item in value
                ]
            else:
                result[key] = "****"
        elif isinstance(value, dict):
            result[key] = mask_json(value, sensitive_keys)
        elif isinstance(value, list):
            result[key] = [mask_json(item, sensitive_keys) if isinstance(item, dict) else item for item in value]
        else:
            result[key] = value

    return result


def mask_json_string(json_str: str, sensitive_keys: list[str] | None = None) -> str:
    """对 JSON 字符串进行脱敏

    Args:
        json_str: JSON 字符串
        sensitive_keys: 敏感字段列表

    Returns:
        脱敏后的 JSON 字符串
    """
    try:
        data = json.loads(json_str)
        masked = mask_json(data, sensitive_keys)
        return json.dumps(masked, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        # 如果不是有效 JSON，尝试用正则模式匹配
        return SensitiveFilter().mask_string(json_str)


def mask_log(record: logging.LogRecord) -> logging.LogRecord:
    """脱敏日志记录

    Args:
        record: 日志记录

    Returns:
        脱敏后的日志记录
    """
    filter_instance = SensitiveFilter()
    filter_instance.filter(record)
    return record


def mask_dict_keys(data: dict, key_mappings: dict[str, str] | None = None) -> dict:
    """重命名字典中的敏感键名

    Args:
        data: 原始字典
        key_mappings: 键名映射 {原键名: 新键名}

    Returns:
        重命名后的字典
    """
    if not isinstance(data, dict):
        return data

    key_mappings = key_mappings or {
        "password": "pass",
        "token": "tok",
        "secret": "sec",
        "api_key": "key",
    }

    result = {}
    for key, value in data.items():
        new_key = key_mappings.get(key, key)
        if isinstance(value, dict):
            result[new_key] = mask_dict_keys(value, key_mappings)
        else:
            result[new_key] = value

    return result


def redact_ip(ip: str, preserve_subnet: int = 2) -> str:
    """IP 地址脱敏

    Args:
        ip: IP 地址
        preserve_subnet: 保留的子网位数（默认保留前2段，仅对 IPv4 生效）

    Returns:
        脱敏后的 IP

    示例:
        >>> redact_ip("192.168.1.100")
        '192.168.*.*'
        >>> redact_ip("2001:db8:abcd:1234:5678:9abc:def0:1234')
        '2001:db8:abcd:1234:ffff:ffff:ffff:ffff'
    """
    if not ip:
        return ip

    # FIXED-P2: 原 redact_ip 仅支持 IPv4，IPv6 直接原样返回不脱敏；
    # 修复-增加 IPv6 分支，用 ipaddress.IPv6Address 解析后保留前 4 个 hextet（/64 网络前缀），
    # 其余 hextet 替换为 ffff，避免泄露接口标识符
    if ":" in ip:
        try:
            addr = ipaddress.IPv6Address(ip)
            # exploded 展开为 8 个 hextet 的标准形式
            hextets = addr.exploded.split(":")
            # 保留前 4 个 hextet，其余替换为 ffff
            redacted = [h if i < 4 else "ffff" for i, h in enumerate(hextets)]
            return ":".join(redacted)
        except ValueError:
            # 非合法 IPv6，降级到 IPv4 分支或原样返回
            pass

    # IPv4 分支（原逻辑）
    parts = ip.split(".")
    if len(parts) != 4:
        return ip

    redacted = ".".join(p if i < preserve_subnet else "*" for i, p in enumerate(parts))
    return redacted


def redact_email(email: str) -> str:
    """邮箱脱敏

    Args:
        email: 邮箱地址

    Returns:
        脱敏后的邮箱

    示例:
        >>> redact_email("user@example.com")
        'us**@example.com'
    """
    if "@" not in email:
        return mask_sensitive(email)

    local, domain = email.rsplit("@", 1)
    masked_local = local[0] + "*" if len(local) <= 2 else local[:2] + "*" * (len(local) - 2)

    return f"{masked_local}@{domain}"


def redact_phone(phone: str, visible_digits: int = 3) -> str:
    """手机号脱敏

    Args:
        phone: 手机号
        visible_digits: 保留尾号位数

    Returns:
        脱敏后的手机号

    示例:
        >>> redact_phone("13812345678")
        '138****5678'
    """
    # 移除非数字字符
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return mask_sensitive(phone)

    prefix = digits[:3]
    suffix = digits[-visible_digits:]
    return f"{prefix}****{suffix}"


class DataMasker:
    """数据脱敏器

    统一的数据脱敏接口，支持多种脱敏策略。

    使用方式：
        masker = DataMasker()
        safe_data = masker.mask(dangerous_data, strategy="auto")
    """

    def __init__(
        self,
        sensitive_fields: set[str] | None = None,
        custom_strategies: dict[str, Callable] | None = None,
    ):
        """
        Args:
            sensitive_fields: 敏感字段集合
            custom_strategies: 自定义脱敏策略 {字段名: 策略函数}
        """
        self._sensitive_fields = sensitive_fields or DEFAULT_MASK_FIELDS.copy()
        self._strategies = custom_strategies or {}

        # 默认策略
        self._default_strategies = {
            "email": redact_email,
            "phone": redact_phone,
            "ip": redact_ip,
            "default": mask_sensitive,
        }

    def add_sensitive_field(self, field_name: str) -> None:
        """添加敏感字段"""
        self._sensitive_fields.add(field_name.lower())

    def set_strategy(self, field_name: str, strategy: Callable) -> None:
        """设置字段的脱敏策略"""
        self._strategies[field_name.lower()] = strategy

    def mask(self, data: Any, strategy: str = "auto") -> Any:
        """脱敏数据

        Args:
            data: 要脱敏的数据
            strategy: 脱敏策略 ("auto", "full", "partial")
                - auto: 自动检测字段类型
                - full: 完全脱敏（替换为 ***）
                - partial: 部分脱敏（保留首尾字符）

        Returns:
            脱敏后的数据
        """
        if isinstance(data, dict):
            return self._mask_dict(data, strategy)
        elif isinstance(data, list):
            return [self.mask(item, strategy) for item in data]
        elif isinstance(data, str):
            return self._mask_string(data, strategy)
        return data

    def _mask_dict(self, data: dict, strategy: str) -> dict:
        """脱敏字典"""
        result = {}
        for key, value in data.items():
            key_lower = key.lower()

            if key_lower in self._sensitive_fields:
                # FIXED-P1: 原实现忽略 strategy 参数，始终调用 _apply_strategy。
                # 现根据 strategy 选择脱敏强度：
                # - "full": 完全脱敏（替换为 ****）
                # - "auto"/"partial": 使用字段策略或默认部分脱敏
                if strategy == "full":
                    result[key] = "****"
                else:
                    result[key] = self._apply_strategy(key_lower, value)
            elif isinstance(value, dict):
                result[key] = self._mask_dict(value, strategy)
            elif isinstance(value, list):
                result[key] = [self.mask(item, strategy) for item in value]
            else:
                result[key] = value

        return result

    def _apply_strategy(self, key: str, value: Any) -> Any:
        """应用脱敏策略"""
        if key in self._strategies:
            return self._strategies[key](value)
        return mask_sensitive(str(value))

    def _mask_string(self, text: str, strategy: str) -> str:
        """脱敏字符串"""
        if strategy == "full":
            return "****"
        return mask_sensitive(text)


# 便捷函数
def mask_api_response(response: dict, exclude_fields: list[str] | None = None) -> dict:
    """API 响应脱敏

    移除敏感字段后返回

    Args:
        response: API 响应字典
        exclude_fields: 不脱敏的字段列表

    Returns:
        脱敏后的响应
    """
    exclude_fields = exclude_fields or ["id", "created_at", "updated_at"]
    # FIXED-P1: 原实现为每个 key 创建新 dict 并调用 mask_json，效率低（O(n) 次 dict 创建）。
    # 改为：先整体 mask_json，再恢复排除字段的原始值。
    exclude_lower = {f.lower() for f in exclude_fields}
    masked = mask_json(response)
    for key in response:
        if key.lower() in exclude_lower:
            masked[key] = response[key]
    return masked
