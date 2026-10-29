"""读取当前项目私有网站配置，以项目内 HTTP 环境从 Python 源码启动。"""
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


class ChineseParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "命令行参数无效，请使用 --help 查看中文说明。\n")

    def format_help(self):
        return super().format_help().replace("usage:", "用法：", 1)


def main(argv: list[str] | None = None) -> int:
    parser = ChineseParser(description="使用当前项目内 .venv-web 启动科研助手", add_help=False)
    parser._optionals.title = "选项"
    parser.add_argument("-h", "--help", action="help", help="显示中文说明并退出")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认回环地址")
    parser.add_argument("--port", type=int, default=8787, help="监听端口，默认 8787")
    parser.add_argument("--local-http", action="store_true", help="只允许回环地址，关闭本机 HTTP 的 Secure Cookie")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if not 1 <= args.port <= 65535:
            raise ValueError()
        if args.local_http and not ipaddress.ip_address(args.host).is_loopback:
            raise ValueError()
        config = ROOT / "runtime/web.env"
        if (not config.is_file() or config.is_symlink() or not config.resolve().is_relative_to(ROOT)
                or config.parent.is_symlink()):
            raise ValueError()
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        for raw in config.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if not separator or not name.replace("_", "").isalnum():
                raise ValueError()
            environment[name] = value
        if args.local_http:
            environment["HIGHSCORE_COOKIE_SECURE"] = "false"
            local_host = f"[{args.host}]" if ":" in args.host else args.host
            environment["HIGHSCORE_PUBLIC_URL"] = f"http://{local_host}:{args.port}/highscore/"
        for relative in (".cache/tmp", "output/web", "output/literature", "output/research-library",
                         "output/abtem", "output/pymatgen"):
            target = ROOT / relative
            if target.is_symlink() or not target.resolve().is_relative_to(ROOT):
                raise ValueError()
            target.mkdir(parents=True, exist_ok=True)
        for name in ("TMP", "TEMP", "TMPDIR"):
            environment[name] = str(ROOT / ".cache/tmp")
        python = ROOT / ".venv-web" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file():
            raise ValueError()
        return subprocess.call([str(python), "-B", "-m", "highscore_web", "serve", "--root", ".",
                                "--host", args.host, "--port", str(args.port)], cwd=ROOT, env=environment)
    except (OSError, ValueError):
        print("启动未完成：请先运行安装和初始化脚本；本机 HTTP 仅允许回环地址。未输出配置。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
