"""固定调试账号登记与受控启停；不创建用户、赠分或改写历史业务。"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

FEATURES = ('research_library', 'literature', 'pymatgen', 'xrd', 'abtem')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS debug_accounts (
 user_id TEXT PRIMARY KEY REFERENCES users(id), email TEXT NOT NULL,
 fixed_feature TEXT UNIQUE CHECK(fixed_feature IS NULL OR fixed_feature IN
 ('research_library','literature','pymatgen','xrd','abtem')),
 evidence TEXT NOT NULL, classified_by TEXT NOT NULL REFERENCES users(id), classified_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS debug_account_runs (
 run_key TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES debug_accounts(user_id),
 operator_id TEXT NOT NULL REFERENCES users(id), started_at REAL NOT NULL, closed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS debug_account_active_run ON debug_account_runs(user_id) WHERE closed_at IS NULL;
'''


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def is_debug(db, user_id):
    # 独立旧账本工具可能尚未初始化网站附表；网站启动会始终创建该表。
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='debug_accounts'").fetchone():
        return False
    return bool(db.execute('SELECT 1 FROM debug_accounts WHERE user_id=?', (user_id,)).fetchone())


def operator(db, user_id):
    row = db.execute('SELECT id FROM users WHERE id=? AND verified=1 AND is_admin=1', (user_id,)).fetchone()
    if row is None or is_debug(db, user_id):
        raise ValueError('操作人必须是当前已验证的正式管理员。')


def audit(db, actor, action, detail):
    db.execute('INSERT INTO audit(user_id,action,detail,created_at) VALUES (?,?,?,?)',
               (actor, action, encoded(detail), time.time()))


def classify(db, actor, accounts):
    operator(db, actor)
    if not accounts or len({a['user_id'] for a in accounts}) != len(accounts):
        raise ValueError('调试账号清单为空或身份重复。')
    changes = 0
    for item in accounts:
        identity, email, feature = item['user_id'], item['email'], item.get('feature')
        evidence = item.get('evidence')
        if (identity == actor or feature is not None and feature not in FEATURES or
                item.get('verified_evidence') is not True or not isinstance(evidence, list) or not evidence):
            raise ValueError('仅接受已逐项核验的调试身份与证据。')
        for proof in evidence:
            relative = Path(proof['path'])
            if (relative.is_absolute() or '..' in relative.parts or '\\' in proof['path'] or ':' in proof['path'] or
                    len(proof['sha256']) != 64 or any(c not in '0123456789abcdef' for c in proof['sha256']) or
                    not proof.get('reference') or not proof.get('reason')):
                raise ValueError('证据必须绑定项目内相对路径、SHA256及可复核位置。')
        row = db.execute('SELECT email,verified,is_admin FROM users WHERE id=?', (identity,)).fetchone()
        if row is None or row['email'] != email or row['is_admin']:
            raise ValueError('调试账号不存在、邮箱不符或具有管理员权限。')
        existing = db.execute('SELECT email,fixed_feature,evidence FROM debug_accounts WHERE user_id=?', (identity,)).fetchone()
        if existing:
            if (existing['email'], existing['fixed_feature'], existing['evidence']) != (email, feature, encoded(evidence)):
                raise ValueError('账号已有不同分类，禁止隐式覆盖。')
            continue
        if row['verified'] or db.execute('SELECT 1 FROM sessions WHERE user_id=? AND expires>?', (identity, time.time())).fetchone():
            raise ValueError('历史调试账号仍已启用或存在有效会话，停止归类。')
        # 归类不能追溯撤销已投递邮件，也不能改变正在执行的业务。
        if db.execute("SELECT 1 FROM jobs WHERE user_id=? AND state IN ('queued','running')", (identity,)).fetchone():
            raise ValueError('账号仍有待执行任务，停止归类。')
        if db.execute("SELECT 1 FROM literature_reminders r JOIN literature_jobs j ON j.id=r.job_id WHERE j.user_id=? AND r.state='sending'", (identity,)).fetchone():
            raise ValueError('账号有正在发送的提醒，停止归类。')
        db.execute('INSERT INTO debug_accounts VALUES (?,?,?,?,?,?)',
                   (identity, email, feature, encoded(evidence), actor, time.time()))
        audit(db, actor, 'debug_account_classified', {'user_id': identity, 'feature': feature, 'evidence': evidence})
        changes += 1
    return {'classified': changes, 'total': db.execute('SELECT count(*) FROM debug_accounts').fetchone()[0]}


def fixed(db, feature):
    if feature not in FEATURES:
        raise ValueError('未知调试功能。')
    row = db.execute('SELECT d.*,u.email AS current_email,u.verified,u.is_admin,u.password_hash FROM debug_accounts d '
                     'JOIN users u ON u.id=d.user_id WHERE d.fixed_feature=?', (feature,)).fetchone()
    if row is None or row['email'] != row['current_email'] or row['is_admin']:
        raise ValueError('固定调试账号缺失、身份发生变化或权限异常，禁止自动补建。')
    return row


def set_initial_password(db, actor, feature, password):
    from .security import password_hash, password_matches
    operator(db, actor)
    row = fixed(db, feature)
    if not isinstance(password, str) or not 24 <= len(password) <= 128:
        raise ValueError('调试密码长度不符合要求。')
    if row['password_hash'].startswith('pbkdf2_sha256$'):
        if not password_matches(password, row['password_hash']):
            raise ValueError('已有登录密码不同，禁止隐式重置。')
        return {'initialized': False}
    if feature != 'literature':
        raise ValueError('只有已确认没有登录密码的旧文献调试账号允许首次设置密码。')
    if row['verified'] or db.execute('SELECT 1 FROM debug_account_runs WHERE user_id=? AND closed_at IS NULL', (row['user_id'],)).fetchone():
        raise ValueError('账号已启用，不能设置初始密码。')
    db.execute('UPDATE users SET password_hash=? WHERE id=?', (password_hash(password), row['user_id']))
    audit(db, actor, 'debug_account_password_initialized', {'user_id': row['user_id'], 'feature': feature})
    return {'initialized': True}


def start(db, actor, feature, run_key, *, password):
    from .security import password_matches
    operator(db, actor)
    row = fixed(db, feature)
    if not isinstance(password, str) or not password_matches(password, row['password_hash']):
        raise ValueError('集中凭据与原账号密码不符，停止启用。')
    if not isinstance(run_key, str) or not 8 <= len(run_key) <= 160 or any(ord(c) < 33 or ord(c) > 126 for c in run_key):
        raise ValueError('调试轮次标识无效。')
    previous = db.execute('SELECT * FROM debug_account_runs WHERE run_key=?', (run_key,)).fetchone()
    if previous:
        if (previous['user_id'] != row['user_id'] or previous['operator_id'] != actor or
                previous['closed_at'] is not None or not row['verified']):
            raise ValueError('轮次标识已被使用或已关闭，请核对原轮次。')
        return {'user_id': row['user_id'], 'email': row['email'], 'feature': feature, 'run_key': run_key}
    if row['verified'] or db.execute('SELECT 1 FROM debug_account_runs WHERE user_id=? AND closed_at IS NULL', (row['user_id'],)).fetchone():
        raise ValueError('同一固定账号已有活动调试轮次，不能并发重新启用。')
    if not row['password_hash'].startswith('pbkdf2_sha256$'):
        raise ValueError('固定账号尚未设置有效登录密码。')
    db.execute('DELETE FROM sessions WHERE user_id=?', (row['user_id'],))
    db.execute('UPDATE users SET verified=1 WHERE id=?', (row['user_id'],))
    db.execute('INSERT INTO debug_account_runs VALUES (?,?,?,?,NULL)', (run_key, row['user_id'], actor, time.time()))
    audit(db, actor, 'debug_account_started', {'user_id': row['user_id'], 'feature': feature, 'run_key': run_key})
    return {'user_id': row['user_id'], 'email': row['email'], 'feature': feature, 'run_key': run_key}


def close(db, actor, feature, run_key):
    operator(db, actor)
    row = fixed(db, feature)
    run = db.execute('SELECT * FROM debug_account_runs WHERE run_key=? AND user_id=?', (run_key, row['user_id'])).fetchone()
    if run is None or run['operator_id'] != actor:
        raise ValueError('调试轮次不属于当前账号和操作人，禁止关闭其他轮次。')
    if run['closed_at'] is not None:
        return {'closed': True, 'already_closed': True, 'run_key': run_key}
    revoked = db.execute('DELETE FROM sessions WHERE user_id=?', (row['user_id'],)).rowcount
    db.execute('UPDATE users SET verified=0 WHERE id=?', (row['user_id'],))
    db.execute('UPDATE debug_account_runs SET closed_at=? WHERE run_key=?', (time.time(), run_key))
    audit(db, actor, 'debug_account_closed', {'user_id': row['user_id'], 'feature': feature, 'run_key': run_key, 'revoked_sessions': revoked})
    return {'closed': True, 'already_closed': False, 'run_key': run_key, 'revoked_sessions': revoked}


def main():
    parser = argparse.ArgumentParser(description='项目内固定调试账号管理，不创建用户。')
    parser.add_argument('action', choices=('status', 'classify', 'initialize-password', 'start', 'close'))
    parser.add_argument('--root', default='.')
    parser.add_argument('--database', default='output/web/state.sqlite3')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    relative = Path(args.database)
    path = (root / relative).resolve()
    if relative.is_absolute() or '..' in relative.parts or not path.is_relative_to(root) or not path.is_file() or (root / relative).is_symlink():
        parser.error('只允许明确存在的项目内网站数据库。')
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        if args.action == 'status':
            db.execute('PRAGMA query_only=ON')
            result = {'accounts': [dict(row) for row in db.execute('SELECT d.user_id,d.email,d.fixed_feature,u.verified,'
                'u.is_admin,(SELECT count(*) FROM sessions s WHERE s.user_id=d.user_id) AS sessions,'
                '(SELECT run_key FROM debug_account_runs r WHERE r.user_id=d.user_id AND r.closed_at IS NULL) AS active_run '
                'FROM debug_accounts d JOIN users u ON u.id=d.user_id ORDER BY d.fixed_feature,d.user_id')]}
        else:
            data = json.load(sys.stdin)
            db.execute('BEGIN IMMEDIATE')
            actor = data['operator_id']
            if args.action == 'classify':
                result = classify(db, actor, data['accounts'])
            elif args.action == 'initialize-password':
                result = set_initial_password(db, actor, data['feature'], data['password'])
            elif args.action == 'start':
                result = start(db, actor, data['feature'], data['run_key'], password=data['password'])
            else:
                result = close(db, actor, data['feature'], data['run_key'])
            db.commit()
        print(encoded(result))
    except (ValueError, KeyError, sqlite3.Error):
        db.rollback()
        print('FAIL 固定调试账号操作未完成；请核对账号身份、活动轮次、权限和证据。', file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
