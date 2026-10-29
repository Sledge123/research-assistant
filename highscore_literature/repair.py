"""管理员受控纠正错误元数据绑定；默认只读，不改积分或外部求助。"""
from contextlib import closing
import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .engine import StateStore, candidate_identity, clean_doi
from .metadata import candidates_from_items, plain
from .pdf import normalize_title


class RepairError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _require(condition, message):
    if not condition:
        raise RepairError(message)


def _admin(db, operator):
    _require(db.execute('SELECT 1 FROM users WHERE id=? AND verified=1 AND is_admin=1',
                        (operator,)).fetchone(), '操作人必须仍是已验证管理员。')


def _job(db, identifier):
    row = db.execute('SELECT * FROM literature_jobs WHERE id=?', (identifier,)).fetchone()
    _require(row, '原文献任务不存在。')
    return dict(row)


def _proof(store, name, job, expected, target):
    path = store.path(name)
    _require(path.is_file() and path.stat().st_size <= 2*1024*1024, '核查依据文件缺失或过大。')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    _require(isinstance(value, dict) and value.get('schema_version') == 1, '核查依据版本无法确认。')
    checked = value.get('checked_at')
    _require(type(checked) in (int, float) and 0 <= time.time()-checked <= 600,
             '核查依据已过期，需重新检查原账号和权威元数据。')
    _require(value.get('job_id') == job['id'] and value.get('account_ref') == job['account_ref']
             and value.get('expected_doi') == expected and value.get('target_doi') == target,
             '核查依据与原任务、账号或DOI不一致。')
    _require(value.get('complete') is True and value.get('matches') == []
             and value.get('submission_state') == 'confirmed_not_submitted',
             '尚未完整确认原账号无对应求助及未知提交，禁止纠正后重发。')
    source, destination = value.get('source_record'), value.get('target_record')
    _require(isinstance(source, dict) and isinstance(destination, dict), '缺少两份权威元数据。')
    _require(clean_doi(source.get('DOI')) == expected and source.get('type') in {'component', 'peer-review'},
             '原DOI未被权威来源明确标识为附件或审稿记录，禁止普通替换。')
    _require(clean_doi(destination.get('DOI')) == target and destination.get('type') == 'journal-article',
             '目标DOI没有得到正文类型核验。')
    for record in (source, destination):
        titles = record.get('title')
        _require(isinstance(titles, list) and len(titles) > 0 and isinstance(titles[0], str)
                 and normalize_title(plain(titles[0], 1000)) == normalize_title(job['canonical_title']),
                 '权威元数据题名与原论文不符。')
    candidates = candidates_from_items([destination])
    _require(len(candidates) == 1 and candidates[0]['doi'] == target, '目标正文候选身份无法核验。')
    return value, candidate_identity(candidates[0])


def _validate(db, store, job, expected, state_override=None):
    _require(job['state'] in {'queued', 'running', 'waiting', 'needs_attention'}
             and job['deadline'] > time.time() and job['credit_status'] == 'frozen',
             '原任务已取消、到期、结束或不再冻结积分，禁止重新激活。')
    _require(job['doi'] == expected and job['canonical_title'] and job['account_ref'],
             '原任务绑定已变化或缺少完整账号与论文身份。')
    _require(not job['external_request_id'] and not job['artifact_path'] and not job['artifact_size']
             and not job['ready_at'], '原任务已有求助编号或全文，禁止替换。')
    _require(not db.execute('SELECT 1 FROM literature_jobs WHERE lease_until>?', (time.time(),)).fetchone(),
             '仍有活动文献租约，需先停止执行器并等待租约到期。')
    selected = candidate_identity(json.loads(job['selected_candidate']))
    _require(selected['doi'] == expected and normalize_title(selected['title']) == normalize_title(job['canonical_title']),
             '原候选身份与任务不一致。')
    state = store.read(job['account_ref'], job['canonical_title']) if state_override is None else state_override
    _require(state['phase'] in {'new', 'publish_intent'} and not state.get('external_request_id')
             and not state.get('pdf_proof') and state.get('doi') == expected
             and state.get('metadata_identity') == selected, '原执行器记录不满足受控纠正条件。')
    entries = [dict(row) for row in db.execute('SELECT * FROM credit_entries WHERE job_id=? ORDER BY rowid', (job['id'],))]
    _require(len(entries) == 1 and entries[0]['kind'] == 'freeze'
             and entries[0]['amount'] == -1 and entries[0]['frozen_amount'] == 1,
             '原冻结流水不唯一或已有结算，禁止纠正。')
    return state, entries


def _audited(db, repair_id):
    return any(json.loads(row['detail']).get('repair_id') == repair_id for row in db.execute(
        "SELECT detail FROM audit WHERE action='literature_identity_repair'"))


