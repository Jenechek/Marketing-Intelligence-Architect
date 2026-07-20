"""Подписанные токены для изменяющих и сетевых действий."""

import hashlib
import hmac
import secrets


DELETE_ACTION = "delete-site"
CHECK_AVAILABILITY_ACTION = "check-availability"
START_CRAWL_ACTION = "start-crawl"


def change_event_view_action(source: str, event_id: int, viewed: bool) -> str:
    """Однозначно связать токен с источником, событием и переходом."""

    transition = "view" if viewed else "unview"
    return f"change-event:{source}:{event_id}:{transition}"


def create_action_token(secret: bytes, site_id: int, action: str) -> str:
    """Создать непредсказуемый токен, связанный с сайтом и действием."""

    nonce = secrets.token_urlsafe(32)
    signature = _sign_action(secret, site_id, action, nonce)
    return f"{nonce}.{signature}"


def validate_action_token(secret: bytes, site_id: int, action: str, token: str) -> bool:
    """Проверить подпись токена для конкретного сайта и действия."""

    try:
        nonce, signature = token.split(".", maxsplit=1)
    except ValueError:
        return False

    if not nonce or not signature:
        return False

    expected_signature = _sign_action(secret, site_id, action, nonce)
    return hmac.compare_digest(signature, expected_signature)


def create_delete_confirmation_token(secret: bytes, site_id: int) -> str:
    """Создать непредсказуемый токен, связанный с идентификатором сайта."""

    return create_action_token(secret, site_id, DELETE_ACTION)


def validate_delete_confirmation_token(secret: bytes, site_id: int, token: str) -> bool:
    """Проверить подпись токена удаления для указанного сайта."""

    return validate_action_token(secret, site_id, DELETE_ACTION, token)


def _sign_action(secret: bytes, site_id: int, action: str, nonce: str) -> str:
    payload = f"{action}:{site_id}:{nonce}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()
