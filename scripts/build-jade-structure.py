"""项目内复制经核验 JCSD 原始快照，并构建 Linux 可用的结构派生 SQLite。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from highscore_database.jade_structure_build import snapshot_sources, build_database


def main():
    parser = argparse.ArgumentParser(description='免安装构建 JADE ICSD 结构派生库，不修改原库。')
    parser.add_argument('--source-dir', required=True)
    parser.add_argument('--software-dir', required=True)
    parser.add_argument('--snapshot-dir', default='data/inspection/pdf4-2009/jade-structure')
    parser.add_argument('--output', default='data/inspection/pdf4-2009/jade-icsd.sqlite3')
    parser.add_argument('--receipt', default='output/inspections/jade-pdf4-2009/linux-integration/icsd/build-receipt.json')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for value in (args.snapshot_dir, args.output, args.receipt):
        if not (root / value).resolve().is_relative_to(root):
            parser.error('所有输出必须位于本项目内。')
    snapshot_sources(root / args.source_dir, root / args.software_dir, root / args.snapshot_dir, project_root=root)
    receipt = build_database(root / args.snapshot_dir, root / args.output)
    receipt['output'] = './' + (root / args.output).resolve().relative_to(root).as_posix()
    path = root / args.receipt
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=True))


if __name__ == '__main__':
    main()
