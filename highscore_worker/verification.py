"""独立的邮箱验证模板和投递账本，不允许自定义邮件内容。"""
from email.message import EmailMessage
import hashlib
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import tempfile
from urllib.parse import parse_qsl, urlsplit

from highscore_downloader.mail_settings import validate_email, validate_mail_settings
from .config import WorkerError


def validate_verification_payload(payload, public_url):
    if not isinstance(payload, dict) or set(payload) != {'to', 'verify_url'}:
        raise WorkerError('验证邮件仅允许固定收件人和验证链接。')
    recipient = validate_email(payload['to'])
    link = payload['verify_url']
    if not isinstance(link, str) or any(ch.isspace() for ch in link) or '\\' in link:
        raise WorkerError('验证链接无效。')
    expected, actual = urlsplit(public_url), urlsplit(link)
    try:
        pairs = parse_qsl(actual.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise WorkerError('验证链接查询参数无效。') from None
    if (actual.scheme != expected.scheme or actual.netloc.lower() != expected.netloc.lower()
            or actual.path != expected.path or actual.fragment or actual.username or actual.password
            or len(pairs) != 1 or pairs[0][0] != 'verify'
            or re.fullmatch(r'[A-Za-z0-9_-]{1,256}', pairs[0][1]) is None):
        raise WorkerError('验证链接必须精确指向已配置的网站验证入口。')
    return recipient, link


def atomic_json(target, value):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix='.worker-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class VerificationMailer:
    def __init__(self, root, settings, public_url, *, smtp_factory=None, submission_guard=None):
        self.root, self.settings, self.public_url = Path(root).resolve(), settings, public_url
        self.smtp_factory = smtp_factory or smtplib.SMTP_SSL
        self.submission_guard = submission_guard

    def send(self, job_id, payload, stop_event):
        recipient, link = validate_verification_payload(payload, self.public_url)
        settings = validate_mail_settings(self.settings, recipient)
        if not settings.enabled:
            raise WorkerError('管理员邮件设置已停用。')
        digest = lambda value: hashlib.sha256(value.encode('utf-8')).hexdigest()
        fingerprint = digest(json.dumps([recipient, link, settings.username]))
        target = self.root / 'output/verification-mail' / (digest(job_id) + '.json')
        state = {'fingerprint': fingerprint, 'status': 'prepared', 'message': '验证邮件尚未提交。'}
        if target.exists():
            try:
                previous = json.loads(target.read_text(encoding='utf-8'))
                if previous.get('fingerprint') != fingerprint or previous.get('status') not in {
                        'prepared', 'sending', 'sent', 'unknown', 'failed', 'stopped'}:
                    raise ValueError()
                if previous['status'] in {'sending', 'unknown', 'sent'}:
                    if previous['status'] == 'sending':
                        previous.update(status='unknown', message='上次投递在确认前中断；禁止自动重发。')
                        atomic_json(target, previous)
                    return previous
            except (OSError, TypeError, ValueError):
                raise WorkerError('验证邮件账本无法校验，禁止自动发送。') from None
        def save(status, message):
            state.update(status=status, message=message)
            atomic_json(target, state)
            return state
        if stop_event.is_set():
            return save('stopped', '任务已停止，未发送验证邮件。')
        message = EmailMessage()
        message['From'], message['To'] = settings.username, recipient
        message['Subject'] = 'HighScore 检索站：验证您的邮箱'
        message.set_content('请打开下面的链接验证邮箱：\n\n' + link + '\n\n如果并非您发起注册，请忽略此邮件。', charset='utf-8')
        smtp, submitted = None, False
        try:
            save('sending', '正在投递验证邮件，等待 SMTP 接受。')
            if stop_event.is_set():
                return save('stopped', '任务已停止，未提交验证邮件。')
            smtp = self.smtp_factory(settings.smtp_host, settings.smtp_port, timeout=30,
                                     context=ssl.create_default_context())
            smtp.login(settings.username, settings.authorization_code)
            if self.submission_guard is not None:
                if self.submission_guard() is False:
                    stop_event.set()
            if stop_event.is_set():
                return save('stopped', '任务已停止，未提交验证邮件。')
            submitted = True
            refused = smtp.send_message(message, from_addr=settings.username, to_addrs=[recipient])
            if refused:
                return save('failed', 'SMTP 服务器明确拒绝收件人。')
            return save('sent', 'SMTP 服务器已接受验证邮件；尚无收件箱送达证明。')
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused,
                smtplib.SMTPDataError, smtplib.SMTPConnectError, smtplib.SMTPHeloError):
            return save('failed', 'SMTP 服务器明确拒绝本次投递。')
        except Exception:
            state.update(status='unknown' if submitted else 'failed',
                         message='验证邮件提交后状态不确定；禁止自动重发。' if submitted else '连接或认证未完成，验证邮件未提交。')
            try:
                atomic_json(target, state)
            except Exception:
                pass  # 原有 sending 回执保留，重启后仍按 unknown 处理。
            return state
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    try:
                        smtp.close()
                    except Exception:
                        pass
