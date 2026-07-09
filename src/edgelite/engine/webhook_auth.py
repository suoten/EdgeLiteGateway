"""HTTP Webhook 安全认证中间件"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

_ENV_VAR_PATTERN = re.compile(r"^\$\{(.+)\}$")

# FIXED-P1: 重放攻击防护 - 时间戳窗口和nonce缓存
_REPLAY_WINDOW_SECONDS = 300  # 允许5分钟时间偏差
_NONCE_CACHE_MAX_SIZE = 10000  # nonce缓存上限，防止内存无限增长


class WebhookAuthMiddleware:
    """Webhook认证中间件，支持Bearer Token和Basic Auth"""

    def __init__(self, mode: str = "none", token: str = "", username: str = "", password: str = ""):
        self._mode = mode
        self._token = self._resolve_env_var(token)
        self._username = self._resolve_env_var(username)
        self._password = self._resolve_env_var(password)
        # FIXED-P1: 重放攻击防护 - nonce缓存，记录已使用的nonce防止重放
        self._used_nonces: dict[str, float] = {}  # nonce -> 使用时间戳
        # 修复P1-8: verify_replay 中"检查存在性→写入"为读-改-写操作，并发请求可同时通过
        # nonce 唯一性检查从而绕过重放防护，使用 threading.Lock 保护（方法为同步，不能用 asyncio.Lock）
        self._nonce_lock = threading.Lock()
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

    def verify(self, authorization_header: str | None, timestamp: str | None = None, nonce: str | None = None, signature: str | None = None) -> bool:
        """验证Authorization头

        Args:
            authorization_header: Authorization头
            timestamp: 请求时间戳（用于重放防护，可选）
            nonce: 请求唯一标识（用于重放防护，可选）
            signature: HMAC签名（用于重放防护，可选）
        """
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

    def verify_replay(self, timestamp: str | None, nonce: str | None) -> bool:
        """FIXED-P1: 重放攻击防护 - 校验时间戳窗口和nonce唯一性

        Args:
            timestamp: 请求时间戳（Unix秒）
            nonce: 请求唯一标识

        Returns:
            是否通过重放防护校验
        """
        if timestamp is None or nonce is None:
            logger.warning("Replay protection: timestamp or nonce missing, rejecting request")
            return False
        try:
            ts = float(timestamp)
        except (ValueError, TypeError):
            logger.warning("Replay protection: invalid timestamp format: %s", timestamp)
            return False
        now = time.time()
        if abs(now - ts) > _REPLAY_WINDOW_SECONDS:
            logger.warning("Replay protection: timestamp out of window (now=%f, ts=%f, diff=%f)", now, ts, now - ts)
            return False
        # 修复P1-8: 用 threading.Lock 保护 _used_nonces 的"检查→清理→写入"读-改-写操作，
        # 避免并发请求同时通过 nonce 唯一性检查而绕过重放防护
        with self._nonce_lock:
            # 清理过期nonce
            if len(self._used_nonces) > _NONCE_CACHE_MAX_SIZE:
                expired_cutoff = now - _REPLAY_WINDOW_SECONDS
                self._used_nonces = {n: t for n, t in self._used_nonces.items() if t > expired_cutoff}
            if nonce in self._used_nonces:
                logger.warning("Replay protection: nonce already used: %s", nonce)
                return False
            self._used_nonces[nonce] = now
        return True

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
