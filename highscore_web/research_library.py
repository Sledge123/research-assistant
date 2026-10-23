"""管理员维护、已验证用户访问的科研资料库。"""
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import unicodedata
import uuid
from urllib.parse import quote, urlsplit

from fastapi import Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .database import dumps

BASE = "/highscore/api"
MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_LIBRARY_BYTES = 1536 * 1024 * 1024
DEFAULT_MIN_FREE_BYTES = 1024 * 1024 * 1024
STATUSES = ("draft", "published", "archived", "deleting")
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

FILE_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv; charset=utf-8",
    ".tsv": "text/tab-separated-values; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".rtf": "application/rtf",
    ".cif": "chemical/x-cif; charset=utf-8",
    ".pdb": "chemical/x-pdb; charset=utf-8",
    ".ent": "chemical/x-pdb; charset=utf-8",
    ".xyz": "chemical/x-xyz; charset=utf-8",
    ".vasp": "application/octet-stream",
    ".poscar": "application/octet-stream",
    ".mrc": "application/octet-stream",
    ".dm3": "application/octet-stream",
    ".dm4": "application/octet-stream",
    ".h5": "application/octet-stream",
    ".hdf5": "application/octet-stream",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
TEXT_TYPES = {".csv", ".tsv", ".txt", ".md", ".rtf", ".cif", ".pdb", ".ent", ".xyz", ".vasp", ".poscar"}
ZIP_TYPES = {".docx", ".pptx", ".xlsx", ".zip"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS research_library_items (
 id TEXT PRIMARY KEY CHECK(length(id)=32),
 title TEXT NOT NULL,
 description TEXT NOT NULL,
 category TEXT NOT NULL,
 license TEXT NOT NULL,
 source_url TEXT NOT NULL,
 filename TEXT NOT NULL,
 extension TEXT NOT NULL,
 media_type TEXT NOT NULL,
 size_bytes INTEGER NOT NULL CHECK(size_bytes>0),
 sha256 TEXT NOT NULL CHECK(length(sha256)=64),
 status TEXT NOT NULL CHECK(status IN ('draft','published','archived','deleting')),
 revision INTEGER NOT NULL CHECK(revision>0),
 created_by TEXT NOT NULL REFERENCES users(id),
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 published_at REAL
);
CREATE INDEX IF NOT EXISTS research_library_public ON research_library_items(status,category,created_at);
CREATE INDEX IF NOT EXISTS research_library_sha ON research_library_items(sha256);
"""


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _clean_text(value, *, multiline=False, required=False):
    allowed = "\r\n\t" if multiline else ""
    if (required and not value.strip()) or any(
        unicodedata.category(char) in ("Cc", "Cs") and char not in allowed for char in value
    ):
        raise ValueError("文字内容为空或包含不支持的字符。")
    return value.strip()


def _source_url(value):
    value = _clean_text(value)
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme.lower() == "https" and bool(parsed.hostname) and parsed.username is None and parsed.password is None
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("来源链接须为有效的 HTTPS 地址。")
    return value


class LibraryCreate(Model):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    category: str = Field(min_length=1, max_length=60)
    license: str = Field(default="", max_length=160)
    source_url: str = Field(default="", max_length=512)
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=4, max_length=28_000_000)
    status: str = "draft"

    @field_validator("title", "category")
    @classmethod
    def required_text(cls, value):
        return _clean_text(value, required=True)

    @field_validator("description")
    @classmethod
    def description_text(cls, value):
        return _clean_text(value, multiline=True)

    @field_validator("license")
    @classmethod
    def license_text(cls, value):
        return _clean_text(value, multiline=True)

    @field_validator("source_url")
    @classmethod
    def source_text(cls, value):
        return _source_url(value)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value):
        value = _clean_text(value, required=True)
        if any(char in value for char in ("/", "\\", "\u0000")) or value in (".", ".."):
            raise ValueError("文件名不能包含路径。")
        if Path(value).suffix.lower() not in FILE_TYPES:
            raise ValueError("此文件格式暂不支持。")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value not in ("draft", "published"):
            raise ValueError("新资料只能保存为草稿或发布。")
        return value


class LibraryUpdate(Model):
    revision: int = Field(ge=1, le=2_147_483_647)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    category: str = Field(min_length=1, max_length=60)
    license: str = Field(default="", max_length=160)
    source_url: str = Field(default="", max_length=512)
    status: str

    @field_validator("title", "category")
    @classmethod
    def required_text(cls, value):
        return _clean_text(value, required=True)

    @field_validator("description", "license")
    @classmethod
    def multiline_text(cls, value):
        return _clean_text(value, multiline=True)

    @field_validator("source_url")
    @classmethod
    def source_text(cls, value):
        return _source_url(value)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value not in ("draft", "published", "archived"):
            raise ValueError("资料状态无效。")
        return value


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _decode_upload(body):
    encoded = body.content_base64
    if len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
        raise HTTPException(413, "单个文件不能超过 20 MiB。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "上传文件内容无效。") from None
    if not data or len(data) > MAX_FILE_BYTES:
        raise HTTPException(413 if data else 422, "单个文件须大于 0 且不能超过 20 MiB。")
    extension = Path(body.filename).suffix.lower()
    if extension in TEXT_TYPES:
        try:
            decoded = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(422, "文本类资料须使用 UTF-8 编码。") from None
        if "\x00" in decoded:
            raise HTTPException(422, "文本类资料内容无效。")
    elif extension == ".pdf":
        if b"%PDF-" not in data[:1024]:
            raise HTTPException(422, "文件内容与 PDF 格式不符。")
    elif extension in ZIP_TYPES:
        if not data.startswith(b"PK\x03\x04"):
            raise HTTPException(422, "文件内容与所选格式不符。")
    elif extension == ".png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(422, "文件内容与 PNG 格式不符。")
    elif extension in (".jpg", ".jpeg") and not data.startswith(b"\xff\xd8\xff"):
        raise HTTPException(422, "文件内容与 JPEG 格式不符。")
    return data, extension


class ResearchLibraryStore:
    def __init__(self, root, database, *, quota_bytes=DEFAULT_LIBRARY_BYTES, min_free_bytes=DEFAULT_MIN_FREE_BYTES):
        self.project_root = Path(root).resolve()
        self.database = database
        self.quota_bytes = int(quota_bytes)
        self.min_free_bytes = int(min_free_bytes)
        if self.quota_bytes <= 0 or self.min_free_bytes < 0:
            raise ValueError("资料库容量和磁盘余量设置无效。")
        self.root = self.project_root / "output" / "research-library"
        self.files = self.root / "files"
        self.temporary = self.root / "temporary"
        self._ensure_directory(self.project_root / "output")
        self._ensure_directory(self.root)
        self._ensure_directory(self.files)
        self._ensure_directory(self.temporary)

    def _ensure_directory(self, path):
        if path.is_symlink():
            raise ValueError("资料库目录不能是符号链接。")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = path.resolve()
        if not resolved.is_relative_to(self.project_root):
            raise ValueError("资料库目录必须留在项目目录内。")
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    def _storage_used(self):
        total = 0
        for directory in (self.files, self.temporary):
            if directory.is_symlink() or not directory.is_dir():
                raise HTTPException(500, "资料库存储状态异常。")
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_file():
                    raise HTTPException(500, "资料库存储状态异常。")
                total += path.stat().st_size
        return total

    def storage_status(self):
        free_bytes = shutil.disk_usage(self.root).free
        used_bytes = self._storage_used()
        quota_remaining = max(0, self.quota_bytes - used_bytes)
        disk_remaining = max(0, free_bytes - self.min_free_bytes)
        return {
            "used_bytes": used_bytes,
            "quota_bytes": self.quota_bytes,
            "available_bytes": min(quota_remaining, disk_remaining),
            "max_file_bytes": MAX_FILE_BYTES,
            "server_free_bytes": free_bytes,
            "minimum_free_bytes": self.min_free_bytes,
        }

    def file_path(self, item_id):
        if not ID_PATTERN.fullmatch(item_id):
            raise HTTPException(404, "资料不存在。")
        path = self.files / (item_id + ".bin")
        if path.is_symlink() or not path.resolve().is_relative_to(self.files.resolve()):
            raise HTTPException(409, "资料文件校验失败。")
        return path

    def file_available(self, row):
        path = self.file_path(row["id"])
        return path.is_file() and path.stat().st_size == row["size_bytes"]

    def checked_bytes(self, row):
        path = self.file_path(row["id"])
        if not path.is_file():
            raise HTTPException(409, "资料文件暂不可用，请联系管理员。")
        data = path.read_bytes()
        if len(data) != row["size_bytes"] or _digest(data) != row["sha256"]:
            raise HTTPException(409, "资料文件校验未通过，请联系管理员。")
        return data

    @staticmethod
    def _admin(db, actor):
        row = db.execute("SELECT id FROM users WHERE id=? AND verified=1 AND is_admin=1", (actor["id"],)).fetchone()
        if row is None:
            raise HTTPException(403, "仅已验证管理员可执行此操作。")

    @staticmethod
    def _audit(db, actor, action, detail):
        db.execute("INSERT INTO audit(user_id,action,detail,created_at) VALUES (?,?,?,?)",
                   (actor["id"], action, dumps(detail), time.time()))

    @staticmethod
    def _view(row, *, admin=False, available=False):
        result = {key: row[key] for key in (
            "id", "title", "description", "category", "license", "source_url", "filename",
            "extension", "size_bytes", "created_at", "updated_at", "published_at", "revision",
        )}
        result["available"] = available
        if admin:
            result.update(status=row["status"], sha256=row["sha256"])
        else:
            result["status"] = "published"
        return result

    def list_items(self, db, *, admin, offset, limit, query, category, status=None):
        db.create_function("library_casefold", 1, lambda value: value.casefold() if isinstance(value, str) else "")
        where, args = [], []
        if not admin:
            where.append("status='published'")
        elif status:
            where.append("status=?")
            args.append(status)
        if category:
            where.append("category=?")
            args.append(category)
        normalized = query.strip().casefold()
        if normalized:
            where.append("instr(library_casefold(title || ' ' || description || ' ' || category || ' ' || filename),?)>0")
            args.append(normalized)
        clause = " WHERE " + " AND ".join(where) if where else ""
        total = db.execute("SELECT count(*) FROM research_library_items" + clause, args).fetchone()[0]
        rows = db.execute("SELECT * FROM research_library_items" + clause +
            " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        category_where = "status!='deleting'" if admin else "status='published'"
        categories = [row[0] for row in db.execute(
            "SELECT DISTINCT category FROM research_library_items WHERE " + category_where + " ORDER BY category COLLATE NOCASE")]
        return {
            "items": [self._view(row, admin=admin, available=self.file_available(row)) for row in rows],
            "total": total,
            "categories": categories,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        }

    def create(self, actor, body):
        data, extension = _decode_upload(body)
        if body.status == "published" and not body.license:
            raise HTTPException(422, "发布资料前请填写许可或权属说明。")
        identifier = uuid.uuid4().hex
        path = self.file_path(identifier)
        temporary = self.temporary / (identifier + ".part")
        now = time.time()
        digest = _digest(data)
        try:
            with self.database.connection(write=True) as db:
                self._admin(db, actor)
                status = self.storage_status()
                if status["used_bytes"] + len(data) > self.quota_bytes:
                    raise HTTPException(507, "资料库容量已满，请先删除资料或提高容量。")
                if status["server_free_bytes"] - len(data) < self.min_free_bytes:
                    raise HTTPException(507, "服务器剩余空间已到安全水位，暂不能上传。")
                if db.execute("SELECT 1 FROM research_library_items WHERE sha256=?", (digest,)).fetchone():
                    raise HTTPException(409, "相同内容已在资料库中。")
                with temporary.open("xb") as stream:
                    os.chmod(temporary, 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                media_type = FILE_TYPES[extension]
                db.execute("""INSERT INTO research_library_items
                    (id,title,description,category,license,source_url,filename,extension,media_type,size_bytes,sha256,
                     status,revision,created_by,created_at,updated_at,published_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                    (identifier, body.title, body.description, body.category, body.license, body.source_url,
                     body.filename, extension, media_type, len(data), digest, body.status, actor["id"], now, now,
                     now if body.status == "published" else None))
                self._audit(db, actor, "research_library_create", {
                    "item_id": identifier, "title": body.title, "category": body.category,
                    "status": body.status, "filename": body.filename, "size_bytes": len(data), "sha256": digest,
                })
                row = db.execute("SELECT * FROM research_library_items WHERE id=?", (identifier,)).fetchone()
            return self._view(row, admin=True, available=True), self.storage_status()
        except BaseException:
            for leftover in (temporary, path):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def update(self, actor, item_id, body):
        if body.status == "published" and not body.license:
            raise HTTPException(422, "发布资料前请填写许可或权属说明。")
        now = time.time()
        with self.database.connection(write=True) as db:
            self._admin(db, actor)
            row = db.execute("SELECT * FROM research_library_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "资料不存在。")
            if row["status"] == "deleting":
                raise HTTPException(409, "资料正在删除，不能修改。")
            if row["revision"] != body.revision:
                raise HTTPException(409, "资料已被其他管理员修改，请刷新后重试。")
            published_at = now if body.status == "published" else row["published_at"]
            db.execute("""UPDATE research_library_items SET title=?,description=?,category=?,license=?,source_url=?,
                status=?,revision=revision+1,updated_at=?,published_at=? WHERE id=?""",
                (body.title, body.description, body.category, body.license, body.source_url, body.status,
                 now, published_at, item_id))
            self._audit(db, actor, "research_library_update", {
                "item_id": item_id, "title": body.title, "category": body.category,
                "status": body.status, "revision": body.revision + 1,
            })
            updated = db.execute("SELECT * FROM research_library_items WHERE id=?", (item_id,)).fetchone()
            return self._view(updated, admin=True, available=self.file_available(updated))

    def delete(self, actor, item_id):
        path = self.file_path(item_id)
        with self.database.connection(write=True) as db:
            self._admin(db, actor)
            row = db.execute("SELECT * FROM research_library_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "资料不存在。")
            if row["status"] != "deleting":
                if path.exists() and not path.is_file():
                    raise HTTPException(409, "资料文件路径状态异常，已停止删除。")
                actual_hash = _digest(path.read_bytes()) if path.is_file() else None
                now = time.time()
                db.execute("UPDATE research_library_items SET status='deleting',revision=revision+1,updated_at=? WHERE id=?",
                           (now, item_id))
                self._audit(db, actor, "research_library_delete", {
                    "item_id": item_id, "title": row["title"], "size_bytes": row["size_bytes"],
                    "sha256": row["sha256"], "actual_sha256": actual_hash,
                    "integrity_verified": actual_hash == row["sha256"],
                })
        if path.is_symlink():
            raise HTTPException(409, "资料文件路径状态异常，已停止删除。")
        if path.exists():
            if not path.is_file():
                raise HTTPException(409, "资料文件路径状态异常，已停止删除。")
            path.unlink()
        with self.database.connection(write=True) as db:
            self._admin(db, actor)
            db.execute("DELETE FROM research_library_items WHERE id=? AND status='deleting'", (item_id,))
        return {"deleted": True, "id": item_id}

    def download(self, item_id, *, admin=False):
        if not ID_PATTERN.fullmatch(item_id):
            raise HTTPException(404, "资料不存在。")
        with self.database.connection() as db:
            if admin:
                row = db.execute("SELECT * FROM research_library_items WHERE id=? AND status!='deleting'", (item_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM research_library_items WHERE id=? AND status='published'", (item_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "资料不存在。")
            data = self.checked_bytes(row)
        fallback = re.sub(r"[^A-Za-z0-9._-]", "_", row["filename"]).strip("._") or "research-material"
        headers = {
            "Content-Disposition": f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(row["filename"], safe="")}',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
        return Response(data, media_type=row["media_type"], headers=headers)


def mount_research_library(app, database, root, session, admin, admin_writer, *, quota_bytes=DEFAULT_LIBRARY_BYTES,
                           min_free_bytes=DEFAULT_MIN_FREE_BYTES):
    store = ResearchLibraryStore(root, database, quota_bytes=quota_bytes, min_free_bytes=min_free_bytes)
    app.state.research_library = store

    @app.get(BASE + "/library")
    def public_library(q: str = Query(default="", max_length=120), category: str = Query(default="", max_length=60),
                       offset: int = Query(default=0, ge=0, le=2_147_483_647),
                       limit: int = Query(default=20, ge=1, le=100), user=Depends(session)):
        with database.connection() as db:
            return store.list_items(db, admin=False, offset=offset, limit=limit, query=q, category=category)

    @app.get(BASE + "/library/{item_id}/download")
    def public_download(item_id: str, user=Depends(session)):
        return store.download(item_id, admin=False)

    @app.get(BASE + "/admin/library")
    def admin_library(q: str = Query(default="", max_length=120), category: str = Query(default="", max_length=60),
                      status: str | None = Query(default=None, pattern="^(draft|published|archived|deleting)$"),
                      offset: int = Query(default=0, ge=0, le=2_147_483_647),
                      limit: int = Query(default=20, ge=1, le=100), actor=Depends(admin)):
        with database.connection() as db:
            ResearchLibraryStore._admin(db, actor)
            result = store.list_items(db, admin=True, offset=offset, limit=limit, query=q, category=category, status=status)
            result["storage"] = store.storage_status()
            return result

    @app.get(BASE + "/admin/library/{item_id}/download")
    def admin_download(item_id: str, actor=Depends(admin)):
        return store.download(item_id, admin=True)

    @app.post(BASE + "/admin/library")
    def create_library_item(body: LibraryCreate, actor=Depends(admin_writer)):
        item, storage = store.create(actor, body)
        return {"item": item, "storage": storage}

    @app.patch(BASE + "/admin/library/{item_id}")
    def update_library_item(item_id: str, body: LibraryUpdate, actor=Depends(admin_writer)):
        return {"item": store.update(actor, item_id, body)}

    @app.delete(BASE + "/admin/library/{item_id}")
    def delete_library_item(item_id: str, actor=Depends(admin_writer)):
        return store.delete(actor, item_id)
