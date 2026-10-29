"""独立晶体工具执行角色：租约、每任务子进程、取消回收和完成回执重试。"""
import base64
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid

from .artifacts import MAX_BUNDLE_BYTES, validate_bundle
from .contract import MAX_RUN_SECONDS, RETENTION_SECONDS, MAX_INPUT_BYTES, validate_inputs, validate_input_bytes, validate_parameters
from highscore_science.lock import ScienceLock, stop_process

ERROR_CODES = {'unsupported_occupancy', 'unsupported_disorder', 'unsupported_structure',
               'unsupported_scale', 'unsupported_precision', 'resource', 'compute'}


class ApiError(Exception):
    def __init__(self, status):
        self.status = status


class Client:
    def __init__(self, url, token):
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('晶体工具执行器服务地址无效。')
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError('晶体工具执行器缺少独立凭据。')
        self.url, self.token = url.rstrip('/'), token

    def post(self, route, payload):
        content = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        request = Request(self.url + route, data=content,
                          headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'}, method='POST')
        try:
            with urlopen(request, timeout=15) as response:
                data = response.read(3 * 1024 * 1024 + 1)
                if len(data) > 3 * 1024 * 1024:
                    raise ApiError(502)
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ApiError(502)
                return result
        except HTTPError as exc:
            raise ApiError(exc.code) from None
        except (URLError, OSError, ValueError):
            raise ApiError(0) from None


def event(name, **values):
    print(json.dumps({'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      'event': name, **values}, ensure_ascii=False), flush=True)


def _private(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush(); os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_file(path, content):
    _private(path, json.dumps(content, ensure_ascii=False, allow_nan=False).encode('utf-8'))


def available(root):
    try:
        values = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        memory = int(values['MemAvailable'].split()[0]) * 1024
    except (OSError, KeyError, ValueError):
        return False
    return memory >= 768 * 1024 * 1024 and shutil.disk_usage(root).free >= 1024 * 1024 * 1024


def _critical_memory():
    try:
        return int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                        if line.startswith('MemAvailable:'))) < 256 * 1024
    except (OSError, StopIteration, ValueError):
        return True


def _child_environment(root, cache):
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT') if key in os.environ}
    environment.update({'PYTHONPATH': str(root), 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                        'PYTHONUNBUFFERED': '1', 'HOME': str(cache), 'XDG_CACHE_HOME': str(cache),
                        'TMPDIR': str(cache / 'tmp'), 'TMP': str(cache / 'tmp'), 'TEMP': str(cache / 'tmp'),
                        'MPLCONFIGDIR': str(cache / 'matplotlib'), 'NUMBA_CACHE_DIR': str(cache / 'numba'),
                        'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
                        'NUMBA_NUM_THREADS': '1', 'NUMEXPR_NUM_THREADS': '1'})
    return environment


def _outcome(directory, state, error_code=None, message=''):
    return {'state': state, **({'error_code': error_code} if error_code else {}), 'message': message}


def execute(client, claim, root, work, cache, stopping, *, budget=MAX_RUN_SECONDS, command=None):
    with ScienceLock(root) as physical:
        return _execute(client, claim, root, work, cache, stopping, budget=budget, command=command, physical=physical)


def _execute(client, claim, root, work, cache, stopping, *, budget, command, physical):
    job, lease = claim['job'], claim['lease_token']
    identifier = job['id']
    if not re.fullmatch(r'[0-9a-f]{32}', identifier) or not isinstance(lease, str) or len(lease) < 32:
        raise ValueError('任务或租约标识无效。')
    operation = job['operation']
    parameters = validate_parameters(operation, job['parameters'])
    inputs = []
    import hashlib
    for item in job['inputs']:
        encoded = item['content_base64']
        if len(encoded) > (MAX_INPUT_BYTES + 2) // 3 * 4:
            raise ValueError('任务输入过大。')
        content = base64.b64decode(encoded, validate=True)
        validate_input_bytes(content, item['filename'])
        if hashlib.sha256(content).hexdigest() != item['sha256']:
            raise ValueError('任务输入摘要不一致。')
        inputs.append({'filename': item['filename'], 'content': content})
    validate_inputs(inputs, operation)
    directory = work / identifier / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    if not directory.resolve().is_relative_to(work.resolve()) or directory.is_symlink():
        raise ValueError('计算目录越出执行器工作区。')
    tasks = []
    for index, item in enumerate(inputs):
        suffix = '.cif' if validate_input_bytes(item['content'], item['filename']) == 'cif' else '.poscar'
        name = 'input-' + chr(97 + index) + suffix
        _private(directory / name, item['content'])
        tasks.append({'filename': item['filename'], 'path': name})
    _json_file(directory / 'task.json', {'operation': operation, 'parameters': parameters, 'inputs': tasks})
    initial = client.post('/jobs/' + identifier + '/heartbeat', {'lease_token': lease, 'progress': 1, 'phase': 'preparing'})
    if initial.get('accepted') is not True:
        raise ApiError(502)
    if initial.get('cancel_requested'):
        return directory, _outcome(directory, 'cancelled')
    wait_started, wait_heartbeat = time.monotonic(), 0.0
    while not physical.acquire():
        if stopping.is_set():
            return directory, _outcome(directory, 'failed', 'resource', '执行角色停止。')
        if time.monotonic() - wait_started > 150:
            return directory, _outcome(directory, 'timed_out', 'resource', '等待共享计算名额超时。')
        if time.monotonic() >= wait_heartbeat:
            heartbeat = client.post('/jobs/' + identifier + '/heartbeat',
                {'lease_token': lease, 'progress': 1, 'phase': 'preparing'})
            if heartbeat.get('accepted') is not True:
                raise ApiError(502)
            if heartbeat.get('cancel_requested'):
                return directory, _outcome(directory, 'cancelled')
            wait_heartbeat = time.monotonic() + 8
        stopping.wait(.2)
    start, next_heartbeat = time.monotonic(), 0.0
    outcome = None
    command = command or [sys.executable, '-B', '-m', 'highscore_pymatgen.compute', '--task', str(directory / 'task.json')]
    descriptor = os.open(directory / 'compute.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as log:
        process = subprocess.Popen(command, cwd=root, env=_child_environment(root, cache),
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=(os.name == 'posix'), **physical.child_options())
        try:
            while process.poll() is None:
                if stopping.is_set():
                    outcome = _outcome(directory, 'failed', 'compute', '执行角色停止，计算已回收。'); break
                if time.monotonic() - start > budget:
                    outcome = _outcome(directory, 'timed_out', 'resource', '计算已达到单任务时长预算。'); break
                if _critical_memory():
                    outcome = _outcome(directory, 'failed', 'resource', '宿主机可用内存降至保留阈值。'); break
                if time.monotonic() >= next_heartbeat:
                    progress, phase = 10, 'preparing'
                    try:
                        data = json.loads((directory / 'progress.json').read_text(encoding='utf-8'))
                        if type(data.get('progress')) is int and 0 <= data['progress'] < 100:
                            progress = data['progress']
                        if data.get('phase') in ('preparing', 'calculating', 'packaging', 'verifying'):
                            phase = data['phase']
                    except (OSError, ValueError):
                        pass
                    heartbeat = client.post('/jobs/' + identifier + '/heartbeat',
                                            {'lease_token': lease, 'progress': progress, 'phase': phase})
                    if heartbeat.get('accepted') is not True:
                        raise ApiError(502)
                    if heartbeat.get('cancel_requested'):
                        outcome = _outcome(directory, 'cancelled'); break
                    next_heartbeat = time.monotonic() + 8
                stopping.wait(0.2)
        except ApiError:
            stop_process(process)
            _json_file(directory / 'outcome.json', {'state': 'lease_lost', 'finished_at': time.time()})
            raise
        finally:
            stop_process(process)
    if outcome is None:
        if process.returncode == 0:
            path = directory / 'result.zip'
            if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_BUNDLE_BYTES:
                outcome = _outcome(directory, 'failed', 'compute', '计算未产生受支持的完整结果。')
            else:
                data = path.read_bytes()
                try:
                    validate_bundle(data, inputs, operation, parameters)
                except (ValueError, OSError):
                    outcome = _outcome(directory, 'failed', 'compute', '结果未通过执行器完整复验。')
                else:
                    outcome = {'state': 'succeeded', 'bundle_base64': base64.b64encode(data).decode('ascii')}
        elif process.returncode in (124, -signal.SIGALRM):
            outcome = _outcome(directory, 'timed_out', 'resource')
        else:
            code, message = ('resource' if process.returncode in (-signal.SIGKILL, 137) else 'compute'), ''
            try:
                error = json.loads((directory / 'error.json').read_text(encoding='utf-8'))
                if error.get('code') in ERROR_CODES:
                    code = error['code']
                message = str(error.get('message', ''))[:1000]
            except (OSError, ValueError):
                pass
            outcome = _outcome(directory, 'failed', code, message)
    _json_file(directory / 'outcome.json', {'state': outcome['state'], 'finished_at': time.time(),
                                           'wall_seconds': time.monotonic() - start})
    return directory, outcome


def deliver(client, directory, payload):
    try:
        reply = client.post('/jobs/' + payload['job_id'] + '/complete', payload['complete'])
        if reply.get('accepted') is not True:
            raise ApiError(502)
    except ApiError as exc:
        if exc.status not in (404, 409, 410, 413, 422):
            raise
        try:
            heartbeat = client.post('/jobs/' + payload['job_id'] + '/heartbeat',
                                    {'lease_token': payload['complete']['lease_token'], 'progress': 99, 'phase': 'verifying'})
        except ApiError as lost:
            if lost.status not in (404, 409, 410):
                raise
            reply = {'accepted': False, 'disposition': 'lease_expired_or_already_closed'}
        else:
            if heartbeat.get('accepted') is not True:
                raise ApiError(502)
            if heartbeat.get('cancel_requested'):
                cancellation = {'lease_token': payload['complete']['lease_token'], 'state': 'cancelled'}
                reply = client.post('/jobs/' + payload['job_id'] + '/complete', cancellation)
                if reply.get('accepted') is not True:
                    raise ApiError(502)
            else:
                # 活租约下成功包被拒绝：明确结束失败，避免自动重算或伪装成功。
                failure = {'lease_token': payload['complete']['lease_token'], 'state': 'failed', 'error_code': 'compute',
                           'message': '结果提交被服务器拒绝，已保留本地回执。'}
                reply = client.post('/jobs/' + payload['job_id'] + '/complete', failure)
                if reply.get('accepted') is not True:
                    raise ApiError(502)
    _json_file(directory / 'delivery.json', {'at': time.time(), **reply})
    (directory / 'pending.json').unlink(missing_ok=True)


def cleanup(work):
    cutoff = time.time() - RETENTION_SECONDS
    for job in work.iterdir():
        if not job.is_dir() or job.is_symlink() or not re.fullmatch(r'[0-9a-f]{32}', job.name):
            continue
        for directory in job.iterdir():
            if (not directory.is_dir() or directory.is_symlink() or not re.fullmatch(r'[0-9a-f]{32}', directory.name)
                    or not directory.resolve().is_relative_to(work.resolve()) or (directory / 'pending.json').exists()):
                continue
            marker = directory / 'outcome.json'
            timestamp = marker.stat().st_mtime if marker.is_file() else directory.stat().st_mtime
            if timestamp < cutoff:
                shutil.rmtree(directory)


def run(root, *, once=False):
    root = Path(root).resolve()
    work, cache = root / 'output/pymatgen-worker', root / '.cache/pymatgen'
    for path in (work, cache, cache / 'tmp'):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (not path.resolve().is_relative_to(root)
                or any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(root))):
            raise ValueError('执行器目录须在项目内。')
        os.chmod(path, 0o700)
    client = Client(os.getenv('HIGHSCORE_PYMATGEN_SERVER_URL', 'http://web:8787/highscore/api/worker/pymatgen'),
                    os.getenv('HIGHSCORE_PYMATGEN_WORKER_TOKEN', ''))
    worker_id = os.getenv('HIGHSCORE_PYMATGEN_WORKER_ID', 'linux-pymatgen-1')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', worker_id):
        raise ValueError('晶体工具执行器标识无效。')
    stopping = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stopping.set())
    while not stopping.is_set():
        try:
            cleanup(work)
            pending = sorted(work.glob('*/*/pending.json'))
            for path in pending:
                if path.is_symlink() or not path.resolve().is_relative_to(work):
                    raise ValueError('完成回执目录无效。')
                deliver(client, path.parent, json.loads(path.read_text(encoding='utf-8')))
            claim = client.post('/claim', {'worker_id': worker_id, 'available': available(work)})
            if claim.get('job') is not None:
                identifier = claim['job']['id']
                event('calculation_started', job_id=identifier)
                directory, result = execute(client, claim, root, work, cache, stopping)
                payload = {'job_id': identifier, 'complete': {'lease_token': claim['lease_token'], **result}}
                _json_file(directory / 'pending.json', payload)
                deliver(client, directory, payload)
                event('calculation_finished', job_id=identifier, state=result['state'])
        except ApiError as exc:
            event('server_request_deferred', status=exc.status)
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
            event('worker_attempt_failed', error_type=type(exc).__name__)
        if once:
            return 0
        stopping.wait(5)
    return 0
