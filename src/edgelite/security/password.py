"""密码哈希与验证"""

import base64
import hashlib

import bcrypt

# FIXED: rounds 从 14 降至 12，每次验证 ~400ms（14 rounds 需 ~1.5s 导致登录 3 秒超时）
# OWASP 推荐 10+，12 在安全性与性能间取得平衡
_BCRYPT_ROUNDS = 12
# FIXED-P0: bcrypt 最多处理 72 字节密码，超长密码被静默截断导致碰撞
# （如 "password123" + 任意后缀 与 "password123" 产生相同哈希）
_BCRYPT_MAX_PASSWORD_BYTES = 72


def _prehash_password(password: str) -> bytes:
    """对密码进行 SHA-256 预哈希，规避 bcrypt 72 字节限制。

    FIXED-P0: bcrypt 对超过 72 字节的密码静默截断，导致不同密码可能产生相同哈希。
    预哈希后所有密码均为固定 44 字节（base64 编码的 32 字节摘要），安全地绕过限制。
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """生成bcrypt密码哈希"""
    # FIXED-P1: 拒绝空密码，空密码哈希无安全意义且可能绕过认证
    if not password:
        raise ValueError("Password cannot be empty")
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    # FIXED-P0: 使用预哈希避免 72 字节截断
    return bcrypt.hashpw(_prehash_password(password), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    # FIXED-P1: 空密码或空哈希直接返回 False，避免无效输入触发异常
    if not plain_password or not hashed_password:
        return False
    try:
        hash_bytes = hashed_password.encode("utf-8")
        plain_bytes = plain_password.encode("utf-8")
        # FIXED(一般): 原问题-对遗留格式密码总是调用两次bcrypt(预哈希+直接)，耗时翻倍;
        # 修复-密码>72字节时只走预哈希路径(跳过第二次checkpw)，因为遗留直接bcrypt路径
        # 对超长密码本就会截断，不可能匹配预哈希格式的哈希值。
        # 注意: 密码<=72字节时仍需两次checkpw，这是有意为之的兼容性设计——
        # 预哈希格式与遗留直接bcrypt格式的哈希均为$2b$前缀，无法通过格式区分，
        # 且项目无密码迁移逻辑，必须保留遗留路径以兼容旧用户密码。
        # 优先尝试预哈希(新格式)，命中则直接返回，避免对新增密码的第二次调用。
        if bcrypt.checkpw(_prehash_password(plain_password), hash_bytes):
            return True
        # 向后兼容 - 短密码的遗留直接哈希格式
        if len(plain_bytes) <= _BCRYPT_MAX_PASSWORD_BYTES:
            return bcrypt.checkpw(plain_bytes, hash_bytes)
        return False
    except (ValueError, TypeError):
        # FIXED-P1: 无效哈希格式返回 False 而非抛出异常，避免时序信息泄漏
        return False
