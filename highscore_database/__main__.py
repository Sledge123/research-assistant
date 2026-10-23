"""显式 --run 才领取在线任务，默认只显示用法。"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from highscore_worker.config import load_config,WorkerError
from .reader import load_reader,DatabaseError

ROOT=Path(__file__).resolve().parents[1]


def main(argv=None):
    parser=argparse.ArgumentParser(description='HighScore SQLite 数据库检索与报告执行器（不启动 HighScore）')
    parser.add_argument('--config',default='worker.local.json',help='项目内执行器配置相对路径')
    parser.add_argument('--inspect',action='store_true',help='只读核验数据库并输出来源及内容指纹')
    parser.add_argument('--check-config',action='store_true',help='离线核验配置和数据库指纹，不联网或发信')
    parser.add_argument('--run',action='store_true',help='明确启动 HTTPS 任务领取、归档及邮件执行')
    parser.add_argument('--once',action='store_true',help='与 --run 同用，仅领取一次')
    parser.add_argument('--mail-only',action='store_true',help='仅领取注册验证邮件，不打开数据库或执行归档')
    options=parser.parse_args(argv)
    if options.once and not options.run:parser.error('--once 必须同时指定 --run')
    if not (options.inspect or options.check_config or options.run):
        parser.print_help();return 0
    try:
        if options.inspect:
            reader=load_reader(ROOT)
            print(json.dumps(reader.metadata(),ensure_ascii=False,indent=2));return 0
        config=load_config(ROOT,options.config)
        if options.mail_only:config=replace(config,mail_only=True)
        reader=None
        if not config.mail_only:
            reader=load_reader(ROOT)
            if config.database_id!=reader.database_id:raise WorkerError('执行器数据库标识与文件 SHA256 指纹不一致。')
        if options.check_config:
            print('PASS 配置与数据库指纹一致；未联网、未发送邮件。');return 0
        from .executor import Worker,JobHandler
        worker=Worker(config,handler=JobHandler(config,reader=reader))
        try:
            if options.once:worker.run_once()
            else:worker.run()
        except KeyboardInterrupt:worker.stop_event.set()
        return 0
    except (WorkerError,DatabaseError):
        print('FAIL 数据库或执行器配置未通过核验；请检查项目本地配置。',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
