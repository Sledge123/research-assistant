"""共享文件锁的互斥、计算继承和Linux进程组清理。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from highscore_science.lock import ScienceLock, stop_process

ROOT = Path(__file__).resolve().parents[1]


class ScienceLockTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'; cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='science-lock-', dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_independent_descriptors_exclude_each_other_until_holder_closes(self):
        with ScienceLock(self.root) as first, ScienceLock(self.root) as second:
            self.assertTrue(first.acquire()); self.assertFalse(second.acquire())
            self.assertTrue(first.acquire())
            first.close()
            self.assertTrue(second.acquire())

    def test_lock_path_must_stay_inside_project_and_no_child_launch_before_acquire(self):
        with self.assertRaises(ValueError):
            ScienceLock(self.root, '../outside.lock')
        with ScienceLock(self.root) as physical, self.assertRaises(RuntimeError):
            physical.child_options()

    def wait_json(self, marker):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                return json.loads(marker.read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError):
                time.sleep(.01)
        self.fail('锁测试子进程未就绪。')

    @unittest.skipUnless(sys.platform == 'linux', '继承flock须在Linux实际执行')
    def test_compute_keeps_flock_after_worker_abruptly_exits(self):
        marker = self.root / 'inherited.json'
        child = '''import json,os,sys,time
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid()}))
time.sleep(60)
'''
        parent = '''import os,subprocess,sys,time
from pathlib import Path
from highscore_science.lock import ScienceLock
physical=ScienceLock(Path(sys.argv[1]))
assert physical.acquire()
process=subprocess.Popen([sys.executable,'-B','-c',sys.argv[3],sys.argv[2]],start_new_session=True,
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**physical.child_options())
while not Path(sys.argv[2]).exists():time.sleep(.01)
os._exit(0)
'''
        process = subprocess.Popen([sys.executable, '-B', '-c', parent, str(self.root), str(marker), child], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        details = self.wait_json(marker)
        def cleanup():
            try:
                os.killpg(details['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.addCleanup(cleanup)
        process.wait(timeout=5)
        with ScienceLock(self.root) as contender:
            # 数据库任务即使已经到期，也不能解开计算进程仍持有的内核锁。
            self.assertFalse(contender.acquire())
            cleanup()
            deadline = time.monotonic() + 5
            while not contender.acquire() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(contender.acquire())

    @unittest.skipUnless(sys.platform == 'linux', '进程组回收须在Linux实际执行')
    def test_group_cleanup_kills_descendant_even_after_group_leader_exits(self):
        marker = self.root / 'descendant.json'
        child = '''import json,os,signal,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid()}))
time.sleep(60)
'''
        leader = '''import subprocess,sys,time
from pathlib import Path
subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]],pass_fds=(int(sys.argv[3]),),
 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
while not Path(sys.argv[2]).exists():time.sleep(.01)
'''
        with ScienceLock(self.root) as physical:
            self.assertTrue(physical.acquire())
            process = subprocess.Popen([sys.executable, '-B', '-c', leader, child, str(marker), str(physical.fd)],
                start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, **physical.child_options())
            self.addCleanup(lambda: stop_process(process))
            details = self.wait_json(marker)
            process.wait(timeout=5)
            physical.close()
            with ScienceLock(self.root) as contender:
                self.assertFalse(contender.acquire())
                stop_process(process)
                self.assertTrue(contender.acquire())
            try:
                state = (Path('/proc') / str(details['pid']) / 'stat').read_text().rsplit(')', 1)[1].split()[0]
            except FileNotFoundError:
                state = 'X'
            self.assertIn(state, ('Z', 'X'))


if __name__ == '__main__':
    unittest.main()
