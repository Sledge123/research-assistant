"""Linux真实Playwright离线回收：只打开about:blank，不登录、不提取、不发布。"""
import importlib.util
import json
import os
import signal
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

from highscore_literature.engine import LiteratureEngine
from highscore_literature.worker import Worker

ROOT = Path(__file__).resolve().parents[1]


def descendants(parent):
    rows = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            tail = path.read_text().rsplit(') ', 1)[1].split()
            rows[int(path.parent.name)] = int(tail[1])
        except (OSError, ValueError, IndexError):
            continue
    found = {parent}
    while True:
        expanded = found | {pid for pid, ppid in rows.items() if ppid in found}
        if expanded == found:
            return sorted(found)
        found = expanded


class RealBrowserEngine(LiteratureEngine):
    def __init__(self, root, abrupt=False):
        super().__init__(root)
        self.abrupt = abrupt

    def __call__(self, job, guard):
        from playwright.sync_api import sync_playwright
        guard()
        manager = sync_playwright().start()
        browser = manager.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto('about:blank', wait_until='domcontentloaded', timeout=5000)
        guard()
        marker = self.root/'browser-processes.json'
        pids = descendants(os.getpid())
        marker.write_text(json.dumps(pids), encoding='utf-8')
        # 有意模拟没有执行close的正常返回或崩溃，父进程必须回收真实脱离组浏览器。
        if self.abrupt:
            # 模拟关闭卡住的真实浏览器，不能指望驱动退出后的正常graceful close。
            for pid in pids:
                if pid != os.getpid() and 'chrome' in (Path('/proc')/str(pid)/'comm').read_text():
                    os.kill(pid, signal.SIGSTOP)
            os._exit(19)
        return {'state': 'waiting', 'message': '真实浏览器离线回收验证'}


@unittest.skipUnless(sys.platform.startswith('linux') and importlib.util.find_spec('playwright'),
                     '仅在装有Playwright的Linux文献镜像验证真实浏览器')
class RealBrowserProcessTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT/'.cache/tmp'
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache, prefix='real-browser-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_round(self, abrupt=False):
        job = {'id': 'synthetic-real-browser', 'title': 'Synthetic offline browser article',
               'canonical_title': 'Synthetic offline browser article', 'deadline': time.time()+120}
        client = Mock()
        client.claim.return_value = {'job': job, 'lease_token': 'synthetic-lease', 'lease_seconds': 180}
        client.heartbeat.return_value = {'cancel_requested': False}
        worker = Worker(self.root, client=client, engine=RealBrowserEngine(self.root, abrupt),
                        accounts_provider=lambda: [{'account_ref': 'synthetic-a'}], max_seconds=20)
        self.assertTrue(worker.run_once())
        pids = json.loads((self.root/'browser-processes.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(pids), 3, '必须实际启动Python、驱动和Chromium')
        until = time.monotonic()+2
        while time.monotonic() < until and any(Path('/proc') .joinpath(str(pid)).exists() for pid in pids):
            time.sleep(.02)
        self.assertFalse([pid for pid in pids if (Path('/proc')/str(pid)).exists()],
                         '本轮真实浏览器、驱动或僵尸进程仍存活')
        client.update.assert_called_once()

    def test_repeated_real_browsers_leave_no_live_or_zombie_descendants(self):
        for _ in range(3):
            self.run_round()

    def test_abrupt_engine_exit_also_cleans_real_detached_browser(self):
        self.run_round(abrupt=True)


if __name__ == '__main__':
    unittest.main()
