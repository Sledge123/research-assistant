"""公开发行入口回归；只使用项目内临时空库和独立静态样本。"""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from highscore_web import create_app


ROOT = Path(__file__).resolve().parents[1]


class PublicEntrypointTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'
        cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='public-entrypoint-', dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def client(self):
        return TestClient(create_app(self.root, {
            'public_url': 'http://testserver/highscore/', 'secure_cookie': False,
        }))

    def test_root_serves_built_page_and_prefixed_assets(self):
        dist = self.root / 'web/dist'
        (dist / 'assets').mkdir(parents=True)
        html = '<html lang="zh-CN"><div id="app"></div><script src="/highscore/assets/synthetic.js"></script></html>'
        (dist / 'index.html').write_text(html, encoding='utf-8')
        (dist / 'assets/synthetic.js').write_text('/* independent synthetic asset */', encoding='utf-8')
        with self.client() as client:
            response = client.get('/?verify=synthetic-token')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, html)
            self.assertEqual(response.url.path, '/')
            self.assertEqual(client.get('/highscore/assets/synthetic.js').status_code, 200)

    def test_legacy_page_redirect_preserves_all_query_parameters(self):
        with self.client() as client:
            for path in ('/highscore', '/highscore/'):
                with self.subTest(path=path):
                    response = client.get(path + '?verify=synthetic-token&tag=a&tag=b%20c', follow_redirects=False)
                    self.assertEqual(response.status_code, 308)
                    self.assertEqual(response.headers['location'], '/?verify=synthetic-token&tag=a&tag=b%20c')

    def test_root_explains_missing_frontend_build(self):
        with self.client() as client:
            response = client.get('/')
            self.assertEqual(response.status_code, 200)
            self.assertIn('网站资源尚未构建', response.text)

    def test_api_authentication_and_closed_documentation_are_preserved(self):
        with self.client() as client:
            health = client.get('/highscore/api/health')
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()['status'], 'ok')
            self.assertIs(health.json()['service_enabled'], False)
            self.assertEqual(client.get('/highscore/api/auth/me').status_code, 401)
            self.assertEqual(client.get('/openapi.json').status_code, 404)


if __name__ == '__main__':
    unittest.main()
