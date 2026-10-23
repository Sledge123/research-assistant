"""中文网站 API；数据库执行器通过独立 HTTPS 队列连接。"""
from datetime import datetime, timedelta, timezone
import hmac
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Literal
from urllib.parse import urlparse
import uuid

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from highscore_downloader.archive import ArchiveError
from highscore_downloader.mail_settings import validate_email
from highscore_downloader.numbers import parse_code, parse_codes
from .artifacts import accept_zip, checked_zip, public_results
from .database import Database, dumps
from .debug_accounts import is_debug
from .credits import Credits,CreditError
from .limits import RequestSizeLimit
from .security import digest, new_token, password_hash, password_matches
from .xrd_messages import public_message,public_card_results

PREFIX = "/highscore/api"
COOKIE = "highscore_session"
SHANGHAI = timezone(timedelta(hours=8))
LEASE_SECONDS = 60
TERMINAL = {"succeeded", "failed", "needs_attention", "cancelled"}
DUMMY_PASSWORD_HASH = "pbkdf2_sha256$600000$" + "00" * 16 + "$" + "00" * 32


def uid():
    return uuid.uuid4().hex


def day():
    return datetime.now(SHANGHAI).date().isoformat()


def error(status, message):
    raise HTTPException(status, message)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Credentials(Model):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=128)


class Verify(Model):
    token: str = Field(min_length=32, max_length=128)


class Search(Model):
    mode: Literal["compound_name", "mineral_name", "formula", "codes"]
    content: Literal["diffraction", "structure"] = "diffraction"
    query: str = Field(min_length=1, max_length=1000)
    search_version: Literal[2] | None = None
    request_key: str = Field(min_length=1, max_length=100)
    database_id: str | None = Field(default=None, min_length=1, max_length=100)


class Archive(Model):
    codes: str | None = Field(default=None, max_length=1000)
    search_job_id: str | None = Field(default=None, max_length=64)
    candidate_ids: list[str] | None = Field(default=None, max_length=20)
    send_mail: bool = False
    request_key: str = Field(min_length=1, max_length=100)
    database_id: str | None = Field(default=None, min_length=1, max_length=100)


class Retry(Model):
    request_key: str = Field(min_length=1, max_length=100)
    confirm_unknown: bool = False


class Settings(Model):
    license_confirmed: bool | None = None
    cloud_validated: bool | None = None
    public_enabled: bool | None = None
    search_daily_limit: int | None = Field(default=None, ge=0)
    archive_daily_limit: int | None = Field(default=None, ge=0)


class Claim(Model):
    worker_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    database_id: str = Field(min_length=1, max_length=200)
    capabilities: list[Literal["search", "archive", "mail_retry", "verification_mail"]] = Field(max_length=4)


class Heartbeat(Model):
    lease_token: str = Field(min_length=32, max_length=128)
    progress: int | None = Field(default=None, ge=0, le=100)
    message: str | None = Field(default=None, max_length=1000)


class Candidate(Model):
    code: str = Field(min_length=1, max_length=30)
    phase_name: str = Field(min_length=1, max_length=300)
    chemical_formula: str | None = Field(default=None, max_length=300)
    source: str = Field(min_length=1, max_length=300)
    mineral_name: str | None = Field(default=None, max_length=300)
    space_group: str | None = Field(default=None, max_length=300)
    cell_parameters: dict[str, str | int | float | None] | None = None
    peak_count: int | None = Field(default=None, ge=0)
    site_count: int | None = Field(default=None, ge=0)
    content_type: Literal["diffraction", "structure"] | None = None
    exportable: bool | None = None
    support_message: str | None = Field(default=None, max_length=1000)

    @field_validator('cell_parameters')
    @classmethod
    def valid_cell_parameters(cls, value):
        if value is None:
            return value
        if not set(value).issubset({'a', 'b', 'c', 'alpha', 'beta', 'gamma'}):
            raise ValueError('晶胞参数字段无效。')
        for item in value.values():
            if isinstance(item, (int, float)):
                try:
                    if not math.isfinite(item):
                        raise ValueError('晶胞参数必须为有限值。')
                except OverflowError:
                    raise ValueError('晶胞参数超出有限范围。') from None
            if isinstance(item, str) and (len(item) > 100 or any(ord(char) < 32 for char in item)):
                raise ValueError('晶胞参数文本无效。')
        return value


class Result(Model):
    candidates: list[Candidate] | None = None
    results: list[dict] | None = Field(default=None, max_length=20)
    archive_status: Literal["not_started", "success", "failed", "partial"] | None = None
    mail_status: Literal["not_requested", "not_started", "pending", "submitted", "failed", "unknown", "offline_validated"] | None = None
    message: str | None = Field(default=None, max_length=2000)


class Complete(Model):
    lease_token: str = Field(min_length=32, max_length=128)
    state: Literal["succeeded", "failed", "needs_attention", "cancelled"]
    result: Result
    artifact_zip_base64: str | None = None


