"""密码哈希与验证"""

from passlib.context import CryptContext

_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(password: str) -> str:
    """生成bcrypt密码哈希"""
    return _context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return _context.verify(plain_password, hashed_password)
