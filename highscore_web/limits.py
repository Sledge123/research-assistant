"""在 JSON 解析前按 ASGI 分块读取硬限制请求体，不依赖客户端长度声明。"""
import re
from starlette.responses import JSONResponse


class RequestSizeLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PATCH", "PUT"):
            return await self.app(scope, receive, send)
        upload = scope.get("path", "").startswith(("/highscore/api/worker/jobs/", "/highscore/api/worker/literature/jobs/"))
        path=scope.get('path','')
        abtem_submit=scope.get('method')=='POST' and path=='/highscore/api/abtem/jobs'
        abtem_complete=(scope.get('method')=='POST' and re.fullmatch(
            r'/highscore/api/worker/abtem/jobs/[0-9a-f]{32}/complete',path) is not None)
        pymatgen_submit=scope.get('method')=='POST' and path=='/highscore/api/pymatgen/jobs'
        pymatgen_complete=(scope.get('method')=='POST' and re.fullmatch(
            r'/highscore/api/worker/pymatgen/jobs/[0-9a-f]{32}/complete',path) is not None)
        announcement=(scope.get('method')=='POST' and path=='/highscore/api/admin/announcements') or (
            scope.get('method')=='PATCH' and re.fullmatch(r'/highscore/api/admin/announcements/[^/]{1,64}',path) is not None)
        library_upload=scope.get('method')=='POST' and path=='/highscore/api/admin/library'
        # 10000个Unicode字符以JSON代理对转义时可占120KB；仅放宽公告写路由。
        analytics=scope.get('method')=='POST' and path=='/highscore/api/analytics/pageview'
        limit = (1024 if analytics else 17 * 1024 * 1024 if abtem_complete or pymatgen_complete else
                 1400 * 1024 if pymatgen_submit else 720 * 1024 if abtem_submit else
                 29 * 1024 * 1024 if library_upload else 56 * 1024 * 1024 if upload else
                 128*1024 if announcement else 20_000)
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                length = int(declared)
                if length < 0:
                    raise ValueError
            except ValueError:
                return await JSONResponse({"detail": "请求长度无效。"}, status_code=400)(scope, receive, send)
            if length > limit:
                return await JSONResponse({"detail": "请求过大。"}, status_code=413)(scope, receive, send)
        size, chunks = 0, []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                # 立即返回，不继续消费攻击者可能无限提供的后续分块。
                return await JSONResponse({"detail": "请求过大。"}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body, replayed = b"".join(chunks), False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
