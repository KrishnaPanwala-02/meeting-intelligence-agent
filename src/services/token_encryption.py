import os

from cryptography.fernet import Fernet, InvalidToken


def _get_fernet() -> Fernet:
    """Return the Fernet cipher using the configured encryption key."""
    key = os.getenv("CALENDAR_TOKEN_ENCRYPTION_KEY", "").strip()

    if not key:
        raise RuntimeError(
            "CALENDAR_TOKEN_ENCRYPTION_KEY is not configured."
        )

    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "CALENDAR_TOKEN_ENCRYPTION_KEY is invalid."
        ) from exc


def encrypt_token(token: str) -> str:
    """Encrypt a calendar OAuth token before database storage."""
    if not token:
        raise ValueError("Token must not be empty.")

    return _get_fernet().encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted_token: str) -> str:
    """Decrypt a stored calendar OAuth token when needed."""
    if not encrypted_token:
        raise ValueError("Encrypted token must not be empty.")

    try:
        return _get_fernet().decrypt(
            encrypted_token.encode("utf-8")
        ).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "Token decryption failed. Check the encryption key."
        ) from exc
