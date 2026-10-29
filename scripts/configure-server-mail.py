"""本地交互配置或在 Windows 上安全迁移已有 DPAPI 邮箱；不打印秘密。"""
import argparse
import getpass
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description='保存项目内加密服务器邮箱配置')
    parser.add_argument('--import-windows', action='store_true', help='读取当前项目 DPAPI 配置后加密另存，不修改原配置')
    options = parser.parse_args()
    from highscore_database.mail_config import save_settings
    from highscore_downloader.mail_settings import MailSettings, load_mail_settings
    if options.import_windows:
        settings = load_mail_settings(ROOT)
        if settings.message or not settings.authorization_code:
            raise ValueError('当前 Windows 邮箱配置不可读取，未迁移。')
    else:
        settings = MailSettings(username=input('发件邮箱：').strip(),
                                smtp_host=input('SMTP 主机（默认 smtp.qq.com）：').strip() or 'smtp.qq.com',
                                smtp_port=int(input('SSL 端口（默认 465）：').strip() or '465'),
                                authorization_code=getpass.getpass('SMTP 授权码（不回显）：'))
    save_settings(ROOT, settings)
    print('已保存 runtime/mail.key 和 runtime/mail.encrypted.json；请保密备份，未发送邮件。')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError):
        print('配置未能完成，请检查输入及现有密钥；秘密内容未输出。', file=sys.stderr)
        sys.exit(1)
