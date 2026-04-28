"""HTTP Webhook 安全认证中间件"""

from __future__ import annotations
import base64
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

_ENV_VAR_PATTERN = re.compile(r"^\$\{(.+)\}$")


class WebhookAuthMiddleware:
    """Webhook认证中间件，支持Bearer Token和Basic Auth"""

    def __init__(self, mode: str = "none", token: str = "", username: str = "", password: str = ""):
        self._mode = mode
        self._token = self._resolve_env_var(token)
        self._username = self._resolve_env_var(username)
        self._password = self._resolve_env_var(password)

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

    def verify(self, authorization_header: Optional[str]) -> bool:
        """验证Authorization头"""
        if self._mode == "none":
            return True
        if not authorization_header:
            return False
        if self._mode == "bearer":
            return self._verify_bearer(authorization_header)
        if self._mode == "basic":
            return self._verify_basic(authorization_header)
        logger.warning("未知的认证模式: %s", self._mode)
        return False

    def _verify_bearer(self, header: str) -> bool:
        if not header.startswith("Bearer "):
            return False
        token = header[7:].strip()
        return token == self._token

    def _verify_basic(self, header: str) -> bool:
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
            username, password = decoded.split(":", 1)
            return username == self._username and password == self._password
        except Exception:
            return False
