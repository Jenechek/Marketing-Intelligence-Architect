"""Токены подтверждения опасных действий."""

import hashlib
import hmac
import secrets


def create_delete_confirmation_token(secret: bytes, site_id: int) -> str:
    """Создать непредсказуемый токен, связанный с идентификатором сайта."""

    nonce = secrets.token_urlsafe(32)
    signature = _sign_delete_confirmation(secret, site_id, nonce)
    return f"{nonce}.{signature}"


def validate_delete_confirmation_token(secret: bytes, site_id: int, token: str) -> bool:
    """Проверить подпись токена удаления для указанного сайта."""

    try:
        nonce, signature = token.split(".", maxsplit=1)
    except ValueError:
        return False

    if not nonce or not signature:
        return False

    expected_signature = _sign_delete_confirmation(secret, site_id, nonce)
    return hmac.compare_digest(signature, expected_signature)


def _sign_delete_confirmation(secret: bytes, site_id: int, nonce: str) -> str:
    payload = f"{site_id}:{nonce}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()
