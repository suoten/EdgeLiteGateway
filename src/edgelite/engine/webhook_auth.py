"""HTTP Webhook 安全认证中间件"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import re

logger = logging.getLogger(__name__)

_ENV_VAR_PATTERN = re.compile(r"^\$\{(.+)\}$")


class WebhookAuthMiddleware:
    """Webhook认证中间件，支持Bearer Token和Basic Auth"""

    def __init__(self, mode: str = "none", token: str = "", username: str = "", password: str = ""):
        self._mode = mode
        self._token = self._resolve_env_var(token)
        self._username = self._resolve_env_var(username)
        self._password = self._resolve_env_var(password)
        # FIXED: 原问题-认证凭据为空时初始化无告警，启动后才发现所有请求被拒
        if self._mode == "bearer" and not self._token:
            logger.warning("WebhookAuth: Bearer模式但token为空，所有webhook请求将被拒绝")
        if self._mode == "basic" and (not self._username or not self._password):
            logger.warning("WebhookAuth: Basic模式但username或password为空，所有webhook请求将被拒绝")

    @staticmethod
    def _resolve_env_var(value: str) -> str:
        """解析环境变量引用 ${VAR_NAME}"""
        m = _ENV_VAR_PATTERN.match(value)
        if m:
            env_val = os.environ.get(m.group(1), "")
            if not env_val:
                logger.warning("环境变量 %s 未设置", m.group(1))
            return env_val
        return value

    def verify(self, authorization_header: str | None) -> bool:
        """验证Authorization头"""
        if self._mode == "none":
            return True
        if not authorization_header:
            return False
        if self._mode == "bearer":
            if not self._token:
                logger.error("Bearer认证模式但token为空，拒绝所有请求")
                return False
            return self._verify_bearer(authorization_header)
        if self._mode == "basic":
            if not self._username or not self._password:
                logger.error("Basic认证模式但username或password为空，拒绝所有请求")
                return False
            return self._verify_basic(authorization_header)
        logger.warning("未知的认证模式: %s", self._mode)
        return False

    def _verify_bearer(self, header: str) -> bool:
        if not header.startswith("Bearer "):
            return False
        token = header[7:].strip()
        return hmac.compare_digest(token, self._token)

    def _verify_basic(self, header: str) -> bool:
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
            username, password = decoded.split(":", 1)
            return hmac.compare_digest(username, self._username) and hmac.compare_digest(
                password, self._password
            )
        except Exception as e:  # FIXED: 原问题-HMAC认证异常静默返回False，可能掩盖配置错误
            logger.debug("Basic认证解码失败: %s", e)
            return False
