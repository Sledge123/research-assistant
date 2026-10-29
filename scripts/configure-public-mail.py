"""交互设置部署者自己的独立 SMTP；不输出凭据、不联网、不发送邮件。"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ROLES = {"verification": "runtime/verification-mail", "literature": "runtime/literature-mail",
         "database": "runtime/database-mail"}


class ChineseParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "命令行参数无效，请使用 --help 查看中文说明。\n")

    def format_help(self):
        return super().format_help().replace("usage:", "用法：", 1)


def main(argv: list[str] | None = None) -> int:
    parser = ChineseParser(description="保存当前项目指定角色的 SMTP Fernet 密钥及加密配置", add_help=False)
    parser._optionals.title = "选项"
    parser.add_argument("-h", "--help", action="help", help="显示中文说明并退出")
    parser.add_argument("--role", choices=ROLES, required=True, help="注册验证、文献提醒或 XRD 邮件")
    args = parser.parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        from highscore_database.mail_config import save_settings
        from highscore_downloader.mail_settings import MailSettings
        role_root = ROOT / ROLES[args.role]
        if any(path.is_symlink() for path in (role_root, *role_root.parents) if path.is_relative_to(ROOT)):
            raise ValueError()
        role_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings = MailSettings(username=input("部署者自己的发件邮箱：").strip(),
                                smtp_host=input("自己的 SMTP SSL 主机名：").strip(),
                                smtp_port=int(input("SMTP SSL 端口：").strip()),
                                authorization_code=getpass.getpass("SMTP 授权码（不回显）："))
        save_settings(role_root, settings)
        print("已保存该角色独立的 SMTP 密钥与密文；未联网、未发送邮件、未输出秘密。")
        print("Linux 容器启动前用原 --site-url 运行初始化脚本 --fix-permissions，确保 UID/GID 10001 可读。")
        return 0
    except (ImportError, OSError, ValueError):
        print("SMTP 配置未完成：请核对安装依赖、私有目录和本机输入；未输出秘密。", file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
