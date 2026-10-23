"""网站离线契约与安全回归；全部样例为 synthetic，不发送邮件。"""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASE = "/highscore/api"


class WebApiTests(unittest.TestCase):
    def setUp(self):
        try:
            from highscore_web import create_app
        except ImportError:
            self.fail("尚未实现可移植网站后端 create_app")
        from fastapi.testclient import TestClient
        cache = ROOT / ".cache/tmp"
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache, prefix="web-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = create_app(self.root, {"public_url": "http://testserver/highscore/",
                                         "worker_token": "synthetic-test-worker-secret-123456",
                                         "database_admin_only": False,
                                         "secure_cookie": False})
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.worker = {"Authorization": "Bearer synthetic-test-worker-secret-123456"}

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "output/web/state.sqlite3")
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, capabilities=None):
        return self.client.post(BASE + "/worker/claim", headers=self.worker,
                                json={"worker_id": "synthetic-worker", "database_id": "synthetic-db",
                                      "capabilities": capabilities or ["search", "archive", "mail_retry", "verification_mail"]})

    def complete(self, claim, state="succeeded", result=None, **kwargs):
        return self.client.post(BASE + f"/worker/jobs/{claim['job']['id']}/complete", headers=self.worker,
                                json={"lease_token": claim["lease_token"], "state": state,
                                      "result": result or {}, **kwargs})

    def register(self, email="synthetic@example.com", password="synthetic-password-123"):
        response = self.client.post(BASE + "/auth/register", json={"email": email, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        claim = self.claim(["verification_mail"]).json()
        self.assertEqual(claim["job"]["kind"], "verification_mail")
        token = parse_qs(urlparse(claim["job"]["payload"]["verify_url"]).query)["verify"][0]
        self.complete(claim, result={"mail_status": "submitted"})
        response = self.client.post(BASE + "/auth/verify", json={"token": token})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(BASE + "/auth/login", json={"email": email, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return self.client.get(BASE + "/auth/me").json()

    def enable(self, user):
        with self.db() as db:
            db.execute("UPDATE users SET is_admin=1 WHERE id=?", (user["id"],))
        response = self.client.patch(BASE + "/admin/settings", headers={"X-CSRF-Token": user["csrf_token"]},
                                     json={"license_confirmed": True, "cloud_validated": True,
                                           "public_enabled": True})
        self.assertEqual(response.status_code, 200, response.text)

    def search(self, user, key="synthetic-search", query="Synthetic"):
        return self.client.post(BASE + "/jobs/search", headers={"X-CSRF-Token": user["csrf_token"]},
                                json={"mode": "compound_name", "query": query, "request_key": key})

    def test_registration_is_generic_no_token_and_unverified_cannot_login(self):
        body = {"email": "synthetic@example.com", "password": "synthetic-password-123"}
        first = self.client.post(BASE + "/auth/register", json=body)
        second = self.client.post(BASE + "/auth/register", json=body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertNotIn("token", first.text)
        self.assertEqual(self.client.post(BASE + "/auth/login", json=body).status_code, 401)
        with self.db() as db:
            row = db.execute("SELECT * FROM users").fetchone()
            self.assertNotIn(body["password"], row["password_hash"])
            self.assertFalse(row["is_admin"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_verification_session_csrf_origin_and_worker_auth(self):
        user = self.register()
        self.assertTrue(user["verified"])
        self.assertFalse(user["is_admin"])
        self.assertEqual(self.client.get(BASE + "/jobs").json(), {"items": []})
        self.assertEqual(self.client.post(BASE + "/jobs/search", json={"mode": "formula", "query": "X2Y", "request_key": "a"}).status_code, 403)
        self.assertEqual(self.client.post(BASE + "/auth/logout", headers={"X-CSRF-Token": user["csrf_token"], "Origin": "https://evil.invalid"}).status_code, 403)
        self.assertEqual(self.client.post(BASE + "/auth/login", headers={"Origin": "https://evil.invalid"}, json={"email": "synthetic@example.com", "password": "synthetic-password-123"}).status_code, 403)
        self.assertEqual(self.client.post(BASE + "/worker/claim", json={"worker_id": "x", "database_id": "x", "capabilities": []}).status_code, 401)
        self.assertEqual(self.client.get(BASE + "/admin/status").status_code, 403)

    def test_gates_default_closed_and_durable_jobs_idempotency_quota(self):
        user = self.register()
        health = self.client.get(BASE + "/health").json()
        self.assertFalse(health["service_enabled"])
        self.assertFalse(health["worker_online"], "只有邮箱执行器在线不能报告数据库能力在线")
        self.assertEqual(self.search(user).status_code, 503)
        self.enable(user)
        self.assertFalse(self.client.get(BASE + "/admin/status").json()["worker_online"])
        first = self.search(user)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.search(user).json()["id"], first.json()["id"])
        self.assertEqual(self.search(user, query="Changed").status_code, 409)
        self.assertEqual(self.search(user, key="another").status_code, 409)
        self.assertEqual(self.client.get(BASE + "/auth/me").json()["quotas"]["search_used"], 1)
        from highscore_web import create_app
        from fastapi.testclient import TestClient
        restarted = create_app(self.root, {"worker_token": "synthetic-test-worker-secret-123456", "secure_cookie": False,
                                           "public_url": "http://testserver/highscore/"})
        with TestClient(restarted) as client:
            client.cookies.update(self.client.cookies)
            self.assertEqual(client.get(BASE + "/jobs").json()["items"][0]["id"], first.json()["id"])

    def test_candidates_owner_database_and_single_desktop_lease(self):
        user = self.register()
        self.enable(user)
        job = self.search(user).json()
        claim = self.claim().json()
        self.assertEqual(claim["job"]["id"], job["id"])
        self.assertIsNone(self.claim().json()["job"])
        self.assertEqual(self.complete(claim, result={"candidates": [{"code": "09-999-0001", "phase_name": "synthetic", "chemical_formula": "X2Y", "source": "synthetic"}]}).status_code, 200)
        candidate = self.client.get(BASE + f"/jobs/{job['id']}").json()["candidates"][0]
        another = self.register("another@example.com")
        self.assertEqual(self.client.get(BASE + f"/jobs/{job['id']}").status_code, 404)
        forged = self.client.post(BASE + "/jobs/archive", headers={"X-CSRF-Token": another["csrf_token"]}, json={"search_job_id": job["id"], "candidate_ids": [candidate["id"]], "send_mail": False, "request_key": "forged"})
        self.assertEqual(forged.status_code, 404)

    def test_expired_lease_never_auto_replays_possible_email(self):
        user = self.register()
        self.enable(user)
        job = self.client.post(BASE + "/jobs/archive", headers={"X-CSRF-Token": user["csrf_token"]}, json={"codes": "09-999-0001", "send_mail": True, "request_key": "archive"}).json()
        claim = self.claim().json()
        with self.db() as db:
            db.execute("UPDATE jobs SET lease_expires=0 WHERE id=?", (job["id"],))
        self.assertIsNone(self.claim().json()["job"])
        current = self.client.get(BASE + f"/jobs/{job['id']}").json()
        self.assertEqual(current["state"], "needs_attention")
        self.assertEqual(current["mail_status"], "unknown")
        self.assertEqual(self.complete(claim).status_code, 409)
        self.assertEqual(self.client.post(BASE + f"/jobs/{job['id']}/retry", headers={"X-CSRF-Token": user["csrf_token"]}, json={"request_key": "retry"}).status_code, 409)

    def archive_zip(self, database_report=False, codes=None):
        from highscore_downloader.archive import ArchiveStore
        from test_archive import synthetic_card
        root = self.root / "synthetic-artifact"
        root.mkdir(exist_ok=True)
        rtf = root / "synthetic.rtf"
        rtf.write_bytes(b"{\\rtf1 synthetic}")
        for code in codes or ["09-999-0001"]:
            card = synthetic_card()
            card["code"] = code
            if database_report:
                card["verification"] = {"method": "sqlite_readonly_database_report", "report_version": 2}
            ArchiveStore(root).save(card, rtf, code)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in (root / "output/cards").rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        return data.getvalue()

    def test_archive_is_revalidated_download_and_unknown_mail_needs_confirmation(self):
        user = self.register()
        self.enable(user)
        job = self.client.post(BASE + "/jobs/archive", headers={"X-CSRF-Token": user["csrf_token"]}, json={"codes": "09-999-0001", "send_mail": True, "request_key": "archive"}).json()
        claim = self.claim().json()
        result = {"archive_status": "success", "mail_status": "unknown", "results": [{"code": "09-999-0001", "source": "synthetic 测试来源"}]}
        self.assertEqual(self.complete(claim, result=result, artifact_zip_base64=base64.b64encode(self.archive_zip()).decode()).status_code, 200)
        current = self.client.get(BASE + f"/jobs/{job['id']}").json()
        self.assertTrue(current["download_available"])
        self.assertEqual(self.client.get(BASE + f"/jobs/{job['id']}/download").status_code, 200)
        url = BASE + f"/jobs/{job['id']}/mail-retry"
        headers = {"X-CSRF-Token": user["csrf_token"]}
        self.assertEqual(self.client.post(url, headers=headers, json={"request_key": "resend"}).status_code, 409)
        self.assertEqual(self.client.post(url, headers=headers, json={"request_key": "resend", "confirm_unknown": True}).status_code, 200)
        self.assertEqual(self.client.post(url, headers=headers, json={"request_key": "resend", "confirm_unknown": True}).status_code, 200)
        self.assertEqual(self.client.post(url, headers=headers, json={"request_key": "new-resend", "confirm_unknown": True}).status_code, 409)

    def test_rejects_zip_traversal_and_claimed_success_without_files(self):
        user = self.register()
        self.enable(user)
        job = self.client.post(BASE + "/jobs/archive", headers={"X-CSRF-Token": user["csrf_token"]}, json={"codes": "09-999-0001", "send_mail": False, "request_key": "archive"}).json()
        claim = self.claim().json()
        self.assertEqual(self.complete(claim, result={"archive_status": "success"}).status_code, 422)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("../evil.txt", "synthetic")
        self.assertEqual(self.complete(claim, result={"archive_status": "success"}, artifact_zip_base64=base64.b64encode(data.getvalue()).decode()).status_code, 422)
        self.assertEqual(self.client.get(BASE + f"/jobs/{job['id']}/download").status_code, 409)
        self.assertFalse((self.root / "evil.txt").exists())

    def test_cancel_queued_and_running_waits_for_worker_ack(self):
        user = self.register()
        self.enable(user)
        job = self.search(user).json()
        csrf = {"X-CSRF-Token": user["csrf_token"]}
        url = BASE + f"/jobs/{job['id']}/cancel"
        self.assertEqual(self.client.post(url, headers=csrf).json()["state"], "cancelled")
        retried = self.client.post(BASE + f"/jobs/{job['id']}/retry", headers=csrf, json={"request_key": "retry"})
        self.assertEqual(retried.status_code, 200, retried.text)
        claim = self.claim().json()
        self.assertEqual(self.client.post(BASE + f"/jobs/{retried.json()['id']}/cancel", headers=csrf).json()["state"], "running")
        heartbeat = self.client.post(BASE + f"/worker/jobs/{claim['job']['id']}/heartbeat", headers=self.worker, json={"lease_token": claim["lease_token"]})
        self.assertTrue(heartbeat.json()["cancel_requested"])
        self.assertEqual(self.complete(claim, state="cancelled").status_code, 200)

    def test_legacy_daily_quota_cannot_reject_second_search_after_first_finishes(self):
        user = self.register()
        self.enable(user)
        with self.db() as db:
            db.execute("UPDATE settings SET value='1' WHERE key='search_daily_limit'")
        self.assertEqual(self.search(user).status_code, 200)
        self.complete(self.claim().json(), result={"candidates": []})
        self.assertEqual(self.search(user, key="second").status_code, 200)
        self.assertEqual(self.client.get(BASE + "/auth/me").json()["quotas"]["search_used"], 2)

    def test_search_is_unlimited_by_default_and_preserves_existing_setting(self):
        user = self.register()
        self.assertEqual(user["quotas"]["search_daily_limit"], 0)
        self.assertEqual(user["quotas"]["archive_daily_limit"], 0)
        with self.db() as db:
            db.execute("UPDATE settings SET value='7' WHERE key='search_daily_limit'")
        from highscore_web import create_app
        create_app(self.root, {"secure_cookie": False})
        self.assertEqual(self.client.get(BASE + "/auth/me").json()["quotas"]["search_daily_limit"], 0)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='search_daily_limit'").fetchone()[0], '7')

    def test_zero_search_limit_allows_all_free_search_modes_after_legacy_usage(self):
        user = self.register()
        self.enable(user)
        from highscore_web.app import day
        with self.db() as db:
            db.execute("UPDATE settings SET value='0' WHERE key='search_daily_limit'")
            db.execute("UPDATE users SET is_admin=0 WHERE id=?", (user["id"],))
            db.execute("INSERT INTO quota_usage VALUES (?,?,?,?)", (user["id"], day(), 10001, 21))
        headers = {"X-CSRF-Token": user["csrf_token"]}
        before = self.client.get(BASE + "/auth/me").json()["credits"]
        for index, (mode, query, content) in enumerate((
            ("formula", "MnO2", "diffraction"), ("formula", "mno2", "structure"),
            ("codes", "00-002-0567", "diffraction"), ("codes", "ICSD-393", "structure"),
        )):
            body = {"mode": mode, "query": query, "content": content, "search_version": 2,
                    "request_key": "unlimited-" + str(index)}
            response = self.client.post(BASE + "/jobs/search", headers=headers, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.client.post(BASE + "/jobs/search", headers=headers, json=body).json()["id"], response.json()["id"])
            self.assertEqual(self.complete(self.claim().json(), result={"candidates": []}).status_code, 200)
        current = self.client.get(BASE + "/auth/me").json()
        self.assertFalse(current["is_admin"])
        self.assertEqual(current["credits"], before)
        self.assertEqual((current["quotas"]["search_used"], current["quotas"]["archive_used"]), (10005, 21))
        with self.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM xrd_credit_holds").fetchone()[0], 0)

    def test_legacy_daily_usage_cannot_block_search_or_credit_backed_archive(self):
        user = self.register()
        self.enable(user)
        from highscore_web.app import day
        with self.db() as db:
            db.execute("UPDATE settings SET value='2' WHERE key='search_daily_limit'")
            db.execute("INSERT INTO quota_usage VALUES (?,?,?,?)", (user["id"], day(), 0, 21))
        response = self.search(user)
        self.assertEqual(response.status_code, 200, response.text)
        self.complete(self.claim().json(), result={"candidates": []})
        archive = self.client.post(BASE + "/jobs/archive", headers={"X-CSRF-Token": user["csrf_token"]},
                                   json={"codes": "09-999-0001", "request_key": "archive-full"})
        self.assertEqual(archive.status_code, 200, archive.text)
        current = self.client.get(BASE + "/auth/me").json()
        self.assertEqual((current["credits"]["available"], current["credits"]["frozen"]), (9, 1))
        self.assertEqual(current["quotas"]["archive_used"], 22)

    def test_unlimited_search_retry_stays_free_and_zero_archive_limit_is_unlimited(self):
        user = self.register()
        self.enable(user)
        headers = {"X-CSRF-Token": user["csrf_token"]}
        with self.db() as db:
            db.execute("UPDATE settings SET value='0' WHERE key IN ('search_daily_limit','archive_daily_limit')")
        before = self.client.get(BASE + "/auth/me").json()["credits"]
        original = self.search(user)
        self.assertEqual(original.status_code, 200, original.text)
        self.assertEqual(self.client.post(BASE + f"/jobs/{original.json()['id']}/cancel", headers=headers).status_code, 200)
        retried = self.client.post(BASE + f"/jobs/{original.json()['id']}/retry", headers=headers,
                                   json={"request_key": "unlimited-retry"})
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(self.complete(self.claim().json(), result={"candidates": []}).status_code, 200)
        archive = self.client.post(BASE + "/jobs/archive", headers=headers,
                                   json={"codes": "09-999-0001", "request_key": "closed-archive"})
        self.assertEqual(archive.status_code, 200, archive.text)
        self.assertEqual(self.client.post(BASE + f"/jobs/{archive.json()['id']}/cancel", headers=headers).status_code, 200)
        current = self.client.get(BASE + "/auth/me").json()
        self.assertEqual(current["credits"], before)
        self.assertEqual((current["quotas"]["search_used"], current["quotas"]["archive_used"]), (2, 1))

    def test_admin_cannot_reenable_daily_limits_and_legacy_zero_patch_is_noop(self):
        user = self.register()
        self.enable(user)
        headers = {"X-CSRF-Token": user["csrf_token"]}
        with self.db() as db:
            db.execute("UPDATE settings SET value='10' WHERE key='search_daily_limit'")
            db.execute("UPDATE settings SET value='20' WHERE key='archive_daily_limit'")
            before = [tuple(row) for row in db.execute('SELECT * FROM settings ORDER BY key')]
            audit_count = db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
        for key in ('search_daily_limit', 'archive_daily_limit'):
            response = self.client.patch(BASE + '/admin/settings', headers=headers, json={key: 1, 'public_enabled': False})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn('积分', response.json()['detail'])
        response = self.client.patch(BASE + '/admin/settings', headers=headers,
                                     json={'search_daily_limit': 0, 'archive_daily_limit': 0})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['settings']['search_daily_limit'], 0)
        self.assertEqual(response.json()['settings']['archive_daily_limit'], 0)
        status = self.client.get(BASE + '/admin/status').json()['settings']
        self.assertEqual((status['search_daily_limit'], status['archive_daily_limit']), (0, 0))
        with self.db() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM settings ORDER BY key')], before)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM audit').fetchone()[0], audit_count)

    def test_search_usage_cannot_block_archive_and_credit_freeze_stays_atomic(self):
        user = self.register()
        self.enable(user)
        from highscore_web.app import day
        with self.db() as db:
            db.execute("UPDATE settings SET value='1' WHERE key='search_daily_limit'")
            db.execute("INSERT INTO quota_usage VALUES (?,?,?,?)", (user["id"], day(), 11, 0))
        headers = {"X-CSRF-Token": user["csrf_token"]}
        body = {"codes": "09-999-0001", "request_key": "own-archive-quota"}
        archive = self.client.post(BASE + "/jobs/archive", headers=headers, json=body)
        self.assertEqual(archive.status_code, 200, archive.text)
        self.assertEqual(self.client.post(BASE + "/jobs/archive", headers=headers, json=body).json()["id"], archive.json()["id"])
        current = self.client.get(BASE + "/auth/me").json()
        self.assertEqual((current["credits"]["available"], current["credits"]["frozen"]), (9, 1))
        self.assertEqual((current["quotas"]["search_used"], current["quotas"]["archive_used"]), (11, 1))
        self.assertEqual(self.client.post(BASE + f"/jobs/{archive.json()['id']}/cancel", headers=headers).status_code, 200)
        current = self.client.get(BASE + "/auth/me").json()
        self.assertEqual((current["credits"]["available"], current["credits"]["frozen"]), (10, 0))

    def test_mail_retry_is_not_blocked_by_exhausted_daily_counters(self):
        user = self.register()
        self.enable(user)
        headers = {"X-CSRF-Token": user["csrf_token"]}
        archive = self.client.post(BASE + "/jobs/archive", headers=headers,
                                   json={"codes": "09-999-0001", "send_mail": False, "request_key": "mail-quota"})
        self.assertEqual(archive.status_code, 200, archive.text)
        done = self.complete(self.claim().json(), result={"archive_status": "success", "mail_status": "not_requested"},
                             artifact_zip_base64=base64.b64encode(self.archive_zip()).decode())
        self.assertEqual(done.status_code, 200, done.text)
        with self.db() as db:
            db.execute("UPDATE settings SET value='1' WHERE key='search_daily_limit'")
            db.execute("UPDATE quota_usage SET searches=11,cards=21 WHERE user_id=?", (user["id"],))
        before = self.client.get(BASE + "/auth/me").json()
        response = self.client.post(BASE + f"/jobs/{archive.json()['id']}/mail-retry", headers=headers,
                                    json={"request_key": "free-mail-retry"})
        self.assertEqual(response.status_code, 200, response.text)
        after = self.client.get(BASE + "/auth/me").json()
        self.assertEqual(after["credits"], before["credits"])
        self.assertEqual(after["quotas"], before["quotas"])

    def test_registration_and_login_rate_limit_do_not_trust_forwarded_ip(self):
        body = {"email": "limited@example.com", "password": "synthetic-password-123"}
        for _ in range(3):
            self.assertEqual(self.client.post(BASE + "/auth/register", json=body).status_code, 200)
        limited = self.client.post(BASE + "/auth/register", headers={"X-Forwarded-For": "192.0.2.10"}, json=body)
        self.assertEqual(limited.status_code, 429)
        for _ in range(10):
            self.assertEqual(self.client.post(BASE + "/auth/login", json=body).status_code, 401)
        self.assertEqual(self.client.post(BASE + "/auth/login", json=body).status_code, 429)

    def test_unconfigured_worker_key_and_cross_origin_register_rejected(self):
        from highscore_web import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(self.root, {"worker_token": "", "secure_cookie": False})) as client:
            result = client.post(BASE + "/worker/claim", headers={"Authorization": "Bearer "}, json={"worker_id": "x", "database_id": "x", "capabilities": []})
            self.assertEqual(result.status_code, 401)
        result = self.client.post(BASE + "/auth/register", headers={"Origin": "http://evil.invalid"}, json={"email": "other@example.com", "password": "synthetic-password-123"})
        self.assertEqual(result.status_code, 403)

    def test_expired_desktop_lease_blocks_others_but_not_verification_mail(self):
        user = self.register()
        self.enable(user)
        job = self.search(user).json()
        claim = self.claim().json()
        with self.db() as db:
            db.execute("UPDATE jobs SET lease_expires=0 WHERE id=?", (job["id"],))
        another = self.register("second@example.com")
        self.assertEqual(self.search(another, key="second").status_code, 200)
        self.assertIsNone(self.claim().json()["job"])

    def test_search_over_limit_is_rejected_without_partial_candidates(self):
        user = self.register()
        self.enable(user)
        job = self.search(user).json()
        claim = self.claim().json()
        result = {"candidates": [{"code": f"09-999-{n:04d}", "phase_name": "synthetic", "chemical_formula": "X", "source": "synthetic"} for n in range(101)]}
        self.assertEqual(self.complete(claim, result=result).status_code, 422)
        self.assertEqual(self.client.get(BASE + f"/jobs/{job['id']}").json()["candidates"], [])

    def test_validation_errors_never_echo_password_or_worker_tokens(self):
        response = self.client.post(BASE + "/auth/register", json={"email": "invalid", "password": "secret-short"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("secret-short", response.text)
        response = self.client.post(BASE + "/worker/claim", headers=self.worker, json={"worker_id": "bad", "database_id": "x", "capabilities": [], "token": "private-synthetic-token"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-synthetic-token", response.text)

    def test_download_rechecks_changed_file_instead_of_serving_stale_zip(self):
        user = self.register()
        self.enable(user)
        job = self.client.post(BASE + "/jobs/archive", headers={"X-CSRF-Token": user["csrf_token"]}, json={"codes": "09-999-0001", "send_mail": False, "request_key": "archive"}).json()
        claim = self.claim().json()
        self.assertEqual(self.complete(claim, result={"archive_status": "success"}, artifact_zip_base64=base64.b64encode(self.archive_zip()).decode()).status_code, 200)
        file = self.root / f"output/web/artifacts/{job['id']}/output/cards/09-999-0001/09-999-0001.txt"
        file.write_bytes(b"corrupt synthetic")
        self.assertEqual(self.client.get(BASE + f"/jobs/{job['id']}/download").status_code, 409)
        self.assertEqual(self.client.post(BASE + f"/jobs/{job['id']}/mail-retry", headers={"X-CSRF-Token": user["csrf_token"]}, json={"request_key": "mail"}).status_code, 409)

    def test_parallel_duplicate_submission_charges_quota_once(self):
        user = self.register()
        self.enable(user)
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.search(user), range(4)))
        self.assertEqual([r.status_code for r in responses], [200] * 4)
        self.assertEqual(len({r.json()["id"] for r in responses}), 1)
        self.assertEqual(self.client.get(BASE + "/auth/me").json()["quotas"]["search_used"], 1)

    def test_expired_verification_can_explicitly_resend_with_password(self):
        body = {"email": "expired@example.com", "password": "synthetic-password-123"}
        self.client.post(BASE + "/auth/register", json=body)
        first = self.claim(["verification_mail"]).json()
        old_token = parse_qs(urlparse(first["job"]["payload"]["verify_url"]).query)["verify"][0]
        self.complete(first, result={"mail_status": "submitted"})
        with self.db() as db:
            db.execute("UPDATE verifications SET expires=0")
        response = self.client.post(BASE + "/auth/resend-verification", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("token", response.text)
        second = self.claim(["verification_mail"]).json()
        new_token = parse_qs(urlparse(second["job"]["payload"]["verify_url"]).query)["verify"][0]
        self.assertNotEqual(new_token, old_token)
        self.assertEqual(self.client.post(BASE + "/auth/verify", json={"token": old_token}).status_code, 400)
        self.assertEqual(self.client.post(BASE + "/auth/verify", json={"token": new_token}).status_code, 200)

    def test_database_report_has_explicit_provenance_label(self):
        user = self.register()
        self.enable(user)
        job = self.client.post(BASE + "/jobs/archive", headers={"X-CSRF-Token": user["csrf_token"]}, json={"codes": "09-999-0001", "send_mail": False, "request_key": "archive"}).json()
        claim = self.claim().json()
        self.assertEqual(self.complete(claim, result={"archive_status": "success"}, artifact_zip_base64=base64.b64encode(self.archive_zip(database_report=True)).decode()).status_code, 200)
        result = self.client.get(BASE + f"/jobs/{job['id']}").json()["results"][0]
        self.assertEqual(result["archive_type"], "database_report")
        self.assertIn("数据库报告", result["archive_label"])

    def test_unknown_from_later_delivery_cannot_be_bypassed_using_original_archive(self):
        user = self.register()
        self.enable(user)
        csrf = {"X-CSRF-Token": user["csrf_token"]}
        job = self.client.post(BASE + "/jobs/archive", headers=csrf, json={"codes": "09-999-0001", "send_mail": True, "request_key": "archive"}).json()
        claim = self.claim().json()
        self.assertEqual(self.complete(claim, state="failed", result={"archive_status": "success", "mail_status": "failed"}, artifact_zip_base64=base64.b64encode(self.archive_zip()).decode()).status_code, 200)
        url = BASE + f"/jobs/{job['id']}/mail-retry"
        first = self.client.post(url, headers=csrf, json={"request_key": "resend-1"})
        self.assertEqual(first.status_code, 200)
        claim = self.claim().json()
        self.assertEqual(self.complete(claim, state="needs_attention", result={"mail_status": "unknown"}).status_code, 200)
        self.assertEqual(self.client.post(url, headers=csrf, json={"request_key": "resend-2"}).status_code, 409)
        self.assertEqual(self.client.post(BASE + f"/jobs/{job['id']}/retry", headers=csrf, json={"request_key": "retry-archive"}).status_code, 409)
        second = self.client.post(url, headers=csrf, json={"request_key": "resend-2", "confirm_unknown": True})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.client.post(BASE + f"/jobs/{second.json()['id']}/cancel", headers=csrf).json()["state"], "cancelled")
        self.assertEqual(self.client.post(url, headers=csrf, json={"request_key": "resend-after-cancel"}).status_code, 409,
                         "取消尚未发送的后来重发不能清除前一次投递未知记录")

    def test_chunked_body_cannot_bypass_request_size_limit(self):
        payload = json.dumps({"email": "chunked@example.com", "password": "synthetic-password-123", "padding": "x" * 25000}).encode()
        chunks = (payload[index:index + 1000] for index in range(0, len(payload), 1000))
        response = self.client.post(BASE + "/auth/register", content=chunks, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(self.client.post(BASE + "/auth/register", content=b"{}", headers={"Content-Length": "25001", "Content-Type": "application/json"}).status_code, 413)

    def test_partial_archive_download_and_mail_only_include_verified_subset(self):
        user = self.register()
        self.enable(user)
        csrf = {"X-CSRF-Token": user["csrf_token"]}
        job = self.client.post(BASE + "/jobs/archive", headers=csrf, json={"codes": "09-999-0001 09-999-0002", "send_mail": True, "request_key": "partial"}).json()
        claim = self.claim().json()
        response = self.complete(claim, state="needs_attention", result={"archive_status": "partial", "mail_status": "submitted", "results": [{"code": "09-999-0002", "status": "not_found", "message": "synthetic 数据库未找到该编号"}]}, artifact_zip_base64=base64.b64encode(self.archive_zip()).decode())
        self.assertEqual(response.status_code, 200, response.text)
        current = self.client.get(BASE + f"/jobs/{job['id']}").json()
        self.assertEqual(current["archive_status"], "partial")
        self.assertEqual(current["state"], "needs_attention")
        self.assertTrue(current["download_available"])
        self.assertEqual(next(item for item in current["results"] if item["code"] == "09-999-0002")["status"], "not_found")
        response = self.client.get(BASE + f"/jobs/{job['id']}/download")
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(len(archive.namelist()), 5)
            self.assertTrue(all("09-999-0001" in name for name in archive.namelist()))
        resend = self.client.post(BASE + f"/jobs/{job['id']}/mail-retry", headers=csrf, json={"request_key": "partial-resend"})
        self.assertEqual(resend.status_code, 200, resend.text)
        claimed = self.claim().json()
        self.assertEqual(claimed["job"]["payload"]["codes"], "09-999-0001")
        self.assertEqual(self.complete(claimed, result={"mail_status": "submitted"}).status_code, 200)

    def test_partial_archive_rejects_unrequested_card_and_false_full_success(self):
        user = self.register()
        self.enable(user)
        job = self.client.post(BASE + "/jobs/archive", headers={"X-CSRF-Token": user["csrf_token"]}, json={"codes": "09-999-0001 09-999-0002", "send_mail": False, "request_key": "partial"}).json()
        claim = self.claim().json()
        encoded = base64.b64encode(self.archive_zip()).decode()
        self.assertEqual(self.complete(claim, result={"archive_status": "success"}, artifact_zip_base64=encoded).status_code, 422)
        self.assertEqual(self.complete(claim, state="succeeded", result={"archive_status": "partial"}, artifact_zip_base64=encoded).status_code, 422)
        extra = base64.b64encode(self.archive_zip(codes=["09-999-0001", "09-999-0003"])).decode()
        self.assertEqual(self.complete(claim, state="needs_attention", result={"archive_status": "partial"}, artifact_zip_base64=extra).status_code, 422)
        self.assertFalse(self.client.get(BASE + f"/jobs/{job['id']}").json()["download_available"])

    def test_cancelled_partial_keeps_verified_files_and_no_fake_mail_submission(self):
        user = self.register()
        self.enable(user)
        job = self.client.post(BASE + "/jobs/archive", headers={"X-CSRF-Token": user["csrf_token"]}, json={"codes": "09-999-0001 09-999-0002", "send_mail": True, "request_key": "partial"}).json()
        claim = self.claim().json()
        response = self.complete(claim, state="cancelled", result={"archive_status": "partial", "mail_status": "not_started"}, artifact_zip_base64=base64.b64encode(self.archive_zip()).decode())
        self.assertEqual(response.status_code, 200, response.text)
        current = self.client.get(BASE + f"/jobs/{job['id']}").json()
        self.assertEqual(current["state"], "cancelled")
        self.assertEqual(current["mail_status"], "not_started")
        self.assertTrue(current["download_available"])
        self.assertEqual(next(item for item in current["results"] if item["code"] == "09-999-0002")["status"], "not_started")

    def test_partial_candidate_zip_still_requires_bound_database_source(self):
        user = self.register()
        self.enable(user)
        search = self.search(user).json()
        self.complete(self.claim().json(), result={"candidates": [{"code": code, "phase_name": "synthetic", "source": "bound-synthetic-source"} for code in ["09-999-0001", "09-999-0002"]]})
        candidates = self.client.get(BASE + f"/jobs/{search['id']}").json()["candidates"]
        job = self.client.post(BASE + "/jobs/archive", headers={"X-CSRF-Token": user["csrf_token"]}, json={"search_job_id": search["id"], "candidate_ids": [item["id"] for item in candidates], "send_mail": False, "request_key": "partial"})
        self.assertEqual(job.status_code, 200)
        claim = self.claim().json()
        response = self.complete(claim, state="needs_attention", result={"archive_status": "partial"}, artifact_zip_base64=base64.b64encode(self.archive_zip()).decode())
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
