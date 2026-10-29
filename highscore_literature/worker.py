"""独立文献执行器协议、续租和有界轮询；不复用 XRD 的令牌或执行锁。"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

from .engine import LiteratureEngine, LiteratureError, LeaseLost, SliceExpired, StateStore

FINAL_SECONDS = 15.0


class WorkerError(LiteratureError):
    pass


@dataclass(frozen=True)
class WorkerConfig:
    root: Path
    server_url: str
    token: str = field(repr=False)
    worker_id: str = 'literature-linux'


def load_config(root, environ=None):
    environ = os.environ if environ is None else environ
    server_url = environ.get('LITERATURE_SERVER_URL', 'http://web:8787/highscore/api').strip().rstrip('/')
    token = environ.get('LITERATURE_WORKER_TOKEN', '').strip()
    try:
        parsed = urlsplit(server_url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path.rstrip('/') != '/highscore/api' or parsed.port == 0):
            raise ValueError()
        if len(token) < 32 or len(token) > 4096 or re.search(r'\s', token):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise WorkerError('文献执行器地址或独立令牌配置无效，请检查部署配置。') from None
    return WorkerConfig(Path(root).resolve(), server_url, token)


class ApiClient:
    def __init__(self, config, transport=None):
        import httpx
        self.config = config
        self.request_budget = threading.local()
        self.client = httpx.Client(timeout=httpx.Timeout(25.0, connect=12.0), follow_redirects=False,
            headers={'Authorization': 'Bearer '+config.token}, transport=transport)

    def close(self):
        self.client.close()

    def _post(self, path, payload):
        try:
            options = {}
            until = getattr(self.request_budget, 'until', None)
            if until is not None:
                import httpx
                remaining = until-time.monotonic()
                if remaining <= 0:
                    raise LeaseLost()
                options['timeout'] = httpx.Timeout(min(25.0, remaining), connect=min(12.0, remaining))
            response = self.client.post(self.config.server_url+path, json=payload, **options)
            if response.status_code == 409:
                raise LeaseLost()
            if response.status_code != 200:
                raise WorkerError('文献服务未接受本轮请求，将在下一轮重新核验。', 'waiting')
            if len(response.content) > 256*1024:
                raise WorkerError('文献服务响应大小无法核验。')
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (LiteratureError, LeaseLost):
            raise
        except Exception:
            raise WorkerError('文献服务暂时无法连接，将稍后重试。', 'waiting') from None

    def call_before(self, until, callback):
        self.request_budget.until = until
        try:
            return callback()
        finally:
            del self.request_budget.until

    def claim(self, accounts_ready):
        return self._post('/worker/literature/claim', {'worker_id': self.config.worker_id, 'accounts_ready': bool(accounts_ready)})

    @staticmethod
    def _job_id(job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', job_id):
            raise WorkerError('文献任务 ID 格式无法核验。')
        return job_id

    def heartbeat(self, job_id, lease_token, stage=None):
        return self._post(f'/worker/literature/jobs/{self._job_id(job_id)}/heartbeat',
                          {'lease_token': lease_token, **({'stage': stage} if stage else {})})

    def update(self, job_id, lease_token, values):
        return self._post(f'/worker/literature/jobs/{self._job_id(job_id)}/update', {'lease_token': lease_token, **values})


class LeaseGuard:
    """后台续租和写前同步核验互相加锁；任一续租异常都不再提交结果。"""
    def __init__(self, client, job, token, lease_seconds=180, max_seconds=900):
        self.client, self.job, self.token = client, job, token
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.mutex = threading.Lock()
        try:
            seconds = float(lease_seconds)
            deadline = float(job['deadline'])
            budget = float(max_seconds)
            if (not math.isfinite(seconds) or not math.isfinite(deadline)
                    or not math.isfinite(budget) or seconds <= 0 or budget <= 0):
                raise ValueError()
            self.until = time.monotonic() + min(budget, max(0, deadline-time.time()))
            self.final_until = self.until + FINAL_SECONDS
            self.interval = max(.01, min(20.0, seconds/3))
        except (ValueError, TypeError, KeyError):
            raise WorkerError('文献租约时间无法核验。') from None
        self.thread = None
        self.stage = None

    def _heartbeat(self, allow_expired=False):
        until = self.final_until if allow_expired else self.until
        if time.monotonic() >= until:
            raise LeaseLost() if allow_expired else SliceExpired()
        if not self.mutex.acquire(timeout=max(0.0, until-time.monotonic())):
            raise LeaseLost() if allow_expired else SliceExpired()
        if self.stop.is_set() or self.lost.is_set() or time.monotonic() >= until:
            self.mutex.release()
            if self.stop.is_set() or self.lost.is_set():
                raise LeaseLost()
            raise LeaseLost() if allow_expired else SliceExpired()
        done = threading.Event()
        expired = threading.Event()
        def request():
            try:
                if self.stop.is_set():
                    return
                if time.monotonic() >= until:
                    expired.set()
                    return
                callback = lambda: self.client.heartbeat(self.job['id'], self.token,
                                                         **({'stage': self.stage} if self.stage else {}))
                value = self.client.call_before(until, callback) if isinstance(self.client, ApiClient) else callback()
                if not isinstance(value, dict) or value.get('cancel_requested') is not False:
                    raise LeaseLost()
            except Exception:
                self.lost.set()
            finally:
                self.mutex.release()
                done.set()
        # 传输异常或卡住也不能让父线程越过本轮预算；线程不会自行重发请求。
        thread = threading.Thread(target=request, name=threading.current_thread().name, daemon=True)
        thread.start()
        if not done.wait(timeout=max(0.0, until-time.monotonic())):
            raise LeaseLost() if allow_expired else SliceExpired()
        if expired.is_set():
            raise LeaseLost() if allow_expired else SliceExpired()
        if self.lost.is_set():
            raise LeaseLost()

    def _loop(self):
        while True:
            remaining = self.until-time.monotonic()
            if remaining <= 0 or self.stop.wait(min(self.interval, remaining)):
                return
            try:
                self._heartbeat()
            except (LeaseLost, SliceExpired):
                return

    def __enter__(self):
        self.check(force=True)
        self.thread = threading.Thread(target=self._loop, name='literature-lease', daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=.1)

    def check(self, force=False, allow_expired=False):
        if self.lost.is_set():
            raise LeaseLost()
        if not allow_expired and time.monotonic() >= self.until:
            raise SliceExpired()
        if force:
            self._heartbeat(allow_expired=allow_expired)
        if self.lost.is_set():
            raise LeaseLost()
        if time.monotonic() >= (self.final_until if allow_expired else self.until):
            raise LeaseLost() if allow_expired else SliceExpired()

    __call__ = check

    def remaining_seconds(self):
        self.check()
        return max(0.0, self.until-time.monotonic())

    def set_stage(self, stage):
        if stage not in {'fetching', 'awaiting_fulltext', 'renewing', 'verifying', 'retrying'}:
            raise WorkerError('文献业务阶段无法核验。')
        self.check()
        self.stage = stage
        self._heartbeat()

    def submit(self, values):
        """清理后再次同步核验；超时后只允许有限宽限期内的最后一次合法回传。"""
        self.final_until = min(self.final_until, time.monotonic() + FINAL_SECONDS)
        self.check(force=True, allow_expired=True)
        done = threading.Event()
        failed = threading.Event()
        def request():
            try:
                if self.lost.is_set():
                    raise LeaseLost()
                callback = lambda: self.client.update(self.job['id'], self.token, values)
                if isinstance(self.client, ApiClient):
                    self.client.call_before(self.final_until, callback)
                else:
                    callback()
            except Exception:
                failed.set()
            finally:
                done.set()
        threading.Thread(target=request, name='literature-update', daemon=True).start()
        if not done.wait(timeout=max(0.0, self.final_until-time.monotonic())) or failed.is_set():
            self.lost.set()
            raise LeaseLost()
        self.check(allow_expired=True)


@contextmanager
def worker_mutex(store):
    path = store.path('output/literature-worker/worker.lock')
    store._directory(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b'0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                pass
        yield locked
    finally:
        if locked:
            if os.name == 'nt':
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class Worker:
    def __init__(self, root, client=None, engine=None, accounts_provider=None, max_seconds=900):
        self.root = Path(root).resolve()
        self.store = StateStore(self.root)
        self.client = client or ApiClient(load_config(self.root))
        self.engine = engine or LiteratureEngine(self.root)
        self.accounts_provider = accounts_provider
        try:
            budget = float(max_seconds)
            if not math.isfinite(budget) or budget <= 0:
                raise ValueError()
            self.max_seconds = min(900.0, budget)
        except (ValueError, TypeError):
            raise WorkerError('文献单轮处理预算无法核验。') from None

    def _accounts(self):
        if self.accounts_provider is not None:
            return self.accounts_provider()
        from .credentials import load_accounts
        return load_accounts(self.root)

    def log(self, event, state=None):
        # 固定事件字典；不记录用户名、论文题名、响应体、异常或页面地址。
        if event not in {'账号配置暂不可用', '本轮服务连接失败', '租约已停止', '本轮任务已回传', '本轮状态需检查', '执行器已停止'}:
            event = '本轮状态需检查'
        path = self.store.path('output/literature-worker/worker.log')
        self.store._directory(path.parent)
        value = {'time': int(time.time()), 'event': event}
        if state in {'waiting', 'needs_selection', 'succeeded', 'failed', 'needs_attention'}:
            value['state'] = state
        try:
            if path.exists() and path.stat().st_size > 256*1024:
                previous = self.store.path('output/literature-worker/worker.previous.log')
                os.replace(path, previous)
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(value, ensure_ascii=False)+'\n')
            os.chmod(path, 0o600)
        except OSError:
            pass

    def run_once(self):
        with worker_mutex(self.store) as acquired:
            if not acquired:
                return False
            try:
                # 本地保留期限不依赖服务器可达；持有进程锁后先独立清理。
                self.store.cleanup()
            except Exception:
                self.log('本轮状态需检查')
                return False
            try:
                accounts = self._accounts()
            except Exception:
                accounts = []
                self.log('账号配置暂不可用')
            try:
                claim = self.client.claim(accounts_ready=bool(accounts))
                if not isinstance(claim, dict) or not claim.get('job'):
                    return False
                job, token = claim['job'], claim['lease_token']
                ApiClient._job_id(job.get('id'))
                if not isinstance(token, str) or not 1 <= len(token) <= 128:
                    raise WorkerError('文献租约令牌格式无法核验。')
                with LeaseGuard(self.client, job, token, claim.get('lease_seconds', 180), self.max_seconds) as guard:
                    if not accounts:
                        values = {'state': 'waiting', 'message': '科研通账号暂不可用，请管理员检查加密配置。'}
                    else:
                        if isinstance(self.engine, LiteratureEngine):
                            from .process_runner import run_engine_process
                            values = run_engine_process(self.engine, job, accounts, guard)
                        else:
                            # 离线测试可注入轻量 callable；真实引擎始终走可终止进程。
                            values = self.engine(job, guard)
                    if 'source_revision' in job:
                        values.setdefault('source_revision', job['source_revision'])
                    guard.submit(values)
                    self.log('本轮任务已回传', values.get('state'))
                return True
            except LeaseLost:
                self.log('租约已停止')
                return False
            except (WorkerError, SliceExpired):
                self.log('本轮服务连接失败')
                return False
            except Exception:
                self.log('本轮状态需检查')
                return False

    def run(self, stop=None):
        stop = stop or threading.Event()
        try:
            while not stop.is_set():
                self.run_once()
                stop.wait(5)
        except KeyboardInterrupt:
            pass
        finally:
            self.log('执行器已停止')
            close = getattr(self.client, 'close', None)
            if callable(close):
                close()

