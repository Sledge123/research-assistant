"""可被 spawn 导入的离线进程夹具；不使用网络或真实账号。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from highscore_literature.engine import LiteratureEngine


TITLE = 'Synthetic bounded literature process verification'


def cleanup_receipt_after_release(channel, release, receipt):
    """真实管道夹具：父方完成首次空轮询后才允许写入清理回执并退出。"""
    try:
        if channel.recv_bytes(32) == b'stop' and release.recv_bytes(32) == b'go':
            channel.send_bytes(receipt)
    finally:
        channel.close()
        release.close()


def marker(root, **values):
    target = Path(root) / 'output/literature-worker/process-marker.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(target.read_text(encoding='utf-8')) if target.exists() else {}
    target.write_text(json.dumps({**previous, 'pid': os.getpid(), **values}), encoding='utf-8')


class ProcessEngine(LiteratureEngine):
    def __init__(self, root, mode='return', size=0):
        super().__init__(root)
        self.mode, self.size = mode, size

    def __call__(self, job, guard):
        from highscore_literature.worker import worker_mutex
        with worker_mutex(self.store) as acquired:
            marker(self.root, mutex_acquired=acquired)
        if self.mode == 'hang':
            time.sleep(30)
        if self.mode.startswith('detached_'):
            ready = self.root / 'output/literature-worker/descendant-ready.json'
            code = (
                'import json,os,signal,sys,time; from pathlib import Path; '
                'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                'Path(sys.argv[1]).write_text(json.dumps({"pid":os.getpid(),'
                '"starttime":Path("/proc/self/stat").read_text().rsplit(")",1)[1].split()[19]})); '
                'time.sleep(30)')
            command = [sys.executable, '-B', '-c', code, str(ready)]
            if self.mode == 'detached_intermediate':
                bridge = ('import subprocess,sys; subprocess.Popen(sys.argv[1:], start_new_session=True, '
                          'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)')
                command = [sys.executable, '-B', '-c', bridge, *command]
            child = subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 3
            while True:
                try:
                    details = json.loads(ready.read_text(encoding='utf-8'))
                    break
                except (FileNotFoundError, ValueError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.005)
            marker(self.root, descendant=details['pid'], descendant_starttime=details['starttime'])
            if self.mode == 'detached_exit':
                os._exit(37)
            if self.mode in ('detached_hang', 'detached_cancel', 'detached_intermediate'):
                time.sleep(30)
        if self.mode in ('tree', 'tree_return'):
            child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            marker(self.root, descendant=child.pid)
            if self.mode == 'tree':
                time.sleep(30)
        if self.mode == 'exit':
            os._exit(37)
        if self.mode == 'raise':
            raise RuntimeError('password=synthetic-private-password token=synthetic-lease')
        if self.mode == 'progress':
            guard.set_stage('verifying')
        guard(force=True)
        marker(self.root, finished=True)
        return {'state': 'waiting', 'message': '离线子进程已完成',
                **({'pdf_base64': 'A' * self.size} if self.size else {})}


class BrowserFactory:
    def __init__(self, mode='waiting'):
        self.mode = mode

    def __call__(self, root, account, store, guard):
        return OfflineBrowser(root, account, store, guard, self.mode)


class OfflineBrowser:
    def __init__(self, root, account, store, guard, mode):
        self.root, self.account, self.store, self.guard, self.mode = root, account, store, guard, mode

    def __enter__(self):
        marker(self.root)
        return self

    def __exit__(self, *_):
        if self.mode == 'close_hang':
            time.sleep(30)

    def login(self):
        pass

    def list_requests(self):
        from highscore_literature.browser import Listing, RequestRecord
        records = [] if self.mode in ('publish_hang', 'empty') else [RequestRecord('12345', TITLE, '待应助')]
        return Listing(records, True)

    def publish(self, title, doi, before_submit):
        before_submit()
        target = Path(self.root) / 'output/literature-worker/publish-count.txt'
        count = int(target.read_text() if target.exists() else '0') + 1
        target.write_text(str(count), encoding='ascii')
        marker(self.root, published=True)
        time.sleep(30)

    def attachment(self, *args):
        return None
