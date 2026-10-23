"""科研资料库权限、容量、完整性和文件交付回归。"""
import base64
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from highscore_web import create_app

BASE = "/highscore/api"
ROOT = Path(__file__).resolve().parents[1]


class ResearchLibraryTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / ".cache" / "tmp"
        cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=cache, prefix="research-library-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = create_app(self.root, {
            "public_url": "http://testserver/highscore/",
            "secure_cookie": False,
            "research_library_quota_bytes": 128,
            "research_library_min_free_bytes": 1024,
        })
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        with self.app.state.database.connection(write=True) as db:
            for identifier, email, is_admin in (("admin", "admin@example.test", 1), ("user", "user@example.test", 0)):
                db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
                           (identifier, email, "hash", 1, is_admin, time.time()))
                token = "session-" + identifier
                db.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                           (hashlib.sha256(token.encode()).hexdigest(), identifier, "csrf-" + identifier, time.time() + 3600))
        self.client.cookies.set("highscore_session", "session-admin")
        self.admin_headers = {"X-CSRF-Token": "csrf-admin"}

    def as_user(self):
        client = TestClient(self.app)
        self.addCleanup(client.close)
        client.cookies.set("highscore_session", "session-user")
        return client, {"X-CSRF-Token": "csrf-user"}

    @staticmethod
    def payload(content=b"sample research data", **overrides):
        return {
            "title": "测试资料",
            "description": "科研用途说明",
            "category": "数据",
            "license": "",
            "source_url": "",
            "filename": "sample.txt",
            "content_base64": base64.b64encode(content).decode("ascii"),
            "status": "draft",
            **overrides,
        }

    def create(self, **overrides):
        return self.client.post(BASE + "/admin/library", headers=self.admin_headers,
                                json=self.payload(**overrides))

    def test_access_is_verified_login_admin_write_and_csrf_protected(self):
        anonymous = TestClient(self.app)
        self.addCleanup(anonymous.close)
        user, user_headers = self.as_user()
        self.assertEqual(anonymous.get(BASE + "/library").status_code, 401)
        self.assertEqual(anonymous.get(BASE + "/admin/library").status_code, 401)
        self.assertEqual(user.get(BASE + "/library").status_code, 200)
        self.assertEqual(user.get(BASE + "/admin/library").status_code, 403)
        self.assertEqual(user.post(BASE + "/admin/library", headers=user_headers,
                                   json=self.payload()).status_code, 403)
        self.assertEqual(self.client.post(BASE + "/admin/library", json=self.payload()).status_code, 403)
        self.assertEqual(self.client.post(BASE + "/admin/library", headers={**self.admin_headers, "Origin": "https://evil.invalid"},
                                          json=self.payload()).status_code, 403)
        with self.app.state.database.connection(write=True) as db:
            db.execute("UPDATE users SET is_admin=0 WHERE id='admin'")
        self.assertEqual(self.client.post(BASE + "/admin/library", headers=self.admin_headers,
                                          json=self.payload()).status_code, 403)

    def test_draft_publish_download_integrity_and_delete(self):
        user, _ = self.as_user()
        contents = "晶体结构数据\n".encode("utf-8")
        response = self.create(content=contents)
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["item"]
        path = self.root / "output" / "research-library" / "files" / (item["id"] + ".bin")
        self.assertEqual(path.read_bytes(), contents)
        self.assertEqual(item["status"], "draft")
        self.assertEqual(self.client.get(BASE + "/library").json()["total"], 0)
        self.assertEqual(user.get(BASE + f"/library/{item['id']}/download").status_code, 404)
        draft_download = self.client.get(BASE + f"/admin/library/{item['id']}/download")
        self.assertEqual(draft_download.status_code, 200)
        self.assertEqual(draft_download.content, contents)
        self.assertEqual(draft_download.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn("filename*=UTF-8''sample.txt", draft_download.headers["content-disposition"])
        self.assertEqual(draft_download.headers["cache-control"], "no-store")

        endpoint = BASE + f"/admin/library/{item['id']}"
        update = {"revision": item["revision"], "title": item["title"], "description": item["description"],
                  "category": item["category"], "license": "CC BY 4.0", "source_url": "https://example.org/source",
                  "status": "published"}
        published = self.client.patch(endpoint, headers=self.admin_headers, json=update)
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()["item"]["status"], "published")
        public_page = user.get(BASE + "/library?q=测试")
        self.assertEqual(public_page.status_code, 200)
        self.assertEqual(public_page.json()["total"], 1)
        self.assertEqual(public_page.json()["categories"], ["数据"])
        public_download = user.get(BASE + f"/library/{item['id']}/download")
        self.assertEqual(public_download.status_code, 200)
        self.assertEqual(public_download.content, contents)
        self.assertEqual(public_download.headers["x-content-type-options"], "nosniff")

        stale = self.client.patch(endpoint, headers=self.admin_headers, json=update)
        self.assertEqual(stale.status_code, 409)
        admin_page = self.client.get(BASE + "/admin/library")
        self.assertEqual(admin_page.status_code, 200)
        self.assertEqual(admin_page.json()["storage"]["used_bytes"], len(contents))
        self.assertEqual(admin_page.json()["items"][0]["sha256"], hashlib.sha256(contents).hexdigest())

        removed = self.client.delete(endpoint, headers=self.admin_headers)
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertFalse(path.exists())
        self.assertEqual(self.client.get(BASE + "/admin/library").json()["storage"]["used_bytes"], 0)
        self.assertEqual(user.get(BASE + f"/library/{item['id']}/download").status_code, 404)
        with self.app.state.database.connection() as db:
            actions = [row[0] for row in db.execute("SELECT action FROM audit WHERE action LIKE 'research_library_%' ORDER BY id")]
        self.assertEqual(actions, ["research_library_create", "research_library_update", "research_library_delete"])

    def test_format_duplicate_quota_and_low_disk_guards(self):
        self.assertEqual(self.create(content=b"not a PDF", filename="paper.pdf").status_code, 422)
        self.assertEqual(self.create(content=b"bad", filename="run.exe").status_code, 422)
        self.assertEqual(list((self.root / "output" / "research-library" / "files").iterdir()), [])

        created = self.create(content=b"dup")
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(self.create(content=b"dup").status_code, 409)

        quota_root = self.root / "small-quota"
        quota_root.mkdir()
        quota_app = create_app(quota_root, {"public_url": "http://testserver/highscore/", "secure_cookie": False,
            "research_library_quota_bytes": 3, "research_library_min_free_bytes": 0})
        quota_client = TestClient(quota_app)
        self.addCleanup(quota_client.close)
        with quota_app.state.database.connection(write=True) as db:
            db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", ("admin", "admin@example.test", "hash", 1, 1, time.time()))
            token = "session-admin"
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), "admin", "csrf-admin", time.time() + 3600))
        quota_client.cookies.set("highscore_session", "session-admin")
        rejected = quota_client.post(BASE + "/admin/library", headers={"X-CSRF-Token": "csrf-admin"}, json=self.payload(content=b"four"))
        self.assertEqual(rejected.status_code, 507)

        store_module = __import__("highscore_web.research_library", fromlist=["ResearchLibraryStore"])
        with patch.object(store_module.shutil, "disk_usage", return_value=SimpleNamespace(free=1025)):
            low_space = self.create(content=b"xx")
        self.assertEqual(low_space.status_code, 507)

    def test_upload_size_and_text_encoding_are_checked(self):
        invalid_text = self.create(content=b"\xff", filename="bad.txt")
        self.assertEqual(invalid_text.status_code, 422)
        store_module = __import__("highscore_web.research_library", fromlist=["MAX_FILE_BYTES"])
        with patch.object(store_module, "MAX_FILE_BYTES", 4):
            too_large = self.create(content=b"large", filename="large.txt")
        self.assertEqual(too_large.status_code, 413)
        self.assertEqual(self.client.get(BASE + "/admin/library").json()["storage"]["used_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
