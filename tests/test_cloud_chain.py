"""跨包云端离线验收：真实 API、队列、数据库报告与邮件逻辑，仅替换网络传输。

所有 HSRDB、会话、归档和邮件账本均为 synthetic，留在项目 .cache/tmp/。
不得调用真实网络、数据库或 SMTP。
"""
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import zipfile

from fastapi.testclient import TestClient

from highscore_database.executor import JobHandler, Worker
from highscore_database.reader import DatabaseReader
from highscore_downloader.mail_settings import MailSettings
from highscore_downloader.mailer import MailService
from highscore_web import create_app
from highscore_worker.client import WorkerClient
from highscore_worker.config import WorkerConfig
from highscore_worker.verification import VerificationMailer
from test_database_reader import fixture


ROOT = Path(__file__).resolve().parents[1]
BASE = "/highscore/api"
PUBLIC = "https://testserver/highscore/"
WORKER_TOKEN = "synthetic-cloud-chain-worker-token-123456"


class FakeSMTP:
    """只记录 synthetic 邮件和目标邮箱，永远不建立连接。"""

    def __init__(self):
        self.messages = []
        self.login_count = 0

    def login(self, username, authorization_code):
        self.login_count += 1

    def send_message(self, message, *, from_addr, to_addrs):
        self.messages.append({"message": message, "sender": from_addr,
                              "recipients": list(to_addrs)})
        return {}

    def quit(self):
        pass

    def close(self):
        pass


class InProcessWorkerClient(WorkerClient):
    """沿用正式客户端的任务协议，仅将 HTTPS I/O 接到同进程 TestClient。"""

    def __init__(self, config, browser):
        super().__init__(config)
        self.browser = browser
        self.claimed_jobs = []
        self.heartbeats = {}
        self.completions = []

    def _post(self, path, payload):
        response = self.browser.post(
            BASE + path,
            content=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.config.token,
                     "Content-Type": "application/json"},
        )
        if response.status_code != 200:
            raise AssertionError(f"跨包执行器接口 {path} 返回 {response.status_code}: {response.text}")
        result = response.json()
        if path == "/worker/claim" and result.get("job"):
            self.claimed_jobs.append(result["job"])
        elif path.endswith("/heartbeat"):
            job_id = path.split("/")[3]
            self.heartbeats[job_id] = self.heartbeats.get(job_id, 0) + 1
        elif path.endswith("/complete"):
            if result.get("accepted") is not True:
                raise AssertionError("跨包执行器完成回执未确认接受")
            self.completions.append(path.split("/")[3])
        return result


class CloudChainTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / ".cache/tmp"
        cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="cloud-chain-", dir=cache)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.server_root, self.worker_root = self.root / "server", self.root / "worker"
        self.server_root.mkdir()
        self.worker_root.mkdir()
        self.database = self.worker_root / "synthetic.hsrdb"
        fixture(self.database)
        self.database_digest = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.database_id = DatabaseReader(self.database).database_id
        self.app = create_app(self.server_root, {"public_url": PUBLIC,
                              "worker_token": WORKER_TOKEN, "secure_cookie": True})
        self.owner = self._browser()
        self.worker_http = self._browser()
        self.smtp = FakeSMTP()
        self.settings = MailSettings(username="synthetic-sender@example.com",
                                     authorization_code="synthetic-only-not-a-secret")
        self.config = WorkerConfig(self.worker_root, PUBLIC + "api", PUBLIC,
                                   "cloud-chain-worker", self.database_id, WORKER_TOKEN)
        self.transport = InProcessWorkerClient(self.config, self.worker_http)
        handler = JobHandler(
            self.config,
            reader_factory=lambda stop: DatabaseReader(self.database, stop_event=stop),
            mail_settings_loader=lambda root: self.settings,
            mail_factory=lambda root, settings, stop_event: MailService(
                root, settings, stop_event=stop_event, smtp_factory=lambda *a, **kw: self.smtp),
            verification_factory=lambda root, settings, public_url, submission_guard: VerificationMailer(
                root, settings, public_url, submission_guard=submission_guard,
                smtp_factory=lambda *a, **kw: self.smtp),
        )
        self.worker = Worker(self.config, client=self.transport, handler=handler)
        network = patch.object(socket, "create_connection",
                               side_effect=AssertionError("离线集成测试禁止真实网络连接"))
        network.start()
        self.addCleanup(network.stop)

    def _browser(self):
        browser = TestClient(self.app, base_url="https://testserver")
        self.addCleanup(browser.close)
        return browser

    def _post(self, browser, path, body, csrf=None):
        headers = {"Origin": "https://testserver"}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return browser.post(BASE + path, headers=headers, json=body)

    def _register_verified(self, browser, email):
        password = "synthetic-cloud-password-123"
        response = self._post(browser, "/auth/register", {"email": email, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(password, response.text)
        self.assertNotIn("verify=", response.text)
        self.assertTrue(self.worker.run_once())
        # 验证令牌只在测试夹具中从已领取的验证任务读取，公众接口不可返回它。
        claimed = self.transport.claimed_jobs[-1]
        self.assertEqual(claimed["kind"], "verification_mail")
        token = parse_qs(urlparse(claimed["payload"]["verify_url"]).query)["verify"][0]
        self.assertEqual(self._post(browser, "/auth/verify", {"token": token}).status_code, 200)
        login = self._post(browser, "/auth/login", {"email": email, "password": password})
        self.assertEqual(login.status_code, 200, login.text)
        user = browser.get(BASE + "/auth/me").json()
        self.assertTrue(user["verified"])
        return user

    def _job(self, job_id, browser=None):
        response = (browser or self.owner).get(BASE + f"/jobs/{job_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_verified_registration_search_archive_delivery_and_owner_download(self):
        user = self._register_verified(self.owner, "cloud-owner@example.com")
        with self.app.state.database.connection(write=True) as db:
            db.execute("UPDATE users SET is_admin=1 WHERE id=?", (user["id"],))
        gates = self.owner.patch(BASE + "/admin/settings",
            headers={"Origin": "https://testserver", "X-CSRF-Token": user["csrf_token"]},
            json={"license_confirmed": True, "cloud_validated": True, "public_enabled": True})
        self.assertEqual(gates.status_code, 200, gates.text)

        search = self._post(self.owner, "/jobs/search", {"mode": "formula", "query": "X Y",
                            "request_key": "cloud-search"}, user["csrf_token"])
        self.assertEqual(search.status_code, 200, search.text)
        self.assertTrue(self.worker.run_once())
        found = self._job(search.json()["id"])
        self.assertEqual(found["state"], "succeeded")
        self.assertEqual(len(found["candidates"]), 2)
        candidate = found["candidates"][0]
        self.assertEqual(candidate["source"], "Synthetic 数据库")

        payload = {"search_job_id": found["id"], "candidate_ids": [candidate["id"]],
                   "send_mail": True, "request_key": "cloud-archive"}
        archive = self._post(self.owner, "/jobs/archive", payload, user["csrf_token"])
        self.assertEqual(archive.status_code, 200, archive.text)
        self.assertTrue(self.worker.run_once())
        archived = self._job(archive.json()["id"])
        self.assertEqual((archived["state"], archived["archive_status"], archived["mail_status"]),
                         ("succeeded", "success", "submitted"))
        self.assertTrue(archived["download_available"])
        self.assertEqual(archived["results"][0]["archive_type"], "database_report")
        self.assertIn("非 HighScore", archived["results"][0]["archive_label"])

        download = self.owner.get(BASE + f"/jobs/{archived['id']}/download")
        self.assertEqual(download.status_code, 200, download.text if download.status_code != 200 else "")
        with zipfile.ZipFile(io.BytesIO(download.content)) as bundle:
            self.assertEqual(len(bundle.namelist()), 9)
            self.assertTrue(all('/' not in name for name in bundle.namelist()))
            self.assertIn(candidate['code']+'-原始记录.sqlite3',bundle.namelist())
            stem = candidate['code']
            download_members = {f"{stem}/{stem}.{kind}":bundle.read(stem+'.'+kind) for kind in ('rtf','txt','json','csv')}
            card = json.loads(bundle.read(stem + ".json"))
            manifest=json.loads(bundle.read(stem+'-回执.json'))
            receipt=manifest['archive_receipt']
            self.assertEqual(card["code"], candidate["code"])
            self.assertEqual(card["database_id"], self.database_id)
            self.assertEqual(card["peak_count"], 2)
            self.assertEqual(receipt["export_method"], "sqlite_readonly_database_report")
            self.assertIsNone(card["wavelength_angstrom"])
            self.assertTrue(all(peak["two_theta_deg"] is None for peak in card["peaks"]))

        reports = [item for item in self.smtp.messages if item["message"].get_content_maintype() == "multipart"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["recipients"], [user["email"]])
        attachments = list(reports[0]["message"].iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_content_type(), "application/zip")
        with zipfile.ZipFile(io.BytesIO(attachments[0].get_payload(decode=True))) as sent_bundle:
            self.assertEqual({name: sent_bundle.read(name) for name in sent_bundle.namelist() if not name.endswith('/receipt.json')},
                             download_members)
            original=sent_bundle.read(f"{candidate['code']}/receipt.json")
            self.assertEqual(json.loads(original),receipt)
            self.assertEqual(hashlib.sha256(original).hexdigest(),manifest['archive_receipt_sha256'])

        # HTTP 重复提交必须返回同一任务；不同任务再次归档相同卡片也不能重复 SMTP。
        duplicate = self._post(self.owner, "/jobs/archive", payload, user["csrf_token"])
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(duplicate.json()["id"], archived["id"])
        self.assertFalse(self.worker.run_once())
        repeated = self._post(self.owner, "/jobs/archive", {**payload, "request_key": "cloud-archive-again"}, user["csrf_token"])
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertNotEqual(repeated.json()["id"], archived["id"])
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self._job(repeated.json()["id"])["mail_status"], "submitted")
        self.assertEqual(len(self.smtp.messages), 2)  # 一封验证邮件，一封报告邮件。

        other = self._browser()
        self._register_verified(other, "cloud-other@example.com")
        self.assertEqual(other.get(BASE + f"/jobs/{archived['id']}").status_code, 404)
        self.assertEqual(other.get(BASE + f"/jobs/{archived['id']}/download").status_code, 404)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), self.database_digest)
        self.assertTrue(self.transport.completions)
        self.assertTrue(all(self.transport.heartbeats.get(job_id, 0) >= 2
                            for job_id in self.transport.completions))
        self.assertFalse(self.worker.run_once())


if __name__ == "__main__":
    unittest.main()
