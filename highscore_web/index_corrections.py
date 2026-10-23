"""受控补全旧JADE索引的ZID字段；原任务、归档、邮件和积分保持。

纠正包从已核验的新v2派生库生成，必须证明原同卡XID/SID/RID全部原样，
且使用完全相同的来源快照清单。这里不借用相似卡片，不自行解析私有库。
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time

from highscore_downloader.archive import ArchiveError, ArchiveStore
from highscore_downloader.numbers import parse_code

ACTION = 'xrd_index_zid_correction_registered'
MAX_FILE_BYTES = 24 * 1024 * 1024
MAX_PACKAGE_BYTES = 80 * 1024 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def _same(left, right):
    # JSON编码同时区分bool/int和int/float，不能让Python的True==1放松来源约束。
    return _json(left) == _json(right)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _safe(root, value):
    root = Path(root).absolute()
    raw = Path(value)
    if '..' in raw.parts or os.name != 'nt' and '\\' in str(value):
        raise ArchiveError('纠正包路径必须位于项目内且不得包含回退或链接。')
    path = raw if raw.is_absolute() else root / raw
    try:
        relative = path.relative_to(root)
        for current in [root, *(root.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts) + 1))]:
            if current.is_symlink(): raise ValueError()
            if current.exists() and getattr(current.lstat(), 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
                raise ValueError()
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        raise ArchiveError('纠正包路径越界或包含符号链接。') from None
    return path


def _read(path, maximum=MAX_FILE_BYTES):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ArchiveError('纠正包文件不存在、为空、过大或包含链接。')
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    if not data or len(data) > maximum:
        raise ArchiveError('纠正包文件超过限制。')
    return data


def _connect(root, *, write=False):
    path = _safe(root, 'output/web/state.sqlite3')
    if not path.is_file(): raise ArchiveError('网站业务数据库不存在。')
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    if not write: db.execute('PRAGMA query_only=ON')
    return db


def _load_archive(root, directory, code):
    directory = _safe(root, directory)
    receipt_path = 'output/cards/' + code + '/receipt.json'
    receipt_bytes = _read(_safe(root, directory / receipt_path), 1024 * 1024)
    receipt = json.loads(receipt_bytes)
    if set(receipt.get('files', {})) != {'json', 'txt', 'csv', 'rtf'}:
        raise ArchiveError('索引纠正只接受完整四格式归档。')
    payloads = {}
    for kind, item in receipt['files'].items():
        expected = 'output/cards/' + code + '/' + code + '.' + kind
        if item.get('path') != expected: raise ArchiveError('纠正包文件路径不符合单卡约定。')
        payloads[code + '.' + kind] = _read(_safe(root, directory / expected))
    if sum(map(len, payloads.values())) > MAX_PACKAGE_BYTES:
        raise ArchiveError('纠正包超过大小限制。')
    ArchiveStore(directory).validate(receipt_path)
    return json.loads(payloads[code + '.json']), payloads, receipt_bytes, receipt


def _original(root, db, job_id, code):
    job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if (not job or job['kind'] != 'archive' or job['archive_status'] not in ['success', 'partial']
            or not job['artifact_dir'] or job['state'] in ['queued', 'running']):
        raise ArchiveError('原任务没有可纠正的已核验索引归档。')
    directory = _safe(root, job['artifact_dir'])
    directory.relative_to(_safe(root, 'output/web/artifacts'))
    card, payloads, receipt_bytes, receipt = _load_archive(root, directory, code)
    binding = json.loads(job['artifact_expected'] or job['results'] or '[]')
    matches = [item for item in binding if isinstance(item, dict) and isinstance(item.get('code'), str)
               and parse_code(item['code']).matches(code)
               and (not item.get('source') or item['source'] == card.get('source'))]
    if len(matches) != 1:
        raise ArchiveError('纠正卡片或实际来源不属于原任务已选归档。')
    if (card.get('data_type') != 'jade_index_data'
            or card.get('database_raw', {}).get('reader_profile') != 'jade_pdf4_2009_index_v1'
            or card.get('verification', {}).get('report_version') not in (3, 4)):
        raise ArchiveError('仅可受控补全已核验v3/v4的v1索引记录；其他版本保持。')
    return job, card, payloads, receipt_bytes, receipt


def _proof(job_id, old, old_payloads, old_receipt_bytes, new, new_payloads, new_receipt_bytes):
    raw = old['database_raw']; updated = new['database_raw']
    previous_version = old.get('verification', {}).get('report_version')
    if type(previous_version) is not int or previous_version not in (3, 4):
        raise ArchiveError('原索引报告版本没有可核验的受控补全契约。')
    if (new.get('data_type') != 'jade_index_data'
            or new.get('verification', {}).get('report_version') not in (5, 6)
            or updated.get('reader_profile') != 'jade_pdf4_2009_index_v2'
            or updated.get('format') != 'jade_index_card_records_v2'
            or not isinstance(new.get('index_metadata'), dict)
            or old['database_sha256'] == new['database_sha256']):
        raise ArchiveError('纠正包必须由独立v2索引的ZID原字节生成已核验报告。')
    allowed = {'database_id', 'database_sha256', 'verification', 'report_fields', 'raw_text',
               'cell_parameters', 'space_group', 'index_metadata', 'database_raw'}
    if new['verification']['report_version'] == 6:
        from highscore_database.jade_index import validate_index_card
        if not _same(validate_index_card(old)['peaks'], validate_index_card(new)['peaks']):
            raise ArchiveError('纠正包改变了原始索引峰表。')
        allowed.update(('peaks', 'angular_conversion', 'wavelength_angstrom', 'intensity_normalization'))
    if not _same({k: v for k, v in old.items() if k not in allowed}, {k: v for k, v in new.items() if k not in allowed}):
        raise ArchiveError('纠正包改变了原卡名称、化学式、来源、峰表或其他既有科学字段。')
    if not _same(raw['index_snapshot'], updated['index_snapshot']):
        raise ArchiveError('纠正包并非来自原索引的完全相同快照清单。')
    if set(raw) != {'format', 'reader_profile', 'index_snapshot', 'records'} or set(updated) != set(raw):
        raise ArchiveError('纠正包来源记录结构不受支持。')
    records = raw['records']; new_records = updated['records']
    if not records or len(records) != len(new_records):
        raise ArchiveError('纠正包改变了原卡的来源子集集合。')
    for before, after in zip(records, new_records):
        additions = set(after) - set(before)
        if (additions != {'zid', 'cid', 'lid'} or set(before) & additions
                or not _same(before, {k: v for k, v in after.items() if k not in additions})):
            raise ArchiveError('纠正包改变了原XID/SID/RID字节、偏移或来源摘要。')
    return {'kind': 'index_zid_metadata_correction', 'job_id': job_id, 'code': old['code'],
        'source': old['source'], 'from_version': previous_version, 'to_version': new['verification']['report_version'],
        'from_database_sha256': old['database_sha256'], 'to_database_sha256': new['database_sha256'],
        'index_snapshot_manifest_sha256': raw['index_snapshot']['manifest_sha256'],
        'source_archive_unchanged': True, 'original_index_records_unchanged': True,
        'archive_receipt_sha256': _sha(old_receipt_bytes), 'corrected_receipt_sha256': _sha(new_receipt_bytes),
        'original_files_sha256': {name: _sha(value) for name, value in old_payloads.items()},
        'rendered_files_sha256': {name: _sha(value) for name, value in new_payloads.items()}}


def _destination(root, job_id, code):
    if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', job_id):
        raise ArchiveError('纠正任务标识无效。')
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{2}-[0-9]{3}-[0-9]{4}', code):
        raise ArchiveError('纠正卡片编号无效。')
    return _safe(root, 'output/web/report-corrections/' + job_id + '/' + code + '/v5')


def _publish(staging, destination):
    # staging与destination的父目录相同，不能跨容器挂载rename。
    staging.rename(destination)


def register_index_correction(root, job_id, corrected_root, admin_id):
    """由本机受控运维注册单卡v5纠正副本；唯一写入为新副本和原审计表。"""
    root = Path(root).absolute(); staging = None; db = None
    try:
        db = _connect(root, write=True); db.execute('BEGIN IMMEDIATE')
        admin = db.execute('SELECT verified,is_admin FROM users WHERE id=?', (admin_id,)).fetchone()
        if not admin or admin['verified'] != 1 or admin['is_admin'] != 1:
            raise ArchiveError('仅实时已验证管理员可注册索引纠正。')
        corrected_root = _safe(root, corrected_root)
        receipts = list(_safe(root, corrected_root / 'output/cards').glob('*/receipt.json'))
        if len(receipts) != 1: raise ArchiveError('每次索引纠正须只包含一张完整卡片。')
        code = receipts[0].parent.name; destination = _destination(root, job_id, code)
        job, old, old_payloads, old_receipt_bytes, old_receipt = _original(root, db, job_id, code)
        new, new_payloads, new_receipt_bytes, new_receipt = _load_archive(root, corrected_root, code)
        proof = _proof(job_id, old, old_payloads, old_receipt_bytes, new, new_payloads, new_receipt_bytes)
        manifest = {'schema_version': 1, 'owner_id': job['user_id'], 'job_id': job_id, 'code': code, 'proof': proof}
        if destination.exists():
            existing = json.loads(_read(_safe(root, destination / 'manifest.json'), 1024 * 1024))
            if not _same(existing, manifest): raise ArchiveError('原任务已注册不同纠正包，禁止覆盖。')
            stored = _load_archive(root, destination, code)
            if not _same(stored[0], new) or stored[1] != new_payloads or stored[2] != new_receipt_bytes:
                raise ArchiveError('已登记纠正包损坏或与本次内容不同。')
        else:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _safe(root, destination.parent)
            staging = Path(tempfile.mkdtemp(prefix='.v5-staging-', dir=destination.parent)); staging.chmod(0o700)
            for kind, item in new_receipt['files'].items():
                target = staging / item['path']; target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(new_payloads[code + '.' + kind]); target.chmod(0o600)
            target = staging / new_receipt['receipt_path']; target.write_bytes(new_receipt_bytes); target.chmod(0o600)
            target = staging / 'manifest.json'; target.write_text(_json(manifest), encoding='utf-8'); target.chmod(0o600)
            _load_archive(root, staging, code)
            _publish(staging, destination); staging = None
        detail = _json({'job_id': job_id, 'code': code, 'owner_id': job['user_id'], 'proof': proof})
        if not db.execute('SELECT 1 FROM audit WHERE action=? AND detail=?', (ACTION, detail)).fetchone():
            db.execute('INSERT INTO audit(user_id,action,detail,created_at) VALUES (?,?,?,?)', (admin_id, ACTION, detail, time.time()))
        db.commit()
        return proof
    except ArchiveError:
        if db: db.rollback()
        raise
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        if db: db.rollback()
        raise ArchiveError('索引纠正注册失败；原任务、归档及账本保留。') from error
    finally:
        if db: db.close()
        if staging is not None and staging.exists():
            _safe(root, staging); shutil.rmtree(staging)


def corrected_report(root, job_id, original_card, original_payloads, original_receipt_bytes):
    """鉴权调用方下载同任务时只读使用已登记副本；未注册返回None。"""
    root = Path(root).absolute(); db = None
    try:
        code = original_card.get('code'); destination = _destination(root, job_id, code)
        if not destination.exists(): return None
        manifest = json.loads(_read(_safe(root, destination / 'manifest.json'), 1024 * 1024))
        db = _connect(root)
        job, old, payloads, receipt_bytes, unused = _original(root, db, job_id, code)
        if (not _same(old, original_card) or payloads != original_payloads or receipt_bytes != original_receipt_bytes
                or manifest.get('schema_version') != 1 or manifest.get('job_id') != job_id
                or manifest.get('code') != code or manifest.get('owner_id') != job['user_id']):
            raise ArchiveError('纠正包与当前任务所有者、原JSON或原回执字节不符。')
        new, new_payloads, new_receipt_bytes, unused = _load_archive(root, destination, code)
        proof = _proof(job_id, old, payloads, receipt_bytes, new, new_payloads, new_receipt_bytes)
        if not _same(proof, manifest.get('proof')): raise ArchiveError('纠正包来源或文件摘要绑定已变化。')
        detail = _json({'job_id': job_id, 'code': code, 'owner_id': job['user_id'], 'proof': proof})
        if not db.execute('SELECT 1 FROM audit WHERE action=? AND detail=?', (ACTION, detail)).fetchone():
            raise ArchiveError('纠正包尚未完成管理员审计登记。')
        return new, new_payloads, proof
    except ArchiveError: raise
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        raise ArchiveError('已登记索引纠正包读回复验失败；原归档保留。') from error
    finally:
        if db: db.close()