def _pending(store, path, repair_id, job):
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    _require(value.get('schema_version') == 1 and value.get('repair_id') == repair_id
             and value.get('phase') in {'prepared', 'aborted'} and value.get('before_job') == job,
             '未完成回执与当前任务不符，保持执行器停止并人工核查。')
    actual = store.read(job['account_ref'], job['canonical_title'])
    _require(actual in (value.get('before_state'), value.get('after_state')),
             '中断后私有状态出现未知变化，禁止覆盖。')
    before_bytes = base64.b64decode(value['before_state_bytes'], validate=True)
    _require(json.loads(before_bytes) == value['before_state'], '原始私有状态快照无法核验。')
    return value


def repair_identity(root, *, job_id, expected_doi, target_doi, proof, operator_id,
                    database='output/web/state.sqlite3', apply=False):
    """仅在停文献角色、无活动租约及原账号完整核查后允许管理员执行。"""
    store = StateStore(root)
    try:
        _require(isinstance(job_id, str) and re.fullmatch(r'[a-f0-9]{32}', job_id), '任务编号格式无效。')
        expected, target = clean_doi(expected_doi), clean_doi(target_doi)
        _require(expected and target and expected != target, '必须指定不同的原DOI和目标正文DOI。')
        db_path = store.path(database)
        _require(db_path.is_file(), '项目内业务数据库不存在。')
        repair_id = hashlib.sha256(_json([job_id, expected, target]).encode()).hexdigest()
        journal_path = store.path('output/literature-worker/identity-repairs/'+repair_id+'/state.json')
        connection = sqlite3.connect(db_path.as_uri()+('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=15)
        connection.row_factory = sqlite3.Row
        with closing(connection) as db:
            if not apply:
                db.execute('PRAGMA query_only=ON')
            _admin(db, operator_id)
            job = _job(db, job_id)
            if _audited(db, repair_id):
                state = store.read(job['account_ref'], job['canonical_title'])
                _require(job['doi'] == target and state.get('doi') == target and journal_path.is_file(),
                         '已审计的纠正与当前状态不一致，保持执行器停止并人工核查。')
                journal = json.loads(journal_path.read_text(encoding='utf-8'))
                _require(journal.get('repair_id') == repair_id, '原纠正回执身份不一致。')
                if apply and journal.get('phase') != 'committed':
                    journal['phase'] = 'committed'
                    store._atomic(journal_path, _json(journal).encode())
                return {'status': 'already_applied', 'job_id': job_id,
                        'journal': './'+journal_path.relative_to(store.root).as_posix()}
            pending = _pending(store, journal_path, repair_id, job)
            state, entries = _validate(db, store, job, expected,
                                       pending['before_state'] if pending else None)
            evidence, candidate = _proof(store, proof, job, expected, target)
            if pending:
                _require(pending['original_entries'] == entries
                         and pending['after_state'].get('doi') == target
                         and pending['after_state'].get('metadata_identity') == candidate,
                         '中断回执的冻结流水或目标候选不一致。')
            if not apply:
                return {'status': 'ready', 'job_id': job_id, 'expected_doi': expected, 'target_doi': target,
                        'ledger_changed': False, 'original_deadline': job['deadline']}
        from .worker import worker_mutex
        with worker_mutex(store) as acquired:
            _require(acquired, '文献执行器仍持有账号互斥锁，禁止纠正。')
            with closing(sqlite3.connect(db_path, timeout=15)) as db:
                db.row_factory = sqlite3.Row
                db.execute('BEGIN IMMEDIATE')
                _admin(db, operator_id)
                _require(_job(db, job_id) == job, '准备期间原任务已改变，未执行纠正。')
                _require(_pending(store, journal_path, repair_id, job) == pending,
                         '准备期间纠正回执已变化。')
                _require(_validate(db, store, job, expected,
                         pending['before_state'] if pending else None) == (state, entries), '准备期间私有记录已改变。')
                _require(_proof(store, proof, job, expected, target) == (evidence, candidate),
                         '准备期间核查依据已变化，未执行纠正。')
                state_path = store.entry(job['account_ref'], job['canonical_title'])/'state.json'
                before_bytes = base64.b64decode(pending['before_state_bytes'], validate=True) if pending else state_path.read_bytes()
                # 已进入after快照的中断接续必须保持该快照稳定，不能先改回执再丢失旧after。
                new_state = dict(pending['after_state']) if pending else {
                    **state, 'phase': 'new', 'doi': target, 'metadata_identity': candidate, 'updated_at': time.time()}
                for key in ('intent_at', 'external_request_id'):
                    new_state.pop(key, None)
                new_bytes = _json(new_state).encode()
                journal = {'schema_version': 1, 'repair_id': repair_id, 'phase': 'prepared',
                           'operator_id': operator_id, 'created_at': time.time(), 'before_job': job,
                           'before_state': state, 'after_state': new_state, 'proof': evidence,
                           'original_entries': entries,
                           'before_state_bytes': base64.b64encode(before_bytes).decode('ascii')}
                if pending:
                    journal['attempts'] = pending.get('attempts', []) + [{
                        'phase': pending['phase'], 'created_at': pending['created_at'],
                        'proof_sha256': hashlib.sha256(_json(pending['proof']).encode()).hexdigest()}]
                store._atomic(journal_path, _json(journal).encode())
                committed = False
                try:
                    store._atomic(state_path, new_bytes)
                    db.execute("UPDATE literature_jobs SET doi=?,selected_candidate=?,state='queued',message='论文信息已核查，等待继续处理',candidates='[]',next_poll=?,lease_hash=NULL,lease_until=NULL WHERE id=?",
                               (target, _json(candidate), time.time(), job_id))
                    detail = {'repair_id': repair_id, 'job_id': job_id, 'before_doi': expected,
                              'after_doi': target, 'journal': './'+journal_path.relative_to(store.root).as_posix(),
                              'proof_sha256': hashlib.sha256(_json(evidence).encode()).hexdigest()}
                    db.execute("INSERT INTO audit(user_id,action,detail,created_at) VALUES (?,'literature_identity_repair',?,?)",
                               (operator_id, _json(detail), time.time()))
                    _require([dict(row) for row in db.execute('SELECT * FROM credit_entries WHERE job_id=? ORDER BY rowid', (job_id,))] == entries,
                             '纠正期间积分记录发生变化，未提交。')
                    db.commit()
                    committed = True
                    journal['phase'] = 'committed'
                    journal['committed_at'] = time.time()
                    store._atomic(journal_path, _json(journal).encode())
                except BaseException:
                    if not committed:
                        # COMMIT已持久化但尚未返回Python也可能抛异常，不能只信内存标志。
                        try:
                            db.rollback()
                        finally:
                            db.close()
                        with closing(sqlite3.connect(db_path.as_uri()+'?mode=ro', uri=True, timeout=15)) as persisted:
                            persisted.row_factory = sqlite3.Row
                            committed = _audited(persisted, repair_id)
                            persistent_job = _job(persisted, job_id)
                            _require(persistent_job['doi'] == (target if committed else expected),
                                     '数据库提交结果与回执不符，保持执行器停止。')
                        if not committed:
                            if state_path.read_bytes() == new_bytes:
                                store._atomic(state_path, before_bytes)
                            journal['phase'] = 'aborted'
                            store._atomic(journal_path, _json(journal).encode())
                    raise RepairError('纠正未完成，保持文献执行器停止并检查私有回执；禁止删除发布意图重试。') from None
        return {'status': 'applied', 'job_id': job_id, 'expected_doi': expected, 'target_doi': target,
                'original_deadline': job['deadline'], 'ledger_changed': False,
                'journal': './'+journal_path.relative_to(store.root).as_posix()}
    except RepairError:
        raise
    except Exception:
        raise RepairError('原任务、管理员或项目内核查依据无法验证，未允许恢复。') from None


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='管理员纠正文献错误附件DOI；默认只读，不修改积分或外部求助')
    parser.add_argument('--root', default='.', help='项目内完整相对目录')
    parser.add_argument('--database', default='output/web/state.sqlite3', help='项目内业务数据库相对路径')
    parser.add_argument('--job-id', required=True, help='原文献任务编号')
    parser.add_argument('--expected-doi', required=True, help='必须匹配的原错误DOI')
    parser.add_argument('--target-doi', required=True, help='经权威记录核验的正文DOI')
    parser.add_argument('--proof', required=True, help='十分钟内的原账号完整核查及两份元数据JSON，项目内路径')
    parser.add_argument('--operator-id', required=True, help='仍具备已验证管理员权限的操作人编号')
    parser.add_argument('--apply', action='store_true', help='显式执行；省略时严格只读')
    args = parser.parse_args(argv)
    try:
        project = Path(__file__).resolve().parents[1]
        root = StateStore(project).path(args.root)
        result = repair_identity(root, job_id=args.job_id, expected_doi=args.expected_doi,
            target_doi=args.target_doi, proof=args.proof, operator_id=args.operator_id,
            database=args.database, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except RepairError as error:
        print(str(error))
        return 2
    except Exception:
        print('恢复参数或项目路径无法核验，未允许执行。')
        return 2
