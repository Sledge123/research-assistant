"""Linux flock 随计算子进程继承；关闭父描述符不会解开子进程持锁。"""
import os
from pathlib import Path
import signal
import subprocess
import time


class ScienceLock:
    def __init__(self, root, path=None):
        root = Path(root).resolve()
        supplied = path or os.getenv('HIGHSCORE_SCIENCE_LOCK_PATH', 'runtime/science-lock/compute.lock')
        target = Path(supplied)
        target = target if target.is_absolute() else root / target
        if (not target.resolve().is_relative_to(root)
                or any(part.is_symlink() for part in (target, *target.parents) if part.is_relative_to(root))):
            raise ValueError('共享计算锁须位于项目目录内。')
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(target, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        self.acquired = False
        if os.name != 'posix' and os.fstat(self.fd).st_size == 0:
            os.write(self.fd, b'0')

    def acquire(self):
        if self.acquired:
            return True
        try:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        except BlockingIOError:
            return False
        except OSError as exc:
            if exc.errno in (11, 13, 35, 36):
                return False
            raise
        self.acquired = True
        return True

    def child_options(self):
        if not self.acquired:
            raise RuntimeError('计算子进程启动前必须持有共享锁。')
        return {'pass_fds': (self.fd,)} if os.name == 'posix' else {}

    def close(self):
        if self.fd is not None:
            # 不调用 LOCK_UN；继承者和父进程共享同一 open-file-description。
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _group_live(group):
    for entry in Path('/proc').iterdir():
        if entry.name.isdecimal():
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) == group and fields[0] not in ('Z', 'X'):
                    return True
            except (OSError, ValueError, IndexError):
                continue
    return False


def stop_process(process):
    """发送组信号并确认组内活进程结束后才返回。"""
    if os.name != 'posix':
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=6)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=6)
    if Path('/proc').is_dir():
        deadline = time.monotonic() + 6
        while _group_live(process.pid):
            if time.monotonic() >= deadline:
                raise RuntimeError('计算进程组尚未完全回收。')
            time.sleep(.02)
