"""从项目内完整快照生成独立 JADE 索引数据库。"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from highscore_database.jade_index_build import build_index
from highscore_database.reader import DatabaseError


def main():
    parser = argparse.ArgumentParser(description='一次性构建已核验 JADE 索引派生库，不覆盖现有文件。')
    parser.add_argument('--source', default='data/inspection/pdf4-2009/jade-index')
    parser.add_argument('--output', default='data/inspection/pdf4-2009/jade-pdf4-2009.sqlite3')
    parser.add_argument('--receipt', default='output/inspections/jade-pdf4-2009/linux-integration/build-receipt.json')
    args = parser.parse_args()
    try:
        result = build_index(ROOT, args.source, args.output, args.receipt)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DatabaseError, OSError, ValueError) as error:
        print('FAIL ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
