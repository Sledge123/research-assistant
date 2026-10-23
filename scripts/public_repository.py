"""公开发行的内容检查和受清单保护的增量写入；不连接任何服务。"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile


MANIFEST = ".public-source-manifest.json"
FORBIDDEN_PARTS = {
    "runtime", "runtime-cache", "output", "logs", "node_modules", "__pycache__",
    ".venv", ".cache", ".npm-cache", "private-databases", ".git",
}
FORBIDDEN_SUFFIXES = {".pyc", ".hsrdb", ".sqlite", ".sqlite3", ".db", ".key", ".pem",
                      ".pfx", ".p12", ".pdf", ".zip", ".whl", ".eml", ".enc"}
CONTENT_RULES = (
    ("GitHub凭据", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("私钥", re.compile(rb"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----")),
    ("生产网址", re.compile(rb"api[.]sledge[.]cn", re.I)),
    ("本机个人路径", re.compile(rb"(?:C:[/\\]+Users[/\\]+A[/\\]|D:[/\\]+ht-codex)", re.I)),
    ("真实字节字面量", re.compile(rb"bytes[.]fromhex\(['\"][0-9a-fA-F]{80,}['\"]\)")),
    ("长编码样本", re.compile(rb"['\"][A-Za-z0-9+/]{300,}={0,2}['\"]")),
)
SECRET_ASSIGNMENT = re.compile(
    r"""(?im)\b(?:smtp_password|mail_password|github_token|literature_worker_token|highscore_worker_token|password)\b["']?\s*[:=]\s*["']([A-Za-z0-9+/_=-]{24,})["']"""
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def relative_name(name):
    if not isinstance(name, str) or not name:
        raise ValueError("公开文件必须使用规范的项目内相对路径。")
    path = PurePosixPath(name)
    if ("\\" in name or ":" in name
            or path.is_absolute() or ".." in path.parts or str(path) != name):
        raise ValueError("公开文件必须使用规范的项目内相对路径。")
    return path


def path_findings(name):
    path = relative_name(name)
    failures = []
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        failures.append("运行数据或依赖目录")
    if path.suffix.lower() in FORBIDDEN_SUFFIXES or name.endswith(".local.json"):
        failures.append("本地配置、数据库、秘密或业务文件")
    if name.endswith((".dpapi.json", ".encrypted.json")) or name in {"runtime.env", ".env"}:
        failures.append("私有配置")
    if (path.name.startswith(".env") or path.name.endswith(".env")) and not name.endswith((".example", ".sample")):
        failures.append("私有环境配置")
    return failures


def content_findings(name, data):
    failures = [title for title, pattern in CONTENT_RULES if pattern.search(data)]
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        return failures + ["非UTF-8源码文件"]
    for found in SECRET_ASSIGNMENT.finditer(text):
        value = found.group(1).lower()
        if not value.startswith(("synthetic", "replace_", "example", "test_", "test-", "dummy")):
            failures.append("疑似硬编码凭据")
    return sorted(set(failures))


def scan_payload(payload):
    failures = []
    for name, data in sorted(payload.items()):
        issues = path_findings(name) + content_findings(name, data)
        if issues:
            failures.append({"path": name, "reasons": sorted(set(issues))})
    return failures


def atomic_write(path, data):
    """在同目录排他创建随机临时文件，关闭后再原子替换。"""
    temporary = None
    try:
        # NamedTemporaryFile以O_EXCL创建，不会跟随预先放置的临时链接。
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.public-',
                                         suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_repository(target, payload):
    """只覆盖上次生成且摘要未变化的文件；从不删除不受管理的文件。"""
    target = Path(target)
    if target.is_symlink():
        raise ValueError("公开副本不能是符号链接。")
    target = target.resolve()
    for name in payload:
        relative_name(name)
        path = target / name
        for parent in (path, *path.parents):
            if parent == target:
                break
            if parent.is_symlink():
                raise ValueError("公开副本包含符号链接。")
        if not path.resolve().is_relative_to(target):
            raise ValueError("公开文件超出目标目录。")
    manifest_path = target / MANIFEST
    old = {}
    if manifest_path.is_symlink():
        raise ValueError("清单不能是符号链接。")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("既有公开副本清单无效。")
        old = manifest["files"]
        for name, expected in old.items():
            relative_name(name)
            path = target / name
            if (not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected)
                    or path.is_symlink() or not path.resolve().is_relative_to(target)
                    or not path.is_file() or digest(path.read_bytes()) != expected):
                raise ValueError("公开副本存在人工修改或缺失，保留原状，请先处理：" + name)
    elif target.exists() and any(target.iterdir()):
        raise ValueError("拒绝覆盖没有发行清单的非空目录。")
    if target.exists():
        ignored = FORBIDDEN_PARTS | {"dist"}
        for path in target.rglob("*"):
            relative = path.relative_to(target)
            if any(part in ignored for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError("公开副本包含符号链接。")
            name = relative.as_posix()
            if path.is_file() and name != MANIFEST and name not in old:
                raise ValueError("公开副本存在未登记的人工文件，保留原状：" + name)
    target.mkdir(parents=True, exist_ok=True)
    for name, data in sorted(payload.items()):
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, data)
    for name in sorted(set(old) - set(payload)):
        # 已完成摘要及边界检查，仅移除上次导出生成的单个文件。
        (target / name).unlink()
    manifest = {"version": 1, "files": {name: digest(data) for name, data in sorted(payload.items())}}
    atomic_write(manifest_path,
                 (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return manifest
