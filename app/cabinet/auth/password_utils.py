"""Password hashing utilities using bcrypt."""

import bcrypt

from app.utils.password_executor import PasswordExecutor


BCRYPT_ROUNDS = 12
BCRYPT_MAX_PASSWORD_BYTES = 72
password_executor = PasswordExecutor()


def validate_password_bytes(password: str) -> str:
    if len(password.encode('utf-8')) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError('Password must be at most 72 UTF-8 bytes')
    return password


def hash_password(password: str) -> str:
    """
    Hash a password using bcrypt.

    Args:
        password: Plain text password

    Returns:
        Hashed password string
    """
    password_bytes = validate_password_bytes(password).encode('utf-8')
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    """
    Verify a password against its hash.

    Args:
        password: Plain text password to verify
        password_hash: Previously hashed password

    Returns:
        True if password matches, False otherwise
    """
    try:
        password_bytes = validate_password_bytes(password).encode('utf-8')
        hash_bytes = password_hash.encode('utf-8')
        return bcrypt.checkpw(password_bytes, hash_bytes)
    except (ValueError, TypeError):
        return False


async def hash_password_async(password: str) -> str:
    """Hash outside the event loop, rejecting oversized input before admission."""
    validate_password_bytes(password)
    return await password_executor.run(hash_password, password)


async def verify_password_async(password: str, password_hash: str) -> bool:
    """Verify outside the event loop without accepting oversized bcrypt input."""
    try:
        validate_password_bytes(password)
    except (ValueError, TypeError):
        return False
    return await password_executor.run(verify_password, password, password_hash)
