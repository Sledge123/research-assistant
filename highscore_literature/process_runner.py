"""每轮引擎独立 spawn；控制管道只传固定消息，大结果使用项目内私有文件。"""
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time

from .engine import LeaseLost, SliceExpired, StateStore
from .pdf import MAX_PDF_BYTES


CLEANUP_SECONDS = 15.0
MAX_RESULT_BYTES = ((MAX_PDF_BYTES + 2) // 3) * 4 + 2 * 1024 * 1024
WAITING = {'state': 'waiting', 'message': '本轮处理已停止，已保留原任务及发布核验记录，将稍后继续核验。'}


class _RemoteGuard:
    """子进程不持有服务器客户端或租约令牌；写前核验必须由父进程批准。"""
    def __init__(self, channel, until):
        self.channel, self.until = channel, until

    def check(self, force=False, allow_expired=False):
        if time.monotonic() >= self.until:
            raise SliceExpired()
        try:
            self.channel.send_bytes(b'force' if force else b'check')
            while True:
                remaining = self.until - time.monotonic()
                if remaining <= 0:
                    raise SliceExpired()
                if self.channel.poll(min(.05, remaining)):
                    response = self.channel.recv_bytes(32)
                    if response == b'ok':
                        return
                    if response == b'expired':
                        raise SliceExpired()
                    raise LeaseLost()
        except (EOFError, OSError):
            raise LeaseLost() from None

    __call__ = check

    def remaining_seconds(self):
        self.check()
        return max(0.0, self.until - time.monotonic())

    def set_stage(self, stage):
        if stage not in {'fetching', 'awaiting_fulltext', 'renewing', 'verifying', 'retrying'}:
            raise LeaseLost()
        if time.monotonic() >= self.until:
            raise SliceExpired()
        try:
            self.channel.send_bytes(('stage:' + stage).encode('ascii'))
            while time.monotonic() < self.until:
                if self.channel.poll(min(.05, max(0, self.until-time.monotonic()))):
                    if self.channel.recv_bytes(32) == b'ok':
                        return
                    raise LeaseLost()
            raise SliceExpired()
        except (EOFError, OSError):
            raise LeaseLost() from None


def _quiet_child():
    # 浏览器及第三方库的输出可能包含秘密；子进程始终不继承日志输出通道。
    descriptor = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
    finally:
        if descriptor not in (1, 2):
            os.close(descriptor)


def _engine_child(engine, job, accounts, until, channel, relative):
    _quiet_child()
    try:
        if os.name != 'nt':
            os.setsid()
        channel.send_bytes(b'ready')
        # 父进程先绑定本轮进程组/Windows Job，再批准创建浏览器。
        if channel.recv_bytes(32) != b'start':
            return
        guard = _RemoteGuard(channel, until)
        guard()
        engine.accounts = accounts
        values = engine(job, guard)
        data = json.dumps(values, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(data) > MAX_RESULT_BYTES:
            raise ValueError()
        store = StateStore(engine.root)
        store._atomic(store.path(relative) / 'result.json', data)
        channel.send_bytes(b'done')
    except LeaseLost:
        try:
            channel.send_bytes(b'lost')
        except (OSError, EOFError):
            pass
    except BaseException:
        # 不序列化异常或 traceback；未知发布状态已经由 StateStore 保留。
        try:
            channel.send_bytes(b'failed')
        except (OSError, EOFError):
            pass
    finally:
        channel.close()


def _linux_process(pid):
    """读取不可仅凭 PID 复用的身份；stat 的 comm 允许包含空格及括号。"""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8').rsplit(')', 1)[1].split()
        return (int(pid), int(fields[19])), int(fields[1]), fields[0]
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def _linux_descendants(root_pid):
    snapshot = {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            value = _linux_process(entry.name)
            if value is not None:
                snapshot[value[0][0]] = value
    owned = {root_pid}
    result = {}
    while True:
        previous = len(owned)
        for identity, parent, state in snapshot.values():
            if parent in owned and identity[0] != root_pid:
                owned.add(identity[0])
                result[identity] = state
        if len(owned) == previous:
            return result


def _linux_owned(identity):
    current = _linux_process(identity[0])
    if current is None or current[0] != identity:
        return False
    visited = set()
    while current[1] != os.getpid():
        if current[1] <= 1 or current[1] in visited:
            return False
        visited.add(current[1])
        parent = _linux_process(current[1])
        if parent is None or parent[0][1] > current[0][1]:
            return False
        current = parent
    return True


def _linux_signal(identity, value):
    descriptor = None
    try:
        # pidfd 将信号绑定到已打开的进程，避免读取 starttime 后 PID 再次复用。
        if hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'):
            try:
                descriptor = os.pidfd_open(identity[0])
            except OSError:
                pass
        current = _linux_process(identity[0])
        if current is None or current[0] != identity or not _linux_owned(identity):
            return
        if descriptor is not None:
            signal.pidfd_send_signal(descriptor, value)
        else:
            os.kill(identity[0], value)
    except ProcessLookupError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _linux_reap():
    while True:
        try:
            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                return
        except ChildProcessError:
            return
        except InterruptedError:
            continue


def _linux_cleanup(until):
    """本轮 supervisor 是稳定收养边界，包含另建 session 的浏览器后代。"""
    empty = 0
    while time.monotonic() < until:
        _linux_reap()
        targets = _linux_descendants(os.getpid())
        if not targets:
            empty += 1
            if empty >= 2:
                return True
            time.sleep(.005)
            continue
        empty = 0
        # 先冻结已知谱系，阻止清理过程中继续 fork，再精确终止并收养/回收。
        for identity, state in targets.items():
            if state != 'Z':
                _linux_signal(identity, signal.SIGSTOP)
        for identity in targets:
            _linux_signal(identity, signal.SIGKILL)
        _linux_reap()
        time.sleep(.005)
    _linux_reap()
    return not _linux_descendants(os.getpid())


def _linux_supervisor(engine, job, accounts, until, channel, relative, lifecycle):
    """独立每轮 subreaper；根调度器及其无关子进程不改变收养关系。"""
    _quiet_child()
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        import ctypes
        api = ctypes.CDLL(None, use_errno=True)
        api.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        api.prctl.restype = ctypes.c_int
        os.setsid()
        if api.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError()
        if api.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise OSError()
        child = os.fork()
        if child == 0:
            lifecycle.close()
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            _engine_child(engine, job, accounts, until, channel, relative)
            os._exit(0)
        channel.close()
        while not stopping and time.monotonic() < until:
            if lifecycle.poll(min(.05, max(0.0, until-time.monotonic()))):
                lifecycle.recv_bytes(32)
                break
            try:
                if os.waitpid(child, os.WNOHANG)[0] != 0:
                    break
            except ChildProcessError:
                break
    except BaseException:
        try:
            channel.send_bytes(b'failed')
        except (OSError, EOFError):
            pass
    finally:
        try:
            cleaned = _linux_cleanup(min(time.monotonic() + CLEANUP_SECONDS - 1, until + CLEANUP_SECONDS - 1))
            lifecycle.send_bytes(b'clean' if cleaned else b'unclean')
        except BaseException:
            pass
        channel.close()
        lifecycle.close()


class _WindowsJob:
    """将本轮子进程及其后代绑定到独立的 kill-on-close Job。"""
    def __init__(self, pid):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', wintypes.DWORD), ('minimum', ctypes.c_size_t),
                        ('maximum', ctypes.c_size_t), ('active_limit', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('read_count', 'write_count', 'other_count', 'read_bytes', 'write_bytes', 'other_bytes')]

        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', Counters), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]

        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        process = None
        try:
            if not self.handle:
                raise OSError()
            limits = Extended()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError()
            process = self.api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not self.api.AssignProcessToJobObject(self.handle, process):
                raise OSError()
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                self.api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.TerminateJobObject(self.handle, 1)
            self.api.CloseHandle(self.handle)
            self.handle = None


def _stop_process(process, group_owned, windows_job, until, lifecycle=None):
    if process.pid is None:
        return
    if lifecycle is not None:
        cleaned = False
        try:
            lifecycle.send_bytes(b'stop')
        except (OSError, EOFError):
            pass
        while time.monotonic() < until:
            if lifecycle.poll(min(.05, max(0.0, until-time.monotonic()))):
                try:
                    cleaned = lifecycle.recv_bytes(32) == b'clean'
                except (OSError, EOFError):
                    pass
                break
            if not process.is_alive():
                break
        process.join(timeout=max(0.0, until-time.monotonic()))
        if process.is_alive():
            process.kill()
            process.join(timeout=max(0.0, until-time.monotonic()))
        # 首次空轮询与 is_alive 之间可能刚好完成 send/exit；退出后再读已缓冲回执。
        # 协议只发一条清理回执；固定上限与 poll(0) 保证不延长清理等待。
        for _ in range(4):
            try:
                if not lifecycle.poll(0):
                    break
                cleaned = lifecycle.recv_bytes(32) == b'clean'
                if not cleaned:
                    break
            except (OSError, EOFError):
                break
        if process.is_alive() or not cleaned:
            raise LeaseLost()
        process.close()
        return
    if windows_job is not None:
        windows_job.close()
    elif os.name != 'nt' and group_owned:
        # 只使用 ready 握手确认的本轮 session；绝不按名称或父进程组杀进程。
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process.join(timeout=min(.15, max(0.0, until - time.monotonic())))
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.is_alive():
        process.terminate()
    process.join(timeout=min(.3, max(0.0, until - time.monotonic())))
    if process.is_alive():
        process.kill()
        process.join(timeout=max(0.0, until - time.monotonic()))
    if process.is_alive():
        raise LeaseLost()
    process.close()


def run_engine_process(engine, job, accounts, guard):
    """父进程保留互斥锁和租约；子进程不含 API 客户端，结果需在清理后回传。"""
    store = StateStore(engine.root)
    base = store.path('output/literature-worker/processes')
    store._directory(base)
    runtime = Path(tempfile.mkdtemp(prefix='slice-', dir=base))
    os.chmod(runtime, 0o700)
    relative = runtime.relative_to(store.root).as_posix()
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe(duplex=True)
    lifecycle = child_lifecycle = None
    arguments = (engine, job, accounts, guard.until, child, relative)
    target = _engine_child
    if sys.platform == 'linux':
        lifecycle, child_lifecycle = context.Pipe(duplex=True)
        arguments += (child_lifecycle,)
        target = _linux_supervisor
    process = context.Process(target=target, args=arguments, name='literature-slice')
    windows_job = None
    group_owned = False
    completed = False
    try:
        guard.check()
        process.start()
        child.close()
        if child_lifecycle is not None:
            child_lifecycle.close()
        if os.name == 'nt':
            windows_job = _WindowsJob(process.pid)
        while True:
            guard.check()
            if parent.poll(min(.05, guard.remaining_seconds())):
                message = parent.recv_bytes(32)
                if message == b'ready':
                    group_owned = os.name != 'nt'
                    guard.check()
                    parent.send_bytes(b'start')
                elif message in (b'check', b'force'):
                    guard.check(force=message == b'force')
                    parent.send_bytes(b'ok')
                elif message.startswith(b'stage:'):
                    guard.set_stage(message[6:].decode('ascii'))
                    parent.send_bytes(b'ok')
                elif message == b'done':
                    completed = True
                    break
                elif message == b'lost':
                    raise LeaseLost()
                else:
                    break
            elif not process.is_alive():
                break
    except LeaseLost:
        raise
    except (SliceExpired, EOFError, OSError, ValueError, TypeError):
        completed = False
    except Exception:
        completed = False
    finally:
        cleanup_until = min(time.monotonic() + CLEANUP_SECONDS, guard.final_until)
        try:
            _stop_process(process, group_owned, windows_job, cleanup_until, lifecycle)
            if completed:
                try:
                    target = store.path(f'{relative}/result.json')
                    if target.is_file() and target.stat().st_size <= MAX_RESULT_BYTES:
                        values = json.loads(target.read_text(encoding='utf-8'))
                        if isinstance(values, dict) and values.get('state') in {
                                'waiting', 'needs_selection', 'succeeded', 'failed', 'needs_attention'}:
                            return_value = values
                        else:
                            return_value = dict(WAITING)
                    else:
                        return_value = dict(WAITING)
                except (OSError, ValueError, UnicodeError):
                    return_value = dict(WAITING)
            else:
                return_value = dict(WAITING)
        finally:
            parent.close()
            child.close()
            if lifecycle is not None:
                lifecycle.close()
                child_lifecycle.close()
            # 只删除本轮随机私有目录；账号状态及 publish_intent 不在此目录。
            shutil.rmtree(store.path(relative))
    return return_value
