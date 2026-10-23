"""一次性核验81文件并构建独立 JADE 索引 SQLite；不改动输入。"""
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
from datetime import datetime, timezone

from .jade_index import (FORMAT, RAW_FORMAT, FORMAT_V2, RAW_FORMAT_V2, EXPECTED_CARDS, SUBSET_HEADERS, SNAPSHOT_NAMES,
                         V2_FIXED_RECORD_SIZES, canonical, decode_sid, decode_rid, decode_zid, manifest_snapshot, _json)
from .reader import DatabaseError
from highscore_worker.config import project_path


def build_index(root, source_relative, output_relative, receipt_relative, *, format_version=2):
    if type(format_version) is not int or format_version not in (1, 2):
        raise DatabaseError('索引派生版本必须为1或2。')
    v2 = format_version == 2
    output_format, raw_format = (FORMAT_V2, RAW_FORMAT_V2) if v2 else (FORMAT, RAW_FORMAT)
    root = Path(root).resolve()
    source, target, receipt_path = (project_path(root, value) for value in (source_relative, output_relative, receipt_relative))
    if not source.is_dir() or target.exists() or receipt_path.exists() or source == target or source in target.parents:
        raise DatabaseError('索引源目录无效，或派生库/回执已存在；禁止覆盖。')
    manifest_path = project_path(root, (Path(source_relative) / 'snapshot-manifest.json').as_posix())
    manifest_bytes = manifest_path.read_bytes()
    snapshot = manifest_snapshot(manifest_bytes)
    files = {item['name']: item for item in snapshot['files']}
    if {path.name for path in source.glob('RDB-*') if path.is_file()} != SNAPSHOT_NAMES:
        raise DatabaseError('索引实际文件集合与81文件清单不一致。')
    signatures = {}
    for name, item in sorted(files.items()):
        path = project_path(root, (Path(source_relative) / name).as_posix())
        before = path.stat()
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        after = path.stat()
        signature = (after.st_size, after.st_mtime_ns)
        if (before.st_size, before.st_mtime_ns) != signature or after.st_size != item['bytes'] or digest.hexdigest() != item['sha256']:
            raise DatabaseError('索引源文件与快照大小/SHA256基线不一致：' + name)
        signatures[path] = signature
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix='.' + target.name + '-', suffix='.tmp', dir=target.parent, delete=False)
    temporary = Path(handle.name)
    handle.close()
    connection = None
    try:
        connection = sqlite3.connect(temporary)
        connection.executescript('''
        PRAGMA journal_mode=DELETE;
        CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE cards (code TEXT PRIMARY KEY,suffix TEXT NOT NULL,formula TEXT NOT NULL,
                            compound TEXT NOT NULL,mineral TEXT,peak_count INTEGER NOT NULL);
        CREATE TABLE records (code TEXT NOT NULL,subset TEXT NOT NULL,index_no INTEGER NOT NULL,
                              xid_start INTEGER NOT NULL,sid_start INTEGER NOT NULL,rid_start INTEGER NOT NULL,
                              xid BLOB NOT NULL,sid BLOB NOT NULL,rid BLOB NOT NULL,
                              PRIMARY KEY(code,subset));
        ''')
        if v2:
            for extension in V2_FIXED_RECORD_SIZES:
                connection.execute(f'ALTER TABLE records ADD COLUMN {extension}_start INTEGER NOT NULL DEFAULT 0')
                connection.execute(f"ALTER TABLE records ADD COLUMN {extension} BLOB NOT NULL DEFAULT X''")
        subset_counts = {}
        content_hashes = {}
        total_peaks = 0
        for subset in sorted(SUBSET_HEADERS):
            blobs = {}
            for extension in ('xid', 'sid', 'rid') + (tuple(V2_FIXED_RECORD_SIZES) if v2 else ()):
                name = f'{subset}.{extension}'
                data = (source / name).read_bytes()
                if len(data) != files[name]['bytes'] or hashlib.sha256(data).hexdigest() != files[name]['sha256']:
                    raise DatabaseError('索引读取内容与已验基线不一致。')
                blobs[extension] = data
            xid, sid, rid = (blobs[key] for key in ('xid', 'sid', 'rid'))
            if (len(xid) < 12 or xid[:2] != b'J7' or struct.unpack_from('<HH', xid, 2) != SUBSET_HEADERS[subset]
                    or xid[6:8] != b'\0\0' or (len(xid) - 12) % 16):
                raise DatabaseError('索引 XID 头或记录结构无效。')
            count = struct.unpack_from('<I', xid, 8)[0]
            if count == 0 or len(xid) != 12 + 16 * count:
                raise DatabaseError('索引 XID 计数无效。')
            for data in (sid, rid):
                if len(data) < 6 or data[:2] != b'J7' or struct.unpack_from('<I', data, 2)[0] != count:
                    raise DatabaseError('索引 SID/RID 头计数无效。')
            if v2:
                zid = blobs['zid']
                for extension, size in V2_FIXED_RECORD_SIZES.items():
                    fixed = blobs[extension]
                    if (len(fixed) != 6 + size * count or fixed[:2] != b'J7'
                            or struct.unpack_from('<I', fixed, 2)[0] != count):
                        raise DatabaseError('索引 '+extension.upper()+' 头、计数或定长记录无效。')
            entries = list(struct.iter_unpack('<IIII', xid[12:]))
            if entries[0][1:3] != (7, 7):
                raise DatabaseError('索引首条偏移错误。')
            subset_counts[subset] = count
            previous = -1
            for index, (number, sid1, rid1, source_id) in enumerate(entries):
                if number <= previous:
                    raise DatabaseError('索引卡号重复或顺序错误。')
                previous = number
                code = canonical(number)
                s, r = sid1 - 1, rid1 - 1
                se = entries[index + 1][1] - 1 if index + 1 < count else len(sid)
                re = entries[index + 1][2] - 1 if index + 1 < count else len(rid)
                if not (6 <= s < se <= len(sid) and 6 <= r < re <= len(rid)):
                    raise DatabaseError('索引 SID/RID 偏移错误。')
                sid_raw, rid_raw = sid[s:se], rid[r:re]
                fields, flags = decode_sid(sid_raw, index)
                _, peaks, _ = decode_rid(rid_raw, collect=False)
                zid_raw = zid[6 + index * 60:66 + index * 60] if v2 else b''
                if v2:
                    decode_zid(zid_raw)
                total_peaks += peaks
                cid_raw = blobs['cid'][6 + index * 44:50 + index * 44] if v2 else b''
                lid_raw = blobs['lid'][6 + index * 132:138 + index * 132] if v2 else b''
                # 全量47246重复记录证明LID仅byte2/3随子集不同，原值照存，
                # 其余130字节与其它单卡原始记录一并核对，不能忽略差异。
                lid_content = lid_raw[:2] + lid_raw[4:] if v2 else b''
                identity = hashlib.sha256(sid_raw[:sid_raw.find(b'\0', 2) + 1] + struct.pack('<HI', flags, source_id) + rid_raw + zid_raw + cid_raw + lid_content).digest()
                if code in content_hashes:
                    if content_hashes[code] != identity:
                        raise DatabaseError('跨子集同号索引卡原始内容冲突。')
                else:
                    content_hashes[code] = identity
                    connection.execute('INSERT INTO cards VALUES (?,?,?,?,?,?)',
                                       (code, code[3:], fields[0], fields[2] or fields[1], fields[1] if fields[2] else None, peaks))
                values = (code, subset, index, 12 + index * 16, s, r, xid[12 + index * 16:28 + index * 16], sid_raw, rid_raw)
                if v2:
                    for extension, size in V2_FIXED_RECORD_SIZES.items():
                        start = 6 + index * size
                        values += (start, blobs[extension][start:start + size])
                    connection.execute('INSERT INTO records VALUES (' + ','.join('?' for _ in values) + ')', values)
                else:
                    connection.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)', values)
            connection.commit()
        run = (source / 'RDB-PLU2009.run').read_bytes()
        if (len(run) != 80 or run[:2] != b'J8' or struct.unpack_from('<I', run, 2)[0] != EXPECTED_CARDS
                or len(content_hashes) != EXPECTED_CARDS):
            raise DatabaseError('索引 RUN/并集卡数与279588张基线不一致。')
        metadata = {'format': output_format, 'raw_format': raw_format, 'pattern_count': len(content_hashes),
                    'source_record_count': sum(subset_counts.values()), 'subset_counts': subset_counts,
                    'index_snapshot': snapshot, 'created_at': datetime.now(timezone.utc).isoformat()}
        connection.executemany('INSERT INTO metadata VALUES (?,?)', [(key, _json(value)) for key, value in metadata.items()])
        connection.executescript('CREATE INDEX card_suffix ON cards(suffix); CREATE INDEX card_compound ON cards(compound); CREATE INDEX card_mineral ON cards(mineral); CREATE INDEX card_formula ON cards(formula);')
        connection.commit()
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise DatabaseError('索引派生库完整性检查失败。')
        connection.close()
        connection = None
        for path, signature in signatures.items():
            current = path.stat()
            if (current.st_size, current.st_mtime_ns) != signature:
                raise DatabaseError('构建期间原始索引文件发生变化。')
        if manifest_path.read_bytes() != manifest_bytes:
            raise DatabaseError('构建期间原始快照清单发生变化。')
        for path in (temporary.resolve(), target.resolve()):
            path.relative_to(root)
        if target.exists():
            raise DatabaseError('输出已存在，禁止覆盖。')
        temporary.rename(target)
        digest = hashlib.sha256()
        with target.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):digest.update(chunk)
        result = {'format': output_format, 'path': target.relative_to(root).as_posix(), 'bytes': target.stat().st_size,
                  'sha256': digest.hexdigest(), 'database_id': 'jadeidx-' + digest.hexdigest()[:48],
                  'pattern_count': len(content_hashes), 'source_record_count': sum(subset_counts.values()),
                  'index_peaks_including_subsets': total_peaks, 'source_content_fingerprint_sha256': snapshot['content_fingerprint_sha256'],
                  'source_files_verified': 81, 'complete_original_peak_table': False}
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with receipt_path.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        return result
    except (sqlite3.Error, OSError, ValueError, struct.error) as error:
        if isinstance(error, DatabaseError):raise
        raise DatabaseError('JADE 索引派生库构建失败，未将半成品用于查询。') from error
    finally:
        if connection is not None:connection.close()
        if temporary.exists():
            temporary.resolve().relative_to(root)
            temporary.unlink()