def create_app(root=None, settings=None):
    """root 为项目根；测试可覆盖配置但不能绕过权限或归档复验。"""
    root = Path(root or os.getenv("HIGHSCORE_ROOT", ".")).resolve()
    if not root.is_dir():
        raise ValueError("项目目录不存在。")
    config = {"public_url": os.getenv("HIGHSCORE_PUBLIC_URL", "http://127.0.0.1:8787/highscore/"),
              "worker_token": os.getenv("HIGHSCORE_WORKER_TOKEN", ""),
              "literature_worker_token": os.getenv("LITERATURE_WORKER_TOKEN", ""),
              "literature_mail_worker_token": os.getenv("LITERATURE_MAIL_WORKER_TOKEN", ""),
              "abtem_worker_token": os.getenv("HIGHSCORE_ABTEM_WORKER_TOKEN", ""),
              "abtem_enabled": os.getenv("HIGHSCORE_ABTEM_ENABLED", "false").lower() == "true",
              "pymatgen_worker_token": os.getenv("HIGHSCORE_PYMATGEN_WORKER_TOKEN", ""),
              "pymatgen_enabled": os.getenv("HIGHSCORE_PYMATGEN_ENABLED", "false").lower() == "true",
              "research_library_quota_bytes": int(os.getenv("HIGHSCORE_RESEARCH_LIBRARY_QUOTA_BYTES", str(1536 * 1024 ** 2))),
              "research_library_min_free_bytes": int(os.getenv("HIGHSCORE_RESEARCH_LIBRARY_MIN_FREE_BYTES", str(1024 ** 3))),
              "database_admin_only": os.getenv("HIGHSCORE_DATABASE_ADMIN_ONLY", "false").lower() == "true",
              "database_catalog": json.loads(os.getenv("HIGHSCORE_DATABASE_CATALOG", "[]")),
              "database_source_types": json.loads(os.getenv("HIGHSCORE_DATABASE_SOURCE_TYPES", "[]")),
              "database_auto_id": os.getenv("HIGHSCORE_DATABASE_AUTO_ID", ""),
              "secure_cookie": os.getenv("HIGHSCORE_COOKIE_SECURE", "true").lower() != "false"}
    if settings:
        config.update(settings)
    catalog = config['database_catalog']
    if (not isinstance(catalog,list) or len(catalog)>20
            or any(not isinstance(item,dict) or set(item)!={'id','name'}
                   or not isinstance(item['id'],str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}',item['id'])
                   or not isinstance(item['name'],str) or not item['name'].strip() or len(item['name'])>300 for item in catalog)
            or len({item['id'] for item in catalog})!=len(catalog)):
        raise ValueError('数据库目录必须使用不重复的标识及公开来源名称。')
    automatic_database_id=config['database_auto_id']
    if automatic_database_id and automatic_database_id not in {item['id'] for item in catalog}:
        raise ValueError('自动检索标识必须绑定已配置数据库目录。')
    default_database_id = automatic_database_id or (catalog[0]['id'] if catalog else None)
    source_types = config['database_source_types']
    profiles = {'pdf2_2.3', 'cod2024_hs4x', 'jade_pdf4_2009_index_v1',
                'jade_pdf4_2009_index_v2', 'jade_icsd_structure_v1'}
    if (not isinstance(source_types, list) or len(source_types) > 100
            or any(not isinstance(item, dict) or set(item) != {'database_id', 'source', 'reader_profile'}
                   or not isinstance(item['database_id'], str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', item['database_id'])
                   or not isinstance(item['source'], str) or not item['source'].strip() or len(item['source']) > 300
                   or item['reader_profile'] not in profiles for item in source_types)
            or len({(item['database_id'], item['source']) for item in source_types}) != len(source_types)):
        raise ValueError('候选数据范围必须绑定明确且不重复的执行器、来源及读取格式。')
    source_profiles = {(item['database_id'], item['source']): item['reader_profile'] for item in source_types}

    def candidate_scope(item, database_id):
        from highscore_database.candidate_scope import candidate_scope as describe
        profile = source_profiles.get((database_id, item['source']))
        if profile is not None:
            item.update(describe(item, reader_profile=profile))
        return item

    def resolve_database(requested):
        if automatic_database_id:
            if requested not in (None,automatic_database_id):error(422,'数据库由系统自动检索，无需选择。')
            return automatic_database_id
        if requested is None:return default_database_id
        if requested not in {item['id'] for item in catalog}:
            error(422,'指定数据库未配置，请刷新数据库列表。')
        return requested
    parsed = urlparse(config["public_url"])
    if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path != "/highscore/"
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("网站地址必须为完整的 http(s)://主机/highscore/。")
    if config["secure_cookie"] and parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("生产网站需要 HTTPS。")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    store = Database(root)
    app = FastAPI(title="科研助手", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestSizeLimit)
    app.state.database = store
    app.state.root = root

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # FastAPI 默认 input 字段可能回显密码、验证令牌和整个上传内容。
        return JSONResponse(status_code=422, content={"detail": "请求格式有误，请检查必填字段、格式及数量限制。"})

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith(PREFIX) else "no-cache"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        return response

    def check_origin(request: Request):
        sent = request.headers.get("origin")
        if sent is not None and sent != origin:
            error(403, "禁止跨站请求。")
        if request.headers.get("sec-fetch-site") == "cross-site":
            error(403, "禁止跨站请求。")

    def session(request: Request):
        token = request.cookies.get(COOKIE, "")
        with store.connection() as db:
            row = db.execute("""SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id
              WHERE s.token_hash=? AND s.expires>? AND u.verified=1""", (digest(token), time.time())).fetchone()
        if row is None:
            error(401, "请使用已验证邮箱登录。")
        return dict(row)

    def writer(request: Request, user=Depends(session)):
        check_origin(request)
        if not hmac.compare_digest(request.headers.get("X-CSRF-Token", "").encode(), user["csrf"].encode()):
            error(403, "安全校验失败，请刷新后重试。")
        return user

    def admin(user=Depends(session)):
        if not user["is_admin"]:
            error(403, "仅管理员可执行此操作。")
        return user

    def admin_writer(user=Depends(writer)):
        if not user["is_admin"]:
            error(403, "仅管理员可执行此操作。")
        return user

    def worker(request: Request):
        token = config["worker_token"]
        given = request.headers.get("Authorization", "")
        if not isinstance(token, str) or len(token) < 32 or not hmac.compare_digest(given.encode(), f"Bearer {token}".encode()):
            error(401, "执行器认证失败。")

    def limited(scope, key, maximum, seconds):
        now = time.time()
        with store.connection(write=True) as db:
            db.execute("DELETE FROM rate_events WHERE created_at<?", (now - 86400,))
            count = db.execute("SELECT COUNT(*) FROM rate_events WHERE scope=? AND key_hash=? AND created_at>?",
                               (scope, digest(key), now - seconds)).fetchone()[0]
            if count >= maximum:
                error(429, "操作过于频繁，请稍后再试。")
            db.execute("INSERT INTO rate_events VALUES (?,?,?)", (scope, digest(key), now))

    def auth_limits(request, email, action):
        address = request.client.host if request.client else "unknown"
        # 不读取可伪造的 X-Forwarded-For；邮箱额度同时限制代理后的同邮箱攻击。
        limited(action + "_ip", address, 30 if action == "login" else 10, 900 if action == "login" else 3600)
        limited(action + "_email", email.casefold(), 10 if action == "login" else 3, 900 if action == "login" else 3600)

    def own(db, job_id, user):
        row = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=? AND kind!='verification_mail'",
                         (job_id, user["id"])).fetchone()
        if row is None:
            error(404, "任务不存在。")
        return row

    def quotas(db, user):
        used = db.execute("SELECT * FROM quota_usage WHERE user_id=? AND day=?", (user["id"], day())).fetchone()
        # 兼容旧客户端的字段；日用量仅作统计，XRD仅通过共用积分结算。
        return {"search_daily_limit": 0, "archive_daily_limit": 0,
                "search_used": used["searches"] if used else 0, "archive_used": used["cards"] if used else 0}

    def public_xrd_settings(db):
        return {key: 0 if key in ("search_daily_limit", "archive_daily_limit") else value
                for key, value in store.settings(db).items() if key != "literature_enabled"}

    def expected(job):
        if job["artifact_expected"]:
            return json.loads(job["artifact_expected"])
        payload = json.loads(job["payload"])
        return payload.get("selected") or [{"code": code.original} for code in parse_codes(payload["codes"])]

    def failed_results(row, worker_results, verified):
        """仅保留请求内未取得已验文件的失败记录；成功结果只能由服务器回执产生。"""
        requested = expected(row)
        reported = {}
        for item in worker_results or []:
            if not isinstance(item, dict) or item.get("status") in ("success", "skipped"):
                continue
            code = item.get("code") or item.get("input_code")
            if not isinstance(code, str):
                continue
            matches = [index for index, source in enumerate(requested) if parse_code(source["code"]).matches(code)]
            if len(matches) != 1:
                error(422, "失败记录包含不属于原请求的编号。")
            status = item.get("status")
            status = status if status in ("failed", "not_found", "not_started", "cancelled", "needs_attention") else "failed"
            message = item.get("message") or item.get("error") or "该编号尚无通过复验的归档。"
            if status == "not_found":
                message = "未发现该卡片信息"
            reported[matches[0]] = {"code": requested[matches[0]]["code"], "status": status,
                                   "message": str(message)[:1000]}
        failures = []
        for index, source in enumerate(requested):
            if any(parse_code(source["code"]).matches(receipt["code"]) for receipt in verified):
                continue
            failures.append(reported.get(index, {"code": source["code"], "status": "not_started",
                                                 "message": "该编号未取得通过复验的归档。"}))
        return failures

    def delivery_history(db, job):
        """查询同一次归档及所有普通重试关联的投递，防止改从旧任务入口清空未知状态。"""
        archives = {row["id"]: row for row in db.execute("SELECT * FROM jobs WHERE user_id=? AND kind='archive'", (job["user_id"],))}
        root_id, seen = job["id"], set()
        while root_id in archives and root_id not in seen:
            seen.add(root_id)
            parent = json.loads(archives[root_id]["payload"]).get("retry_of")
            if not parent or parent not in archives:
                break
            root_id = parent
        family = {root_id}
        changed = True
        while changed:
            changed = False
            for item in archives.values():
                if item["id"] not in family and json.loads(item["payload"]).get("retry_of") in family:
                    family.add(item["id"])
                    changed = True
        deliveries = []
        for item in archives.values():
            if item["id"] in family and json.loads(item["payload"]).get("send_mail"):
                deliveries.append(item)
        for item in db.execute("SELECT * FROM jobs WHERE user_id=? AND kind='mail_retry'", (job["user_id"],)):
            if json.loads(item["payload"]).get("original_job_id") in family:
                deliveries.append(item)
        deliveries.sort(key=lambda item: (item["created_at"], item["id"]))
        return deliveries

    def candidate_page(db, row, offset=0, limit=50):
        total = db.execute('SELECT count(*) FROM candidates WHERE job_id=?', (row['id'],)).fetchone()[0]
        items = []
        for candidate in db.execute('SELECT c.id,c.code,c.phase_name,c.chemical_formula,c.source,d.details '
                'FROM candidates c LEFT JOIN candidate_details d ON c.id=d.candidate_id '
                'WHERE c.job_id=? ORDER BY c.rowid LIMIT ? OFFSET ?', (row['id'], limit, offset)):
            item = {key: candidate[key] for key in ('id', 'code', 'phase_name', 'chemical_formula', 'source')}
            if candidate['details']:
                item.update(json.loads(candidate['details']))
            items.append(candidate_scope(item, row['database_id']))
        next_offset = offset + len(items) if offset + len(items) < total else None
        return {'items': items, 'total': total, 'next_offset': next_offset}

    def view(db, row):
        payload = json.loads(row['payload'])
        version_two = row['kind'] == 'search' and payload.get('search_version') == 2
        context = {}
        if row['kind'] == 'search':
            query, mode, content = payload.get('query'), payload.get('mode'), payload.get('content', 'diffraction')
            if isinstance(query, str) and len(query) <= (1000 if mode == 'codes' else 200):
                context['search_query'] = query
            if mode in ('codes', 'formula'):
                context['search_mode'] = mode
            if content in ('diffraction', 'structure'):
                context['content_type'] = content
        elif row['kind'] == 'archive':
            parent = payload.get('search_job_id')
            if (isinstance(parent, str) and len(parent) <= 128 and db.execute(
                    "SELECT 1 FROM jobs WHERE id=? AND user_id=? AND kind='search'",
                    (parent, row['user_id'])).fetchone()):
                context['search_job_id'] = parent
        page = candidate_page(db, row) if version_two else None
        candidates = page['items'] if page is not None else [candidate_scope(dict(item), row['database_id']) for item in db.execute("SELECT id,code,phase_name,chemical_formula,source FROM candidates WHERE job_id=? ORDER BY rowid", (row["id"],))]
        results=public_card_results(json.loads(row['results']))
        credit=Credits.xrd_status(db,row['id'])
        message=public_message(row,candidates if row['kind']=='search' else results,credit)
        return {"id": row["id"], "kind": row["kind"], "state": row["state"],
                "archive_status": row["archive_status"], "mail_status": row["mail_status"],
                "message": message, "results": results, "candidates": candidates,
                "created_at": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat(),
                "progress": row["progress"], "credits": credit,
                "download_available": bool(row["artifact_dir"] and row["archive_status"] in ("success", "partial")),
                **context,
                **({'search_version': 2, 'candidate_total': page['total'], 'candidate_next_offset': page['next_offset']}
                   if version_two else {})}

    def enabled(db):
        flags = store.settings(db)
        if not all(flags[key] for key in ("license_confirmed", "cloud_validated", "public_enabled")):
            error(503, "公众检索暂未开放，请等待授权及云环境验收完成。")

    def enqueue(db, user, kind, payload, request_key, *, database_id=None, searches=0, cards=0):
        if is_debug(db, user['id']) and (kind == 'mail_retry' or payload.get('send_mail')):
            error(422, '调试账号不发送邮件。')
        if config["database_admin_only"] and not user["is_admin"]:
            error(503, "数据库目前仅供管理员私有使用，公众检索尚未开放。")
        fingerprint = digest(dumps({"kind": kind, "payload": payload, "database_id": database_id}))
        existing = db.execute("SELECT * FROM jobs WHERE user_id=? AND request_key=?", (user["id"], request_key)).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                error(409, "请求标识已用于不同的操作。")
            return view(db, existing)
        enabled(db)
        store.expire(db)
        if db.execute("SELECT 1 FROM jobs WHERE user_id=? AND state IN ('queued','running') AND kind!='verification_mail'", (user["id"],)).fetchone():
            error(409, "请先等待当前任务结束。")
        # 保留原日统计，不再作为准入门槛；归档仍在同一事务内冻结共用积分。
        db.execute("INSERT INTO quota_usage(user_id,day,searches,cards) VALUES (?,?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET searches=searches+excluded.searches,cards=cards+excluded.cards",
                   (user["id"], day(), searches, cards))
        job_id, now = uid(), time.time()
        mail_status = "pending" if payload.get("send_mail") else "not_requested"
        db.execute("""INSERT INTO jobs(id,user_id,kind,state,payload,fingerprint,request_key,database_id,
          mail_status,created_at,updated_at) VALUES (?,?,?,'queued',?,?,?,?,?,?,?)""",
                   (job_id, user["id"], kind, dumps(payload), fingerprint, request_key, database_id, mail_status, now, now))
        if kind=='archive':
            try:Credits.reserve_xrd(db,user['id'],job_id,cards)
            except CreditError as exc:error(402,str(exc))
        return view(db, db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def queue_verification(db, user_id, email):
        if is_debug(db, user_id):
            return
        now, token = time.time(), new_token()
        db.execute("DELETE FROM verifications WHERE user_id=?", (user_id,))
        db.execute("UPDATE jobs SET state='cancelled',payload='{}',updated_at=? WHERE user_id=? AND kind='verification_mail' AND state='queued'", (now, user_id))
        db.execute("INSERT INTO verifications VALUES (?,?,?)", (digest(token), user_id, now + 86400))
        payload = {"to": email, "verify_url": config["public_url"] + "?verify=" + token}
        db.execute("""INSERT INTO jobs(id,user_id,kind,state,payload,fingerprint,request_key,mail_status,created_at,updated_at)
          VALUES (?,?,'verification_mail','queued',?,?,?,'pending',?,?)""",
                   (uid(), user_id, dumps(payload), digest(dumps(payload)), "verify:" + uid(), now, now))

    def database_worker_online(db):
        return bool(db.execute("""SELECT 1 FROM workers,json_each(workers.capabilities) capability
          WHERE last_seen>? AND capability.value IN ('search','archive','mail_retry')
          AND (?='' OR database_id=?) LIMIT 1""",
                               (time.time() - 90,automatic_database_id,automatic_database_id)).fetchone())

    @app.get(PREFIX + "/health")
    def health():
        with store.connection() as db:
            flags = store.settings(db)
            online = database_worker_online(db)
        gates = {key: flags[key] for key in ("license_confirmed", "cloud_validated", "public_enabled")}
        return {"status": "ok", "service_enabled": all(gates.values()), "worker_online": online,
                "database_admin_only": config["database_admin_only"], "gates": gates}

    @app.post(PREFIX + "/auth/register", dependencies=[Depends(check_origin)])
    def register(body: Credentials, request: Request):
        try:
            email = validate_email(body.email).casefold()
        except ValueError:
            error(422, "请输入有效的单个邮箱地址。")
        auth_limits(request, email, "register")
        hashed = password_hash(body.password)
        with store.connection(write=True) as db:
            if not db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                user_id, now = uid(), time.time()
                db.execute("INSERT INTO users VALUES (?,?,?,0,0,?)", (user_id, email, hashed, now))
                queue_verification(db, user_id, email)
        return {"message": "如该邮箱可以注册，验证邮件将在队列处理后发送，请查收。"}

    @app.post(PREFIX + "/auth/resend-verification", dependencies=[Depends(check_origin)])
    def resend_verification(body: Credentials, request: Request):
        email = body.email.casefold()
        auth_limits(request, email, "resend")
        with store.connection(write=True) as db:
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            encoded = row["password_hash"] if row else DUMMY_PASSWORD_HASH
            matches = password_matches(body.password, encoded)
            if row and matches and not row["verified"]:
                # 正在投递的任务不自动重复；未知投递由显式重发生成新记录。
                active = db.execute("SELECT 1 FROM jobs WHERE user_id=? AND kind='verification_mail' AND state IN ('queued','running')", (row["id"],)).fetchone()
                if not active:
                    queue_verification(db, row["id"], email)
        return {"message": "如账号信息正确且需要验证，新的验证邮件将在队列处理后发送。"}

    @app.post(PREFIX + "/auth/verify", dependencies=[Depends(check_origin)])
    def verify(body: Verify):
        with store.connection(write=True) as db:
            row = db.execute("SELECT * FROM verifications WHERE token_hash=? AND expires>?", (digest(body.token), time.time())).fetchone()
            if row is None or is_debug(db, row['user_id']):
                error(400, "验证链接无效或已过期。")
            db.execute("UPDATE users SET verified=1 WHERE id=?", (row["user_id"],))
            literature.grant(db, row["user_id"])
            db.execute("DELETE FROM verifications WHERE user_id=?", (row["user_id"],))
            # 邮件执行后令牌不再需要持久保存在队列载荷中。
            db.execute("UPDATE jobs SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,payload='{}' WHERE user_id=? AND kind='verification_mail' AND state!='running'", (row["user_id"],))
        return {"message": "邮箱验证成功，请登录。"}

    @app.post(PREFIX + "/auth/login", dependencies=[Depends(check_origin)])
    def login(body: Credentials, request: Request, response: Response):
        email = body.email.casefold()
        auth_limits(request, email, "login")
        with store.connection(write=True) as db:
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            # 不存在的账号同样执行一次慢散列，避免简单时间枚举。
            encoded = row["password_hash"] if row else DUMMY_PASSWORD_HASH
            if not password_matches(body.password, encoded) or row is None or not row["verified"]:
                error(401, "邮箱、密码有误或邮箱尚未验证。")
            token, csrf = new_token(), new_token()
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (digest(token), row["id"], csrf, time.time() + 86400))
        response.set_cookie(COOKIE, token, max_age=86400, path="/highscore/", httponly=True, secure=config["secure_cookie"], samesite="strict")
        return {"message": "登录成功。"}

    @app.get(PREFIX + "/auth/me")
    def me(user=Depends(session)):
        with store.connection(write=True) as db:
            quota = quotas(db, user)
            literature.maintain(db)
            literature.grant(db,user["id"])
            credits = literature.wallet(db,user["id"])
        return {"id": user["id"], "email": user["email"], "verified": bool(user["verified"]),
                "is_admin": bool(user["is_admin"]), "csrf_token": user["csrf"], "quotas": quota,
                "credits": {key:credits[key] for key in ("available","frozen","checked_in")}}

    @app.post(PREFIX + "/auth/logout")
    def logout(request: Request, response: Response, user=Depends(writer)):
        with store.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(request.cookies.get(COOKIE, "")),))
        response.delete_cookie(COOKIE, path="/highscore/")
        return {"message": "已退出登录。"}

    @app.get(PREFIX + "/databases")
    def databases():
        with store.connection() as db:
            online={row['database_id'] for row in db.execute(
                "SELECT database_id FROM workers WHERE last_seen>? AND capabilities LIKE '%\"search\"%'", (time.time()-90,))}
        return {'items':[{**item,'online':item['id'] in online} for item in catalog], 'default_id':default_database_id,
                'selection':'automatic' if automatic_database_id else 'configured'}

    @app.post(PREFIX + "/jobs/search")
    def search(body: Search, user=Depends(writer)):
        query = body.query.strip()
        code_separators = '\r\n\t' if body.search_version == 2 and body.mode == 'codes' else ''
        if not query or any((ord(char) < 32 and char not in code_separators)
                or (body.search_version == 2 and 127 <= ord(char) < 160) for char in query):
            error(422, "检索内容不能为空或包含控制字符。")
        if body.search_version == 2:
            if body.mode not in ('codes', 'formula'):
                error(422, '请选择编号或化学式检索。')
            if body.mode == 'codes':
                try:
                    parse_codes(query)
                except ValueError as exc:
                    error(422, str(exc))
            elif len(body.query) > 200:
                error(422, '化学式不能超过200字。')
        elif body.mode == 'codes' or len(body.query) > 200:
            error(422, '检索方式或内容长度无效。')
        payload={"mode":body.mode,"query":query}
        if body.content!='diffraction':payload['content']=body.content
        if body.search_version is not None:payload['search_version']=body.search_version
        with store.connection(write=True) as db:
            return enqueue(db, user, "search", payload, body.request_key,
                           database_id=resolve_database(body.database_id), searches=1)

    @app.post(PREFIX + "/jobs/archive")
    def archive(body: Archive, user=Depends(writer)):
        has_codes = bool(body.codes and body.codes.strip())
        has_candidates = body.search_job_id is not None or body.candidate_ids is not None
        if has_codes == has_candidates:
            error(422, "请单独提供卡片编号，或选择同一个搜索任务的候选。")
        with store.connection(write=True) as db:
            database_id = None
            selected = None
            if has_candidates:
                search_job = own(db, body.search_job_id, user)
                if search_job["kind"] != "search" or search_job["state"] != "succeeded" or not body.candidate_ids:
                    error(409, "搜索尚未完成或没有选择候选。")
                ids = list(dict.fromkeys(body.candidate_ids))
                if not 1 <= len(ids) <= 20:
                    error(422, "每批请选择 1 至 20 张卡片。")
                selected = []
                for candidate_id in ids:
                    candidate = db.execute("SELECT * FROM candidates WHERE id=? AND job_id=? AND user_id=?", (candidate_id, search_job["id"], user["id"])).fetchone()
                    if not candidate or candidate["database_id"] != search_job["database_id"]:
                        error(404, "候选不存在或不属于当前账号。")
                    selected.append({"code": candidate["code"], "source": candidate["source"], "candidate_id": candidate_id})
                if len({item["code"] for item in selected}) != len(selected):
                    error(409, "所选候选存在同编号来源歧义，请联系管理员核查。")
                database_id = search_job["database_id"]
                if body.database_id is not None and body.database_id != database_id:
                    error(422,'选卡归档必须沿用原检索任务的数据库。')
                codes = " ".join(item["code"] for item in selected)
                count = len(selected)
            else:
                database_id = resolve_database(body.database_id)
                try:
                    parsed_codes = parse_codes(body.codes)
                except ValueError as exc:
                    error(422, str(exc))
                codes = " ".join(item.query for item in parsed_codes)
                count = len(parsed_codes)
            payload = {"codes": codes, "send_mail": body.send_mail}
            if selected:
                payload.update(selected=selected, search_job_id=body.search_job_id)
                if json.loads(search_job['payload']).get('search_version') == 2:
                    payload['search_version'] = 2
            return enqueue(db, user, "archive", payload, body.request_key, database_id=database_id, cards=count)

    @app.get(PREFIX + "/jobs")
    def jobs(user=Depends(session)):
        with store.connection(write=True) as db:
            store.expire(db)
            rows = db.execute("SELECT * FROM jobs WHERE user_id=? AND kind!='verification_mail' ORDER BY created_at DESC LIMIT 200", (user["id"],)).fetchall()
            return {"items": [view(db, row) for row in rows]}

    @app.get(PREFIX + "/jobs/{job_id}")
    def job(job_id: str, user=Depends(session)):
        with store.connection(write=True) as db:
            store.expire(db)
            return view(db, own(db, job_id, user))

    @app.get(PREFIX + '/jobs/{job_id}/candidates')
    def job_candidates(job_id: str, offset: int = Query(default=0, ge=0, le=2147483647),
                       limit: int = Query(default=50, ge=1, le=50), user=Depends(session)):
        with store.connection() as db:
            row = own(db, job_id, user)
            if row['kind'] != 'search' or row['state'] != 'succeeded':
                error(409, '检索尚未完成，暂时没有可读取的候选快照。')
            return candidate_page(db, row, offset, limit)

    @app.post(PREFIX + "/jobs/{job_id}/cancel")
    def cancel(job_id: str, user=Depends(writer)):
        with store.connection(write=True) as db:
            row = own(db, job_id, user)
            if row["state"] == "queued":
                db.execute("UPDATE jobs SET state='cancelled',message='已取消尚未执行的任务',updated_at=? WHERE id=?", (time.time(), job_id))
                Credits.settle_xrd(db,job_id,0)
            elif row["state"] == "running":
                db.execute("UPDATE jobs SET cancel_requested=1,message='已请求取消，等待执行器在安全边界确认',updated_at=? WHERE id=?", (time.time(), job_id))
            return view(db, own(db, job_id, user))

    @app.post(PREFIX + "/jobs/{job_id}/retry")
    def retry(job_id: str, body: Retry, user=Depends(writer)):
        with store.connection(write=True) as db:
            store.expire(db)
            row = own(db, job_id, user)
            if row["state"] not in ("failed", "cancelled", "needs_attention"):
                error(409, "当前任务不能重试。")
            if row["mail_status"] in ("submitted", "unknown") or row["kind"] == "mail_retry":
                error(409, "该任务可能已投递，请使用明确的邮件重发操作。")
            if row["kind"] == "archive":
                history = delivery_history(db, row)
                if row["archive_status"] in ("success", "partial") or any(item["mail_status"] in ("submitted", "unknown") or item["state"] in ("queued", "running") and item["id"] != row["id"] for item in history):
                    error(409, "已存在归档或关联投递记录，请使用邮件重发并核查最近投递状态。")
            payload = json.loads(row["payload"])
            payload["retry_of"] = row["id"]
            return enqueue(db, user, row["kind"], payload, body.request_key, database_id=row["database_id"],
                           searches=int(row["kind"] == "search"), cards=len(parse_codes(payload["codes"])) if row["kind"] == "archive" else 0)

    @app.post(PREFIX + "/jobs/{job_id}/mail-retry")
    def mail_retry(job_id: str, body: Retry, user=Depends(writer)):
        with store.connection(write=True) as db:
            store.expire(db)
            row = own(db, job_id, user)
            if row["kind"] != "archive" or row["archive_status"] not in ("success", "partial") or not row["artifact_dir"]:
                error(409, "仅通过文件复验的完整卡片可以发往已验证邮箱。")
            history = delivery_history(db, row)
            in_flight = [item for item in history if item["state"] in ("queued", "running")]
            existing = db.execute("SELECT * FROM jobs WHERE user_id=? AND request_key=?", (user["id"], body.request_key)).fetchone()
            if in_flight and not (len(in_flight) == 1 and existing and in_flight[0]["id"] == existing["id"]):
                error(409, "已有相关邮件等待或正在投递，请先等待结果。")
            uncertain = row["mail_status"] == "unknown" or any(item["mail_status"] == "unknown" for item in history)
            if uncertain and not body.confirm_unknown:
                error(409, "历史投递状态未知，可能已送达；明确确认后才能创建新投递。")
            try:
                checked_zip(root, row["artifact_dir"], expected(row))
            except ArchiveError:
                error(409, "归档重新核验失败，不能发送邮件。")
            payload = {"original_job_id": row["id"], "codes": " ".join(item["code"] for item in expected(row)),
                       "send_mail": True, "confirm_unknown": body.confirm_unknown, "force_new_delivery": True}
            return enqueue(db, user, "mail_retry", payload, body.request_key, database_id=row["database_id"])

    @app.get(PREFIX + "/jobs/{job_id}/download")
    def download(job_id: str, user=Depends(session)):
        upgrades = []
        with store.connection() as db:
            row = own(db, job_id, user)
            if row["archive_status"] not in ("success", "partial") or not row["artifact_dir"]:
                error(409, "该任务尚无已核验的完整归档。")
            try:
                from .source_files import download_zip
                artifact_dir = row['artifact_dir']
                content = download_zip(root, artifact_dir, expected(row), upgrades=upgrades, job_id=job_id)
            except ArchiveError:
                error(409, "归档读回核验失败，已停止下载。")
        if upgrades:
            detail = json.dumps({'job_id': job_id, 'cards': upgrades}, ensure_ascii=False, sort_keys=True)
            # 生成ZIP时不持有写锁；审计同内容幂等，既有归档、邮件和积分均不改。
            with store.connection(write=True) as db:
                current = own(db, job_id, user)
                if current['artifact_dir'] != artifact_dir or current['archive_status'] not in ('success', 'partial'):
                    error(409, '归档状态已变化，请重新下载。')
                if not db.execute("SELECT 1 FROM audit WHERE user_id=? AND action='xrd_download_report_upgraded' AND detail=?",
                                  (user['id'], detail)).fetchone():
                    db.execute('INSERT INTO audit(user_id,action,detail,created_at) VALUES (?,?,?,?)',
                               (user['id'], 'xrd_download_report_upgraded', detail, time.time()))
        return Response(content, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="highscore-{job_id}.zip"'})

    @app.get(PREFIX + "/jobs/{job_id}/native-download")
    def download_native(job_id: str, code: str, user=Depends(session)):
        with store.connection() as db:
            row=own(db,job_id,user)
            if row['archive_status'] not in ('success','partial') or not row['artifact_dir']:
                error(409,'该任务尚无已核验的归档。')
            try:
                from .source_files import selected_native
                content=selected_native(root,row['artifact_dir'],expected(row),code)
            except ArchiveError:error(409,'原生附件复验失败，已停止下载。')
        if content is None:error(404,'该卡片没有已核验的HighScore原生附件。')
        return Response(content,media_type='application/rtf',headers={
            'Content-Disposition':f'attachment; filename="{code}-HighScore-original.rtf"'})

    @app.get(PREFIX + "/jobs/{job_id}/cif-download")
    def download_cif(job_id: str, code: str, user=Depends(session)):
        with store.connection() as db:
            row=own(db,job_id,user)
            if row['archive_status'] not in ('success','partial') or not row['artifact_dir']:
                error(409,'该任务尚无已核验的结构归档。')
            try:
                from .source_files import selected_cif
                content=selected_cif(root,row['artifact_dir'],expected(row),code)
            except ArchiveError:error(409,'CIF 文件复验失败，已停止下载。')
        if content is None:error(404,'该编号没有已核验的 CIF 文件。')
        return Response(content,media_type='chemical/x-cif',headers={
            'Content-Disposition':f'attachment; filename="{code}.cif"'})

    @app.get(PREFIX + "/admin/status")
    def admin_status(user=Depends(admin)):
        with store.connection(write=True) as db:
            store.expire(db)
            return {"settings": public_xrd_settings(db), "worker_online": database_worker_online(db),
                    "active_jobs": db.execute("SELECT COUNT(*) FROM jobs WHERE state='running'").fetchone()[0],
                    "queue_length": db.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0],
                    "lease_attention": db.execute("SELECT COUNT(*) FROM jobs WHERE state='needs_attention' AND lease_expires IS NOT NULL AND kind!='verification_mail'").fetchone()[0]}

    @app.patch(PREFIX + "/admin/settings")
    def admin_settings(body: Settings, user=Depends(admin_writer)):
        values = body.model_dump(exclude_none=True)
        for key in ("search_daily_limit", "archive_daily_limit"):
            if values.pop(key, 0) > 0:
                error(422, "XRD仅按共用积分结算，不设置每日检索或归档限额。")
        with store.connection(write=True) as db:
            for key, value in values.items():
                db.execute("UPDATE settings SET value=? WHERE key=?", (dumps(value), key))
            if values:
                db.execute("INSERT INTO audit(user_id,action,detail,created_at) VALUES (?,'settings',?,?)", (user["id"], dumps(values), time.time()))
            return {"settings": public_xrd_settings(db)}

    @app.post(PREFIX + "/worker/claim", dependencies=[Depends(worker)])
    def claim(body: Claim):
        with store.connection(write=True) as db:
            now = time.time()
            store.expire(db)
            db.execute("INSERT INTO workers VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET database_id=excluded.database_id,capabilities=excluded.capabilities,last_seen=excluded.last_seen",
                       (body.worker_id, body.database_id, dumps(body.capabilities), now))
            desktop_busy = db.execute("SELECT 1 FROM jobs WHERE kind!='verification_mail' AND (state='running' OR (state='needs_attention' AND lease_expires IS NOT NULL))").fetchone()
            flags = store.settings(db)
            allowed = all(flags[key] for key in ("license_confirmed", "cloud_validated", "public_enabled"))
            rows = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at").fetchall()
            row = None
            for candidate in rows:
                if candidate["kind"] not in body.capabilities:
                    continue
                if (is_debug(db, candidate['user_id']) and
                        (candidate['kind'] in ('verification_mail', 'mail_retry') or json.loads(candidate['payload']).get('send_mail'))):
                    continue
                if (candidate["kind"] != "verification_mail" and config["database_admin_only"]
                        and not db.execute("SELECT 1 FROM users WHERE id=? AND is_admin=1 AND verified=1", (candidate["user_id"],)).fetchone()):
                    continue
                if candidate["kind"] == "verification_mail":
                    # 验证邮件走独立队列，不受数据库许可和执行任务互斥限制。
                    if db.execute("SELECT 1 FROM jobs WHERE worker_id=? AND state='running' AND kind='verification_mail'", (body.worker_id,)).fetchone():
                        continue
                elif desktop_busy or not allowed or ((candidate["database_id"] or default_database_id)
                        and (candidate["database_id"] or default_database_id) != body.database_id):
                    continue
                row = candidate
                break
            if row is None:
                return {"job": None}
            token = new_token()
            db.execute("UPDATE jobs SET state='running',worker_id=?,lease_hash=?,lease_expires=?,database_id=COALESCE(database_id,?),updated_at=?,message='执行器已领取任务' WHERE id=?",
                       (body.worker_id, digest(token), now + LEASE_SECONDS, body.database_id, now, row["id"]))
            email = db.execute("SELECT email FROM users WHERE id=?", (row["user_id"],)).fetchone()[0]
            return {"job": {"id": row["id"], "kind": row["kind"], "user_id": row["user_id"], "payload": json.loads(row["payload"]),
                            "email": email, "database_id": row["database_id"] or body.database_id},
                    "lease_token": token, "lease_seconds": LEASE_SECONDS}

    def leased(db, job_id, token):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if (not row or row["state"] != "running" or not row["lease_hash"]
                or not hmac.compare_digest(row["lease_hash"], digest(token)) or row["lease_expires"] < time.time()):
            error(409, "执行器租约无效或已过期，请停止当前操作。")
        return row

    @app.post(PREFIX + "/worker/jobs/{job_id}/heartbeat", dependencies=[Depends(worker)])
    def heartbeat(job_id: str, body: Heartbeat):
        with store.connection(write=True) as db:
            row = leased(db, job_id, body.lease_token)
            now = time.time()
            db.execute("UPDATE jobs SET lease_expires=?,progress=?,message=?,updated_at=? WHERE id=?",
                       (now + LEASE_SECONDS, row["progress"] if body.progress is None else body.progress,
                        row["message"] if body.message is None else body.message, now, job_id))
            db.execute("UPDATE workers SET last_seen=? WHERE id=?", (now, row["worker_id"]))
            return {"cancel_requested": bool(row["cancel_requested"])}

    @app.post(PREFIX + "/worker/jobs/{job_id}/complete", dependencies=[Depends(worker)])
    def complete(job_id: str, body: Complete):
        with store.connection(write=True) as db:
            row = leased(db, job_id, body.lease_token)
            result, kind = body.result, row["kind"]
            state, message = body.state, result.message or "任务处理结束"
            verified_count=0
            artifact_dir = row["artifact_dir"]
            artifact_expected = row["artifact_expected"]
            archive_status = result.archive_status or row["archive_status"]
            mail_status = result.mail_status or row["mail_status"]
            results = result.results or []
            if kind == "search":
                if body.artifact_zip_base64 or archive_status != "not_started" or mail_status != "not_requested":
                    error(422, "搜索任务不能提交归档或邮件结果。")
                candidates = result.candidates or []
                version_two = json.loads(row['payload']).get('search_version') == 2
                if not version_two and len(candidates) > 100:
                    error(422, '旧版检索候选不能超过100条。')
                if body.state == "succeeded":
                    if not candidates:
                        message = "未发现该卡片信息"
                    identities = set()
                    for item in candidates:
                        try:
                            code = parse_code(item.code)
                        except ValueError:
                            error(422, "候选编号无效。")
                        if code.canonical_hint != item.code or not item.phase_name.strip() or not item.source.strip():
                            error(422, "候选必须包含完整编号、物相名和数据库来源。")
                        identity = (item.code, item.source)
                        if identity in identities:
                            error(422, "候选列表包含重复身份。")
                        identities.add(identity)
                        candidate_id = uid()
                        db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?)", (candidate_id, job_id, row["user_id"], row["database_id"], item.code, item.phase_name, item.chemical_formula, item.source))
                        if version_two:
                            details = item.model_dump(exclude_unset=True, exclude={'code', 'phase_name', 'chemical_formula', 'source'})
                            if details:
                                db.execute('INSERT INTO candidate_details VALUES (?,?)', (candidate_id, dumps(details)))
            elif kind == "archive":
                if result.candidates:
                    error(422, "归档任务不能提交搜索候选。")
                if not json.loads(row["payload"]).get("send_mail") and mail_status != "not_requested":
                    error(422, "未请求邮件的任务不能提交投递状态。")
                if mail_status == "submitted" and not body.artifact_zip_base64:
                    error(422, "未核验成功的归档不能报告邮件已提交。")
                if archive_status == "partial" and body.state == "succeeded":
                    error(422, "部分归档必须保留失败、待处理或取消状态。")
                # 在归档落盘之前验证失败记录身份，拒绝客户端将无关编号混入结果。
                failures = failed_results(row, result.results, [])
                if body.artifact_zip_base64:
                    try:
                        artifact_dir, receipts = accept_zip(root, job_id, body.artifact_zip_base64, expected(row),
                                                            allow_subset=archive_status == "partial")
                    except ArchiveError:
                        error(422, "归档上传复验失败，请核对文件、编号、来源及峰表。")
                    archive_status = "partial" if archive_status == "partial" else "success"
                    artifact_expected = dumps([{"code": item["code"], "source": item.get("source")} for item in receipts])
                    # 对外只返回真实复验的身份与峰数，忽略执行器自述的可伪造文件路径。
                    results = public_results(root, artifact_dir, receipts) + failed_results(row, result.results, receipts)
                    verified_count=len(receipts)
                elif archive_status == "success" or body.state == "succeeded":
                    error(422, "完整归档成功必须上传并通过文件复验。")
                else:
                    results = failures
                if mail_status == "submitted" and archive_status not in ("success", "partial"):
                    error(422, "未核验成功的归档不能报告邮件已提交。")
            elif kind == "mail_retry":
                if body.artifact_zip_base64 or result.candidates or result.archive_status:
                    error(422, "重发任务不能覆盖原归档。")
                original = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=? AND kind='archive'", (json.loads(row["payload"])["original_job_id"], row["user_id"])).fetchone()
                try:
                    if not original or not original["artifact_dir"]:
                        raise ArchiveError("缺少原归档")
                    checked_zip(root, original["artifact_dir"], expected(original))
                except ArchiveError:
                    error(422, "原归档核验失败，不能报告邮件结果。")
            elif kind == "verification_mail":
                if body.artifact_zip_base64 or result.candidates or result.results or result.archive_status:
                    error(422, "验证邮件不能提交归档内容。")
                if body.state == "succeeded" and mail_status != "submitted":
                    error(422, "验证邮件成功必须取得服务器接受回执。")
            if kind in ("mail_retry", "verification_mail") and body.state == "succeeded" and mail_status != "submitted":
                error(422, "发信任务成功必须取得服务器接受回执。")
            if kind in ("archive", "mail_retry") and body.state == "cancelled" and mail_status == "pending":
                mail_status = "not_started"
            if kind=='archive':
                Credits.settle_xrd(db,job_id,verified_count)
                missing = [item["code"] for item in results if item.get("status") == "not_found"]
                if missing and state != "cancelled":
                    if len(missing) == len(results):
                        state, archive_status, message = "failed", "failed", "未发现该卡片信息"
                    else:
                        message += " 未发现该卡片信息：" + "、".join(missing) + "。"
                    credit = Credits.xrd_status(db,job_id)
                    refunded = credit["reserved"] - credit["charged"]
                    if refunded:
                        message += ("，" if len(missing) == len(results) else "未成功卡片") + f"已返还{refunded}积分。"
            now = time.time()
            db.execute("""UPDATE jobs SET state=?,archive_status=?,mail_status=?,message=?,results=?,artifact_dir=?,artifact_expected=?,
              lease_hash=NULL,lease_expires=NULL,updated_at=?,progress=? WHERE id=?""",
                       (state, archive_status, mail_status, message, dumps(results), artifact_dir, artifact_expected, now, 100 if state == "succeeded" else row["progress"], job_id))
            db.execute("UPDATE workers SET last_seen=? WHERE id=?", (now, row["worker_id"]))
            if kind == "verification_mail" and mail_status == "submitted":
                db.execute("UPDATE jobs SET payload='{}' WHERE id=?", (job_id,))
            return {"accepted": True}

    def literature_worker(request: Request):
        token = config["literature_worker_token"]
        given = request.headers.get("Authorization", "")
        if not isinstance(token,str) or len(token)<32 or not hmac.compare_digest(given.encode(),f"Bearer {token}".encode()):
            error(401,"文献执行器认证失败。")

    def literature_mail_worker(request: Request):
        token = config["literature_mail_worker_token"]
        given = request.headers.get("Authorization", "")
        if not isinstance(token,str) or len(token)<32 or not hmac.compare_digest(given.encode(),f"Bearer {token}".encode()):
            error(401,"文献邮件执行器认证失败。")

    from .literature_api import mount_literature
    literature = mount_literature(app,store,root,session,writer,admin,admin_writer,literature_worker,literature_mail_worker)
    def abtem_worker(request: Request):
        token = config['abtem_worker_token']
        given = request.headers.get('Authorization', '')
        if (not isinstance(token, str) or len(token) < 32
                or token in {config['worker_token'], config['literature_worker_token'], config['literature_mail_worker_token'], config['pymatgen_worker_token']}
                or not hmac.compare_digest(given.encode(), f'Bearer {token}'.encode())):
            error(401, '模拟执行器认证失败。')

    from .abtem_api import mount_abtem
    mount_abtem(app, store, root, session, writer, abtem_worker, enabled=config['abtem_enabled'])
    def pymatgen_worker(request: Request):
        token = config['pymatgen_worker_token']
        given = request.headers.get('Authorization', '')
        if (not isinstance(token, str) or len(token) < 32
                or token in {config['worker_token'], config['literature_worker_token'],
                             config['literature_mail_worker_token'], config['abtem_worker_token']}
                or not hmac.compare_digest(given.encode(), f'Bearer {token}'.encode())):
            error(401, '晶体工具执行器认证失败。')

    from .pymatgen_api import mount_pymatgen
    mount_pymatgen(app, store, root, session, writer, pymatgen_worker, enabled=config['pymatgen_enabled'])
    from .admin_center import mount_admin_center
    mount_admin_center(app,store,admin,admin_writer)
    from .analytics import mount_analytics
    mount_analytics(app, store, root, admin, origin, config['secure_cookie'])
    from .research_library import mount_research_library
    mount_research_library(app,store,root,session,admin,admin_writer,
        quota_bytes=config['research_library_quota_bytes'],
        min_free_bytes=config['research_library_min_free_bytes'])
    @app.get("/highscore", include_in_schema=False)
    @app.get("/highscore/", include_in_schema=False)
    def legacy_home(request: Request):
        query = "?" + request.url.query if request.url.query else ""
        return RedirectResponse("/" + query, status_code=308)

    dist = root / "web/dist"
    if dist.is_dir() and (dist / "index.html").is_file():
        @app.get("/", include_in_schema=False)
        def home():
            return FileResponse(dist / "index.html", media_type="text/html")

        app.mount("/highscore", StaticFiles(directory=dist, html=True), name="website")
    else:
        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def placeholder():
            return "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>科研助手</title><body><h1>科研助手</h1><p>网站资源尚未构建，请管理员执行前端构建命令。</p></body></html>"
    return app
