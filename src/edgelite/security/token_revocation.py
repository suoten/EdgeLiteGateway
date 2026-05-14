"""令牌撤销管理模块"""

from __future__ import annotations

import logging
import threading
import time

from edgelite.constants import _TOKEN_REVOCATION_MAX

logger = logging.getLogger(__name__)

_MAX_REVOKED_ENTRIES = _TOKEN_REVOCATION_MAX  # FIXED: 原问题-硬编码撤销上限，现引用constants.py
_CLEANUP_THRESHOLD = _TOKEN_REVOCATION_MAX * 8 // 10


class TokenRevocationManager:
    """内存令牌撤销管理器，支持撤销检查和过期自动清理"""

    def __init__(self):
        self._revoked_tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    def revoke_token(self, jti: str, exp: float | None = None) -> None:
        """撤销指定jti的令牌，exp为过期时间戳(Unix)"""
        with self._lock:
            self._revoked_tokens[jti] = exp or (time.time() + 86400)
            if len(self._revoked_tokens) > _MAX_REVOKED_ENTRIES:
                self._cleanup_expired()

    def is_token_revoked(self, jti: str) -> bool:
        """检查令牌是否已被撤销"""
        with self._lock:
            return jti in self._revoked_tokens

    def _cleanup_expired(self) -> None:
        """清理已过期的撤销记录"""
        now = time.time()
        expired_jtis = [jti for jti, exp in self._revoked_tokens.items() if exp <= now]
        for jti in expired_jtis:
            del self._revoked_tokens[jti]

        if len(self._revoked_tokens) > _CLEANUP_THRESHOLD:
            sorted_items = sorted(self._revoked_tokens.items(), key=lambda x: x[1])
            to_remove = len(self._revoked_tokens) - _CLEANUP_THRESHOLD
            for jti, _ in sorted_items[:to_remove]:
                del self._revoked_tokens[jti]

    def cleanup(self) -> None:
        """外部调用的清理方法"""
        with self._lock:
            self._cleanup_expired()


_revocation_manager = TokenRevocationManager()


def revoke_token(jti: str, exp: float | None = None) -> None:
    _revocation_manager.revoke_token(jti, exp)


def is_token_revoked(jti: str) -> bool:
    return _revocation_manager.is_token_revoked(jti)


def get_revocation_manager() -> TokenRevocationManager:
    return _revocation_manager
