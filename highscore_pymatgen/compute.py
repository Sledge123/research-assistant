"""无凭据的受限晶体计算子进程；只读取任务同目录输入。"""
import argparse
import json
import os
from pathlib import Path
import signal
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description='受限晶体工具数值计算')
    parser.add_argument('--task', required=True)
    args = parser.parse_args(argv)
    task = Path(args.task).resolve()
    root = task.parent
    from .contract import MAX_INPUT_BYTES, MAX_RUN_SECONDS, validate_parameters
    # 线程及缓存必须在导入科学依赖前固定；容器另行执行内存与CPU配额。
    for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
        os.environ[name] = '1'
    if sys.platform == 'linux':
        import ctypes
        import resource
        parent = os.getppid()
        if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() == 1 or os.getppid() != parent:
            raise RuntimeError('计算父进程死亡联动未启用。')
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (MAX_RUN_SECONDS, MAX_RUN_SECONDS))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
        signal.alarm(MAX_RUN_SECONDS)
    try:
        if task.is_symlink() or task.stat().st_size > 16384:
            raise ValueError('计算任务文件无效。')
        data = json.loads(task.read_text(encoding='utf-8'))
        parameters = validate_parameters(data['operation'], data['parameters'])
        if not isinstance(data.get('inputs'), list) or len(data['inputs']) not in (1, 2):
            raise ValueError('计算任务输入数量无效。')
        inputs = []
        for item in data['inputs']:
            name = item['path']
            if (not isinstance(name, str) or not name.startswith('input-') or any(char in name for char in ('/', '\\', ':'))
                    or name in ('.', '..') or len(name) > 120):
                raise ValueError('计算输入必须是任务同目录文件。')
            path = root / name
            if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= MAX_INPUT_BYTES:
                raise ValueError('计算输入文件无效。')
            inputs.append({'filename': item['filename'], 'content': path.read_bytes()})
        from .core import run
        result = run(inputs, data['operation'], parameters, root / 'progress.json')
        temporary = root / 'result.tmp'
        temporary.write_bytes(result)
        temporary.replace(root / 'result.zip')
        return 0
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        (root / 'error.json').write_text(json.dumps({'code': getattr(exc, 'code', 'compute'),
                                                    'message': str(exc)[:1000]}, ensure_ascii=False), encoding='utf-8')
        return 2
    except Exception:
        (root / 'error.json').write_text(json.dumps({'code': 'compute', 'message': '晶体计算未完成，请检查结构或参数。'}, ensure_ascii=False), encoding='utf-8')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
