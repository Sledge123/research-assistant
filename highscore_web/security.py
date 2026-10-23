"""会话、密码和令牌；此模块不保存或输出明文凭据。"""
import hashlib
import hmac
import secrets


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_token():
    return secrets.token_urlsafe(32)


def password_hash(password):
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 600_000)
    return f"pbkdf2_sha256$600000${salt}${hashed.hex()}"


def password_matches(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False
