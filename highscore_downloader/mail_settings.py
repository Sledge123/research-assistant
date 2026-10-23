"""单独的本地邮件配置；授权码只以当前 Windows 用户 DPAPI 密文保存。"""
from dataclasses import dataclass, field, replace
import base64
import json
import os
from pathlib import Path
import re
import tempfile


@dataclass(frozen=True)
class MailSettings:
    username: str = ""
    authorization_code: str = field(default="", repr=False)
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_ssl: bool = True
    recipient: str = ""
    enabled: bool = True
    message: str = ""


_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*\Z")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def validate_email(value):
    """仅接受一个 ASCII 裸邮箱；保留 localpart 大小写。"""
    if (not isinstance(value, str) or not value.isascii() or value.count("@") != 1
            or len(value) > 254 or any(character.isspace() for character in value)):
        raise ValueError("请输入一个有效的 ASCII 邮箱，不支持多个地址或换行。")
    local, domain = value.split("@")
    if (len(local) > 64 or not _LOCAL.fullmatch(local) or "." not in domain
            or len(domain) > 253 or not all(_LABEL.fullmatch(label) for label in domain.split("."))):
        raise ValueError("请输入一个有效的 ASCII 邮箱，不支持显示名称或多个地址。")
    return f"{local}@{domain.lower()}"


def validate_mail_settings(settings, recipient=None, *, require_credentials=True):
    if not isinstance(settings, MailSettings):
        raise ValueError("邮件配置格式无效。")
    username = validate_email(settings.username)
    host = settings.smtp_host
    if (not isinstance(host, str) or not host.isascii() or len(host) > 253
            or not all(_LABEL.fullmatch(label) for label in host.split("."))):
        raise ValueError("SMTP 服务器必须为有效的 ASCII 主机名。")
    if type(settings.smtp_port) is not int or not 1 <= settings.smtp_port <= 65535:
        raise ValueError("SMTP 端口必须为 1 到 65535 的整数。")
    if settings.smtp_ssl is not True:
        raise ValueError("邮件发送需要启用 SSL，并校验服务器证书。")
    if type(settings.enabled) is not bool:
        raise ValueError("邮件启用设置无效。")
    if (not isinstance(settings.authorization_code, str) or "\r" in settings.authorization_code
            or "\n" in settings.authorization_code or "\0" in settings.authorization_code
            or (require_credentials and not settings.authorization_code.strip())):
        raise ValueError("请在邮件设置中填写有效的 SMTP 授权码。")
    configured = validate_email(settings.recipient) if settings.recipient else ""
    if recipient is not None:
        validate_email(recipient)
    return replace(settings, username=username, smtp_host=host.lower(), recipient=configured, message="")


def _config_path(root):
    project = Path(root).resolve()
    path = project / "mail.local.json"
    if not project.is_dir() or path.is_symlink() or not path.resolve().is_relative_to(project):
        raise ValueError("邮件配置路径无效。")
    return path


def load_mail_settings(root):
    """读取失败保留原文件，返回安全空配置和固定中文提示。"""
    try:
        config = _config_path(root)
        if not config.exists():
            return MailSettings()
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("secret_protection") != "windows-dpapi":
            raise ValueError("unsupported")
        import win32crypt
        ciphertext = base64.b64decode(data["authorization_code_dpapi"], validate=True)
        code = win32crypt.CryptUnprotectData(ciphertext, None, None, None, 0)[1].decode("utf-8")
        settings = MailSettings(username=data["username"], authorization_code=code,
                                smtp_host=data["smtp_host"], smtp_port=data["smtp_port"],
                                smtp_ssl=data["smtp_ssl"], recipient=data.get("recipient", ""),
                                enabled=data.get("enabled", True))
        return validate_mail_settings(settings)
    except Exception:
        return MailSettings(message="邮件配置读取失败；请重新保存邮件设置。原文件已保留，未尝试发送。")


def save_mail_settings(root, settings):
    settings = validate_mail_settings(settings)
    temporary = None
    try:
        config = _config_path(root)
        import win32crypt
        protected = win32crypt.CryptProtectData(settings.authorization_code.encode("utf-8"),
                                               "HighScore SMTP", None, None, None, 0)
        data = {"schema_version": 1, "secret_protection": "windows-dpapi", "username": settings.username,
                "authorization_code_dpapi": base64.b64encode(protected).decode("ascii"),
                "smtp_host": settings.smtp_host, "smtp_port": settings.smtp_port,
                "smtp_ssl": settings.smtp_ssl, "recipient": settings.recipient, "enabled": settings.enabled}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=config.parent,
                                         prefix=".mail-config-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(config)
        return settings
    except Exception:
        raise ValueError("邮件配置保存失败；请检查项目目录权限及 Windows 用户加密服务。") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
