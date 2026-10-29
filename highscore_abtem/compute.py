"""仅由受控执行器启动的单任务进程，任务不携带凭据。"""
import argparse
import json
import os
from pathlib import Path
import signal
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description='受限电镜数值计算')
    parser.add_argument('--task', required=True)
    args = parser.parse_args(argv)
    task = Path(args.task).resolve()
    root = task.parent
    data = json.loads(task.read_text(encoding='utf-8'))
    # 子进程先设置进程死亡联动和墙钟预算；主进程另外持有独立超时回收。
    if sys.platform == 'linux':
        import ctypes
        import resource
        if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise RuntimeError('无法启用计算子进程死亡联动')
        if os.getppid() == 1:
            raise RuntimeError('计算父进程已退出')
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
        signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
        signal.alarm(180)
    from .core import SimulationError, simulate
    try:
        result = simulate((root / 'input.cif').read_bytes(), data['parameters'], root / 'progress.json')
        temporary = root / 'result.tmp'
        temporary.write_bytes(result)
        temporary.replace(root / 'result.zip')
        return 0
    except (SimulationError, ValueError, KeyError, RuntimeError) as exc:
        (root / 'error.json').write_text(json.dumps({'code': getattr(exc, 'code', 'compute'),
                                                    'message': str(exc)[:1000]}, ensure_ascii=False), encoding='utf-8')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
