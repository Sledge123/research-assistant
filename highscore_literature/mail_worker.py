"""只发送文献全文下载提醒的独立邮件执行器。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
import hashlib
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPHandler, HTTPRedirectHandler

from highscore_downloader.mail_settings import validate_email, validate_mail_settings
from .worker import WorkerError

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = timezone(timedelta(hours=8))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WorkerError('文献邮件服务返回重定向，已停止。')


class ApiClient:
    def __init__(self, token, server_url):
        parsed = urlsplit(server_url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path != '/highscore/api' or parsed.port == 0):
            raise WorkerError('文献邮件服务地址配置无效。')
        if not isinstance(token, str) or len(token) < 32 or re.search(r'\s', token):
            raise WorkerError('文献邮件执行器凭据配置无效。')
        self.server_url = server_url.rstrip('/')
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()), HTTPHandler())
        self.headers = {'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'}

    def post(self, path, payload):
        request = Request(self.server_url+path, json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf-8'),
                          self.headers,method='POST')
        try:
            with self.opener.open(request,timeout=15) as response:
                raw = response.read(256*1024+1)
            if len(raw)>256*1024:
                raise WorkerError('文献邮件服务响应超出限制。')
            result = json.loads(raw)
            if not isinstance(result,dict):
                raise ValueError()
            return result
        except WorkerError:
            raise
        except (HTTPError,URLError,TimeoutError,OSError,ValueError,TypeError):
            raise WorkerError('文献邮件服务暂不可用。') from None

    def claim(self, worker_id):
        return self.post('/worker/literature/reminders/claim',{'worker_id':worker_id})

    def complete(self, reminder_id, lease_token, state):
        if not re.fullmatch(r'[a-f0-9]{32}',str(reminder_id)):
            raise WorkerError('下载提醒任务编号无效。')
        return self.post(f'/worker/literature/reminders/{reminder_id}/complete',
                         {'lease_token':lease_token,'state':state})


class ReminderMailer:
    def __init__(self, root, settings, site_url, *, smtp_factory=None):
        self.root = Path(root).resolve()
        self.settings = validate_mail_settings(settings)
        if not self.settings.enabled:
            raise WorkerError('服务器邮件发送已停用。')
        parsed = urlsplit(site_url)
        if (parsed.scheme != 'https' or not parsed.netloc or parsed.path != '/' or parsed.query or parsed.fragment
                or parsed.username is not None or parsed.password is not None):
            raise WorkerError('文献服务入口必须为 HTTPS 根网址。')
        self.site_url = site_url
        self.smtp_factory = smtp_factory or smtplib.SMTP_SSL

    @staticmethod
    def _safe_id(value):
        if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{32}',value):
            raise WorkerError('下载提醒任务编号无效。')
        return value

    def _receipt(self, reminder_id):
        key = hashlib.sha256(reminder_id.encode('ascii')).hexdigest()
        directory = self.root/'output/literature-mail'
        directory.mkdir(parents=True,exist_ok=True)
        os.chmod(directory,0o700)
        return directory/(key+'.json')

    @staticmethod
    def _save(target, receipt):
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=target.parent,
                                             prefix='.reminder-',suffix='.tmp',delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary,0o600)
                json.dump(receipt,stream,ensure_ascii=False,allow_nan=False)
                stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,target)
            os.chmod(target,0o600)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def send(self, reminder):
        required = {'id','task_id','title','recipient','download_expires_at','attempt'}
        if not isinstance(reminder,dict) or not required.issubset(reminder):
            raise WorkerError('下载提醒内容无法核验。')
        reminder_id = self._safe_id(reminder['id'])
        task_id = self._safe_id(reminder['task_id'])
        recipient = validate_email(reminder['recipient'])
        title = ' '.join(str(reminder['title']).split())
        if not title or len(title)>1000:
            raise WorkerError('下载提醒题名无法核验。')
        expires = float(reminder['download_expires_at'])
        if not (0 < expires < 10**11):
            raise WorkerError('下载期限无法核验。')
        attempt = reminder['attempt']
        if type(attempt) is not int or not 1<=attempt<=3:
            raise WorkerError('下载提醒重试次数无法核验。')
        fingerprint = hashlib.sha256(json.dumps(
            [reminder_id,task_id,recipient,title,expires,self.site_url,self.settings.username],
            ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
        target = self._receipt(reminder_id)
        receipt = {'fingerprint':fingerprint,'status':'prepared','attempt':attempt}
        if target.exists():
            try:
                prior = json.loads(target.read_text(encoding='utf-8'))
                if (prior.get('fingerprint') != fingerprint or prior.get('status') not in {'prepared','sending','submitted','failed','unknown'}
                        or type(prior.get('attempt')) is not int):
                    raise ValueError()
                if prior['status'] in {'sending','submitted','unknown'}:
                    if prior['status']=='sending':
                        prior['status']='unknown';self._save(target,prior)
                    return prior['status']
                if prior['status']=='failed' and attempt<=prior['attempt']:
                    return 'failed'
                receipt['attempt']=attempt
            except (OSError,ValueError,TypeError):
                raise WorkerError('下载提醒本地投递回执无法核验，已禁止自动重发。') from None

        expiry = datetime.fromtimestamp(expires,SHANGHAI).strftime('%Y年%m月%d日 %H:%M')
        message = EmailMessage()
        sender = validate_email(self.settings.username)
        message['From'],message['To'] = sender,recipient
        message['Subject'] = '科研助手：文献全文已获取，请及时下载'
        message['Date'] = format_datetime(datetime.now(timezone.utc))
        message['Message-ID'] = make_msgid(domain=sender.split('@',1)[1])
        message.set_content(
            f'您求助的论文《{title}》已成功获取并通过文件校验。\n\n'
            f'请登录科研助手，在文献任务中下载全文。下载有效期至北京时间 {expiry}。\n\n'
            f'登录入口：{self.site_url}\n\n'
            '此邮件仅用于提醒下载，请勿回复。',charset='utf-8')

        smtp = None
        submitted = False
        try:
            receipt.update(status='sending')
            self._save(target,receipt)
            smtp = self.smtp_factory(self.settings.smtp_host,self.settings.smtp_port,timeout=30,
                                     context=ssl.create_default_context())
            smtp.login(self.settings.username,self.settings.authorization_code)
            submitted = True
            refused = smtp.send_message(message,from_addr=sender,to_addrs=[recipient])
            if refused:
                receipt.update(status='failed');self._save(target,receipt)
                return 'failed'
            receipt.update(status='submitted');self._save(target,receipt)
            return 'submitted'
        except (smtplib.SMTPAuthenticationError,smtplib.SMTPSenderRefused,smtplib.SMTPRecipientsRefused,
                smtplib.SMTPDataError,smtplib.SMTPConnectError,smtplib.SMTPHeloError):
            receipt.update(status='failed')
            try:self._save(target,receipt)
            except OSError:pass
            return 'failed'
        except Exception:
            receipt.update(status='unknown' if submitted else 'failed')
            try:self._save(target,receipt)
            except OSError:pass
            return receipt['status']
        finally:
            if smtp is not None:
                try:smtp.quit()
                except Exception:
                    try:smtp.close()
                    except Exception:pass


@contextmanager
def worker_lock(root):
    path = Path(root)/'output/literature-mail/worker.lock'
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor = os.open(path,os.O_RDWR|os.O_CREAT,0o600)
    locked = False
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor,b'0')
            os.lseek(descriptor,0,os.SEEK_SET)
            try:
                msvcrt.locking(descriptor,msvcrt.LK_NBLCK,1);locked=True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB);locked=True
            except BlockingIOError:
                pass
        yield locked
    finally:
        if locked:
            if os.name == 'nt':
                import msvcrt
                os.lseek(descriptor,0,os.SEEK_SET)
                msvcrt.locking(descriptor,msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(descriptor,fcntl.LOCK_UN)
        os.close(descriptor)


class Worker:
    def __init__(self,client,mailer,worker_id='literature-mail-linux'):
        self.client,self.mailer,self.worker_id = client,mailer,worker_id

    def run_once(self):
        with worker_lock(self.mailer.root) as acquired:
            if not acquired:
                return False
            claim = self.client.claim(self.worker_id)
            reminder = claim.get('reminder')
            if reminder is None:
                return False
            token = claim.get('lease_token')
            if not isinstance(token,str) or not 32<=len(token)<=128 or any(c.isspace() for c in token):
                raise WorkerError('下载提醒租约无法核验。')
            state = self.mailer.send(reminder)
            if state not in {'submitted','failed','unknown'}:
                state='unknown'
            self.client.complete(reminder['id'],token,state)
            print({'submitted':'下载提醒已由 SMTP 接受','failed':'下载提醒发送失败','unknown':'下载提醒状态待核查'}[state],flush=True)
            return True


def main(argv=None):
    parser = argparse.ArgumentParser(description='文献全文下载提醒邮件执行器')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--run',action='store_true',help='持续领取并发送已核验全文提醒')
    group.add_argument('--once',action='store_true',help='最多领取一封提醒')
    group.add_argument('--check-config',action='store_true',help='离线核验配置，不联网或发送邮件')
    args = parser.parse_args(argv)
    try:
        settings_path = os.environ.get('LITERATURE_MAIL_SETTINGS_PATH','runtime/mail.encrypted.json')
        settings = __import__('highscore_database.mail_config',fromlist=['load_settings']).load_settings(ROOT,settings_path)
        site_url = os.environ.get('LITERATURE_MAIL_SITE_URL','https://example.com/')
        mailer = ReminderMailer(ROOT,settings,site_url)
        if args.check_config:
            token=os.environ.get('LITERATURE_MAIL_WORKER_TOKEN','')
            server=os.environ.get('LITERATURE_MAIL_SERVER_URL','http://web:8787/highscore/api')
            ApiClient(token,server)
            print('PASS 文献提醒邮件配置已核验；未联网、未发送邮件。')
            return 0
        client = ApiClient(os.environ.get('LITERATURE_MAIL_WORKER_TOKEN',''),
                           os.environ.get('LITERATURE_MAIL_SERVER_URL','http://web:8787/highscore/api'))
        worker=Worker(client,mailer)
        if args.once:
            worker.run_once();return 0
        while True:
            try:worker.run_once()
            except WorkerError:print('文献提醒执行轮次失败，将稍后重试。',flush=True)
            time.sleep(5)
    except Exception:
        print('FAIL 文献提醒邮件执行器配置或运行失败；未输出配置内容。')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
