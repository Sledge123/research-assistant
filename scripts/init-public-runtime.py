"""仅初始化本公开项目内的空目录与随机凭据；不联网、不覆盖、不输出秘密。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PureWindowsPath
import secrets
import sys
from urllib.parse import urlsplit


PROJECT = Path(__file__).resolve().parents[1]


class ChineseParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "命令行参数无效，请使用 --help 查看中文说明。\n")

    def format_help(self):
        return super().format_help().replace("usage:", "用法：", 1)


TOKEN_NAMES = (
    "HIGHSCORE_WORKER_TOKEN", "LITERATURE_WORKER_TOKEN", "LITERATURE_MAIL_WORKER_TOKEN",
    "HIGHSCORE_ABTEM_WORKER_TOKEN", "HIGHSCORE_PYMATGEN_WORKER_TOKEN",
)
DIRECTORIES = (
    "runtime", "runtime/web", "runtime/web/analytics", "runtime/literature-files",
    "runtime/research-library", "runtime/abtem-files", "runtime/pymatgen-files",
    "runtime/literature-credentials", "runtime/literature-worker", "runtime/database-worker",
    "runtime/verification-mail-worker", "runtime/literature-mail-worker",
    "runtime/abtem-worker", "runtime/pymatgen-worker", "runtime/science-lock",
    "runtime/verification-mail", "runtime/verification-mail/runtime",
    "runtime/literature-mail", "runtime/literature-mail/runtime",
    "runtime/database-mail", "runtime/database-mail/runtime", "runtime-cache",
    "runtime-cache/web/tmp", "runtime-cache/database/tmp", "runtime-cache/verification-mail/tmp",
    "runtime-cache/literature/tmp", "runtime-cache/literature-mail/tmp",
    "runtime-cache/abtem/tmp", "runtime-cache/pymatgen/tmp", "data/private-databases",
)


def safe_path(root: Path, relative: str) -> Path:
    value = Path(relative)
    if value.is_absolute() or value.anchor or PureWindowsPath(relative).anchor or ".." in relative.replace("\\", "/").split("/"):
        raise ValueError("路径必须在项目内。")
    target = root / value
    if not target.resolve().is_relative_to(root):
        raise ValueError("路径不能超出项目。")
    for part in (target, *target.parents):
        if part == root.parent:
            break
        if part.is_symlink():
            raise ValueError("初始化路径不允许符号链接。")
    return target


def write_missing(root: Path, relative: str, content: str) -> None:
    target = safe_path(root, relative)
    if target.exists():
        if not target.is_file():
            raise ValueError("已有配置路径不是文件。")
        return
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name in values:
            raise ValueError("已有配置格式无法校验；未替换。")
        values[name] = value
    return values


def write_role_env(root: Path, relative: str, values: dict[str, str]) -> None:
    target = safe_path(root, relative)
    if target.exists():
        if not target.is_file():
            raise ValueError("已有执行器配置不是文件。")
        current = read_env(target)
        if any(current.get(key) != value for key, value in values.items()):
            raise ValueError("已有执行器凭据与网站不一致；未自动替换。")
    write_missing(root, relative, "".join(f"{key}={value}\n" for key, value in values.items()))


def normalize_permissions(root: Path, uid: int, gid: int) -> None:
    if os.name != "posix":
        return
    if os.geteuid() != 0 and (os.geteuid() != uid or os.getegid() != gid):
        raise ValueError("Linux 请使用 sudo 运行本脚本，为容器 UID/GID 10001 准备私有权限。")
    paths: list[Path] = []
    for relative in ("runtime", "runtime-cache", "data/private-databases"):
        base = safe_path(root, relative)
        paths.append(base)
        for directory, names, filenames in os.walk(base, followlinks=False):
            for name in (*names, *filenames):
                target = Path(directory) / name
                if target.is_symlink() or not target.resolve().is_relative_to(root):
                    raise ValueError("运行目录存在不安全链接，未继续修正权限。")
                paths.append(target)
    for relative in ("database.local.json", "database-worker.local.json", "verification-mail-worker.local.json"):
        paths.append(safe_path(root, relative))
    for target in paths:
        os.chmod(target, 0o700 if target.is_dir() else 0o600)
        if os.geteuid() == 0:
            os.chown(target, uid, gid)


def initialize(root: Path, site_url: str, uid: int = 10001, gid: int = 10001) -> None:
    parsed = urlsplit(site_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path != "/" or parsed.query
            or parsed.fragment or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise ValueError("站点地址必须是 HTTPS 根网址，且不含账号、查询参数或非标准端口。")
    for relative in DIRECTORIES:
        safe_path(root, relative).mkdir(parents=True, exist_ok=True, mode=0o700)

    env_path = safe_path(root, "runtime/web.env")
    if env_path.exists():
        if not env_path.is_file():
            raise ValueError("已有网站配置路径不是文件。")
        existing = read_env(env_path)
        tokens = {name: existing.get(name, "") for name in TOKEN_NAMES}
        if (any(len(token) < 32 or not token.isascii() or any(ch.isspace() for ch in token)
                for token in tokens.values()) or len(set(tokens.values())) != len(tokens)):
            raise ValueError("已有网站令牌缺失或不能校验；未覆盖现有凭据。")
        public_url = existing.get("HIGHSCORE_PUBLIC_URL", "")
        if public_url != site_url + "highscore/":
            raise ValueError("已有站点地址与参数不一致；请使用原 --site-url，未覆盖现有配置。")
    else:
        tokens = {name: secrets.token_urlsafe(48) for name in TOKEN_NAMES}
        values = {
            "HIGHSCORE_PUBLIC_URL": site_url + "highscore/",
            "HIGHSCORE_COOKIE_SECURE": "true", "HIGHSCORE_DATABASE_ADMIN_ONLY": "false",
            "HIGHSCORE_DATABASE_CATALOG": "[]", "HIGHSCORE_DATABASE_SOURCE_TYPES": "[]",
            "HIGHSCORE_DATABASE_AUTO_ID": "", "HIGHSCORE_ABTEM_ENABLED": "false",
            "HIGHSCORE_PYMATGEN_ENABLED": "false",
            "HIGHSCORE_RESEARCH_LIBRARY_QUOTA_BYTES": "1610612736",
            "HIGHSCORE_RESEARCH_LIBRARY_MIN_FREE_BYTES": "1073741824",
            "HIGHSCORE_TRUSTED_PROXY_IPS": "127.0.0.1", **tokens,
        }
        write_missing(root, "runtime/web.env", "".join(f"{key}={value}\n" for key, value in values.items()))

    # 数据库和注册邮件沿用同一已存在 API 令牌组，配置/SMTP/输出仍完全隔离。
    write_role_env(root, "runtime/database.env", {"HIGHSCORE_WORKER_TOKEN": tokens["HIGHSCORE_WORKER_TOKEN"]})
    write_role_env(root, "runtime/verification-mail.env", {"HIGHSCORE_WORKER_TOKEN": tokens["HIGHSCORE_WORKER_TOKEN"]})
    write_role_env(root, "runtime/literature.env", {"LITERATURE_WORKER_TOKEN": tokens["LITERATURE_WORKER_TOKEN"]})
    write_role_env(root, "runtime/literature-mail.env",
                   {"LITERATURE_MAIL_WORKER_TOKEN": tokens["LITERATURE_MAIL_WORKER_TOKEN"], "LITERATURE_MAIL_SITE_URL": site_url})
    write_role_env(root, "runtime/abtem.env", {"HIGHSCORE_ABTEM_WORKER_TOKEN": tokens["HIGHSCORE_ABTEM_WORKER_TOKEN"]})
    write_role_env(root, "runtime/pymatgen.env", {"HIGHSCORE_PYMATGEN_WORKER_TOKEN": tokens["HIGHSCORE_PYMATGEN_WORKER_TOKEN"]})
    write_missing(root, "runtime/science-lock/compute.lock", "")

    database = {"databases": [{"path": "./data/private-databases/your-licensed-database.hsrdb"}]}
    worker = {"server_url": site_url + "highscore/api", "public_url": site_url + "highscore/",
              "worker_id": "public-database-1", "database_id": "configure-verified-database-id",
              "database_only": True, "desktop_enabled": False, "poll_seconds": 5}
    verification = {"server_url": site_url + "highscore/api", "public_url": site_url + "highscore/",
                    "worker_id": "public-verification-mail-1", "database_id": "verification-mail",
                    "mail_only": True, "desktop_enabled": False, "poll_seconds": 5}
    for name, value in (("database.local.json", database), ("database-worker.local.json", worker),
                        ("verification-mail-worker.local.json", verification)):
        write_missing(root, name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    normalize_permissions(root, uid, gid)


def main(argv: list[str] | None = None) -> int:
    parser = ChineseParser(description="初始化当前公开项目的私有运行目录；不联网或覆盖已有配置", add_help=False)
    parser._optionals.title = "选项"
    parser.add_argument("-h", "--help", action="help", help="显示中文说明并退出")
    parser.add_argument("--root", default=".", help="仅允许当前项目内相对路径，默认当前源码根目录")
    parser.add_argument("--site-url", default="https://example.com/", help="部署者自己的 HTTPS 根网址")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fix-permissions", action="store_true", help="重跑安全检查并修正 Linux 容器 UID/GID 10001 权限")
    mode.add_argument("--source", action="store_true", help="仅用于本机源码运行，Linux 目录归当前用户所有")
    args = parser.parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        relative = Path(args.root)
        if relative.is_absolute() or relative.anchor or PureWindowsPath(args.root).anchor or ".." in args.root.replace("\\", "/").split("/"):
            raise ValueError("根目录须为当前项目内的相对路径。")
        root = safe_path(PROJECT, args.root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.source and os.name == "posix":
            initialize(root, args.site_url, uid=os.geteuid(), gid=os.getegid())
        else:
            initialize(root, args.site_url)
        print("已初始化缺失配置和空运行目录；已有内容未覆盖，凭据未输出。")
        if os.name != "posix":
            print("Linux 部署前请以原 --site-url 再运行 sudo python3 ./scripts/init-public-runtime.py --fix-permissions。")
        return 0
    except (OSError, ValueError):
        print("初始化未完成：请核对项目内路径、原站点地址和已有配置；Linux 需 sudo 准备 UID/GID 10001。未输出秘密。", file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
