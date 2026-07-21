import hashlib
import hmac


def unsubscribe_token(message_id: int, destination: str, secret: str) -> str:
    value = f"{message_id}:{destination.lower()}".encode()
    return hmac.new(secret.encode(), value, hashlib.sha256).hexdigest()


def valid_unsubscribe_token(message_id: int, destination: str, token: str, secret: str) -> bool:
    expected = unsubscribe_token(message_id, destination, secret)
    return hmac.compare_digest(expected, token)
