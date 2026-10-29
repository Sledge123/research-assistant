"""公告正文的字数上限与UTF-8/JSON转义体积兼容，保持其它路由硬限制。"""
import asyncio
import json
import unittest

from highscore_web.limits import RequestSizeLimit


class AdminBodyLimitTests(unittest.TestCase):
    def invoke(self, path, body, *, method='POST', declared=False):
        sent=[];received=[]
        async def downstream(scope, receive, send):
            message=await receive();received.append(message['body'])
            await send({'type':'http.response.start','status':204,'headers':[]})
            await send({'type':'http.response.body','body':b''})
        chunks=[body[i:i+8192] for i in range(0,len(body),8192)] or [b'']
        async def receive():
            item=chunks.pop(0)
            return {'type':'http.request','body':item,'more_body':bool(chunks)}
        async def send(message):sent.append(message)
        headers=[(b'content-length',str(len(body)).encode())] if declared else []
        asyncio.run(RequestSizeLimit(downstream)({'type':'http','method':method,'path':path,'headers':headers},receive,send))
        return next(m['status'] for m in sent if m['type']=='http.response.start'),received

    def test_legal_chinese_and_escaped_astral_announcement_survive_declared_and_chunked_limits(self):
        for content in ('中'*10000,'\U0001f52c'*10000):
            for ascii_only in (False,True):
                payload=json.dumps({'title':'科研助手','body':content,'request_key':'fixture'},ensure_ascii=ascii_only).encode()
                for declared in (False,True):
                    for path,method in (('/highscore/api/admin/announcements','POST'),('/highscore/api/admin/announcements/'+32*'a','PATCH')):
                        status,received=self.invoke(path,payload,method=method,declared=declared)
                        self.assertEqual(status,204)
                        self.assertEqual(received,[payload])

    def test_unrelated_paths_methods_and_oversize_announcements_keep_hard_bounds(self):
        for declared in (False,True):
            for path,method in (('/highscore/api/auth/register','POST'),('/highscore/api/admin/users/a/credits','POST'),
                ('/highscore/api/admin/announcements/a/other','PATCH'),('/highscore/api/admin/announcements','PUT'),
                ('/highscore/api/admin/announcements-extra','POST')):
                self.assertEqual(self.invoke(path,b'x'*20001,method=method,declared=declared),(413,[]))
            for path,method in (('/highscore/api/admin/announcements','POST'),('/highscore/api/admin/announcements/a','PATCH')):
                self.assertEqual(self.invoke(path,b'x'*(128*1024+1),method=method,declared=declared),(413,[]))
        worker='/highscore/api/worker/jobs/fixture/complete'
        status,received=self.invoke(worker,b'x'*(128*1024+1))
        self.assertEqual(status,204)
        self.assertEqual(len(received[0]),128*1024+1)


if __name__=='__main__':unittest.main()
