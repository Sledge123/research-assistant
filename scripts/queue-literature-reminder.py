"""为一项已核验成功且仍可下载的历史文献任务排入提醒邮件。"""
import argparse
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]


def main(argv=None):
    parser=argparse.ArgumentParser(description='为已成功的文献任务排入下载提醒（幂等）')
    parser.add_argument('--job-id',required=True)
    args=parser.parse_args(argv)
    if not re.fullmatch(r'[a-f0-9]{32}',args.job_id):
        parser.error('--job-id 格式无效。')
    try:
        from highscore_web.database import Database
        from highscore_web.literature_store import LiteratureStore
        database=Database(ROOT)
        service=LiteratureStore(ROOT,database)
        with database.connection(write=True) as db:
            result=service.queue_existing_success_reminder(db,args.job_id)
        print(f"{result['state']} {'已排入' if result['created'] else '此前已存在'}；未输出收件人地址。")
        return 0
    except Exception:
        print('FAIL 未能核验并排入文献下载提醒；未输出收件人地址。',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
