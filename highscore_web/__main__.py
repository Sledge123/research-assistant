"""在项目目录内启动服务及执行可审计的本地运维操作。"""
import argparse
import ipaddress
import os
from pathlib import Path
import sys
import time

from .database import Database, dumps


def main(argv=None):
    parser = argparse.ArgumentParser(description="HighScore 网站服务及本地管理")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动单进程 API 与网站")
    serve.add_argument("--root", default=".")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    admin = sub.add_parser("admin", help="仅将已验证邮箱设为管理员")
    admin.add_argument("--root", default=".")
    admin.add_argument("--email", required=True)
    release = sub.add_parser("release-lease", help="人工确认旧数据库执行器已停止后释放过期执行锁")
    release.add_argument("--root", default=".")
    release.add_argument("--job-id", required=True)
    release.add_argument("--note", required=True, help="必须记录已检查旧进程停止的依据")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        parser.error("项目根目录不存在。")
    if args.command == "serve":
        from .app import create_app
        import uvicorn
        trusted_proxy_ips = os.getenv("HIGHSCORE_TRUSTED_PROXY_IPS", "127.0.0.1")
        try:
            # 仅信任运维明确指定的代理 IP，不接受通配符或隐式整网段。
            trusted_proxy_ips = ",".join(str(ipaddress.ip_address(value.strip()))
                                         for value in trusted_proxy_ips.split(","))
        except ValueError:
            parser.error("HIGHSCORE_TRUSTED_PROXY_IPS 必须为逗号分隔的明确代理 IP，不允许通配符。")
        # 禁止 access log 将邮箱验证链接 query token 写入日志。
        uvicorn.run(create_app(root), host=args.host, port=args.port, workers=1, access_log=False,
                    proxy_headers=True, forwarded_allow_ips=trusted_proxy_ips)
        return 0
    store = Database(root)
    with store.connection(write=True) as db:
        if args.command == "admin":
            row = db.execute("SELECT * FROM users WHERE email=? AND verified=1", (args.email.casefold(),)).fetchone()
            if not row:
                parser.error("邮箱不存在或尚未验证，未修改管理员权限。")
            db.execute("UPDATE users SET is_admin=1 WHERE id=?", (row["id"],))
            db.execute("INSERT INTO audit(user_id,action,detail,created_at) VALUES (?,'grant_admin','{}',?)", (row["id"], time.time()))
            print("已将已验证账号设为管理员。")
        else:
            if len(args.note.strip()) < 10:
                parser.error("核查依据至少需要 10 个字符。")
            row = db.execute("SELECT * FROM jobs WHERE id=? AND state='needs_attention' AND lease_expires IS NOT NULL", (args.job_id,)).fetchone()
            if not row:
                parser.error("任务不存在或没有需要人工释放的过期租约。")
            db.execute("UPDATE jobs SET lease_expires=NULL WHERE id=?", (args.job_id,))
            db.execute("INSERT INTO audit(action,detail,created_at) VALUES ('release_lease',?,?)", (dumps({"job_id": args.job_id, "note": args.note.strip()}), time.time()))
            print("已记录人工核查并释放数据库执行锁；原任务和邮件状态保持待核查。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
