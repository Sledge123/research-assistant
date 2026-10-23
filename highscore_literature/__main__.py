"""python -B -m highscore_literature；省略运行参数只显示帮助。"""
import argparse
import json
import os
from pathlib import Path


class ChineseParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '命令行参数无效，请使用 --help 查看中文说明。\n')


def main(argv=None):
    parser = ChineseParser(description='Linux 科研通文献执行器（源码启动）', add_help=False,
        usage='python -B -m highscore_literature [--run | --once | --check-config] [--root ./项目内路径]')
    parser._optionals.title = '选项'
    parser.add_argument('-h', '--help', action='help', help='显示此说明并退出')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--run', action='store_true', help='每五秒领取一次文献任务')
    mode.add_argument('--once', action='store_true', help='最多处理一个有界任务片段')
    mode.add_argument('--check-config', action='store_true', help='只检查配置与启用账号数量，不登录或联网')
    parser.add_argument('--root', default='.', help='相对于本项目根目录的路径，默认当前源码项目')
    args = parser.parse_args(argv)
    if not (args.run or args.once or args.check_config):
        parser.print_help()
        return 0
    project = Path(__file__).resolve().parents[1]
    relative = Path(args.root)
    root = (project / relative).resolve()
    if relative.is_absolute() or '..' in relative.parts or not root.is_relative_to(project):
        print('执行器根目录必须使用项目内完整相对路径。')
        return 2
    try:
        from .worker import ApiClient, Worker, load_config
        from .credentials import load_accounts
        config = load_config(root)
        if args.check_config:
            accounts = load_accounts(root)
            print(json.dumps({'配置状态': '可用' if accounts else '缺少启用账号', '启用账号数量': len(accounts),
                '账号目录': './runtime/literature-credentials/', '执行器目录': './output/literature-worker/'}, ensure_ascii=False))
            return 0 if accounts else 2
        client = ApiClient(config)
        worker = Worker(root, client=client)
        if args.once:
            try:
                worker.run_once()
            finally:
                client.close()
        else:
            worker.run()
        return 0
    except Exception:
        # 配置异常和第三方库异常可能包含秘密路径、表单或 URL；只返回固定消息。
        print('文献执行器未能完成配置或运行检查，请管理员检查项目内加密账号与独立令牌。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
