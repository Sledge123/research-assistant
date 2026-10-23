"""在项目内安装源码运行/验证依赖并按前端锁文件构建，不读取私有配置。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv


ROOT = Path(__file__).resolve().parents[1]


class ChineseParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "命令行参数无效，请使用 --help 查看中文说明。\n")

    def format_help(self):
        return super().format_help().replace("usage:", "用法：", 1)


def python_path(directory: str) -> Path:
    return ROOT / directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def create_environment(name: str, requirements: list[str], environment: dict[str, str]) -> None:
    target = ROOT / name
    if target.is_symlink():
        raise ValueError("依赖环境不允许符号链接。")
    if not python_path(name).is_file():
        venv.EnvBuilder(with_pip=True).create(target)
    command = [str(python_path(name)), "-m", "pip", "install"]
    for requirement in requirements:
        command.extend(("-r", requirement))
    subprocess.run(command, cwd=ROOT, env=environment, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = ChineseParser(description="安装本地 HTTP 与前端依赖；可选创建独立验证环境", add_help=False)
    parser._optionals.title = "选项"
    parser.add_argument("-h", "--help", action="help", help="显示中文说明并退出")
    parser.add_argument("--checks", action="store_true", help="另外创建 ./.venv-check 安装公开验证所需依赖")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        print("请使用 Python 3.12 或更新版本运行安装脚本。", file=sys.stderr)
        return 1
    try:
        for relative in (".cache/tmp", ".cache/pip", ".cache/npm"):
            target = ROOT / relative
            if target.is_symlink() or not target.resolve().is_relative_to(ROOT):
                raise ValueError("缓存路径必须在项目内。")
            target.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
                           PIP_CACHE_DIR=str(ROOT / ".cache/pip"), TMPDIR=str(ROOT / ".cache/tmp"),
                           TEMP=str(ROOT / ".cache/tmp"), TMP=str(ROOT / ".cache/tmp"),
                           npm_config_cache=str(ROOT / ".cache/npm"))
        create_environment(".venv-web", ["requirements-web.txt"], environment)
        if args.checks:
            create_environment(".venv-check", ["requirements-web.txt", "requirements-literature.txt"], environment)
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if npm is None:
            raise ValueError("需要 Node.js 22 与 npm。")
        subprocess.run([npm, "ci"], cwd=ROOT / "web", env=environment, check=True)
        subprocess.run([npm, "run", "build"], cwd=ROOT / "web", env=environment, check=True)
        print("依赖环境与前端构建位于项目内；未初始化业务账号或运行秘密。")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("源码依赖或前端构建未完成，请检查上方安装输出。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
