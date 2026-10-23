"""仅 HTTPS、证书校验及禁止重定向的任务客户端。"""
import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPSHandler, HTTPRedirectHandler, Request

from .config import WorkerError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WorkerError('任务接口返回重定向，已停止以保护执行器凭据。')


class WorkerClient:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def _post(self, path, payload):
        request = Request(self.config.server_url + path,
                          json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                          {'Authorization': 'Bearer ' + self.config.token, 'Content-Type': 'application/json'},
                          method='POST')
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise WorkerError('任务接口响应超出限制。')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise WorkerError('任务接口响应格式无效。')
            return data
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise WorkerError('任务接口连接或响应失败，请检查服务和凭据；未记录远端原始内容。') from None

    def claim(self):
        return self._post('/worker/claim', {'worker_id': self.config.worker_id,
                          'database_id': self.config.database_id, 'capabilities': self.config.capabilities})

    def heartbeat(self, job_id, lease_token, message='正在执行任务'):
        return self._post(f'/worker/jobs/{job_id}/heartbeat', {'lease_token': lease_token, 'message': message})

    def complete(self, job_id, payload):
        return self._post(f'/worker/jobs/{job_id}/complete', payload)
