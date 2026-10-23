"""从完整私有 JCSD 快照构建只读结构派生 SQLite，保留原始字节。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct

from .jade_structure import (FORMAT, RAW_FORMAT, SOURCE_NAMES, canonical, decode_payload,
                             decode_setting, manifest_snapshot, _json)
from .reader import DatabaseError


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


def snapshot_sources(csd_directory, software_directory, destination, *, project_root=None):
    root = Path(project_root or Path.cwd()).resolve()
    destination = Path(destination).resolve()
    if not destination.is_relative_to(root) or destination.is_symlink():
        raise DatabaseError('结构快照必须保存在项目内。')
    destination.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in sorted(SOURCE_NAMES):
        source = Path(csd_directory if name.startswith('ICSD-') else software_directory) / name
        if not source.is_file() or source.is_symlink():
            raise DatabaseError('结构来源文件不存在或不是普通文件：' + name)
        before = source.stat()
        digest = file_sha(source)
        target = destination / name
        if target.exists():
            if target.is_symlink() or not target.is_file() or target.stat().st_size != before.st_size or file_sha(target) != digest:
                raise DatabaseError('已有结构快照内容不同，禁止覆盖：' + name)
        else:
            shutil.copyfile(source, target)
        after = source.stat()
        if ((before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                or target.stat().st_size != before.st_size or file_sha(target) != digest):
            raise DatabaseError('结构来源在复制期间变化或摘要不一致：' + name)
        rows.append({'name': name, 'bytes': before.st_size, 'sha256': digest,
                     'source_relative_path': os.path.relpath(source, root).replace('\\', '/')})
    manifest = _json({'type': 'JADE_STRUCTURE_SNAPSHOT', 'files': rows}).encode('utf-8')
    path = destination / 'manifest.json'
    if path.exists() and path.read_bytes() != manifest:
        raise DatabaseError('已有结构快照清单不同，禁止覆盖。')
    if not path.exists():
        path.write_bytes(manifest)
    return manifest_snapshot(manifest)


def build_database(snapshot_directory, output_path):
    directory = Path(snapshot_directory).resolve()
    output = Path(output_path).resolve()
    if output.exists() or output.is_symlink():
        raise DatabaseError('结构派生库目标已存在，禁止覆盖。')
    manifest_path = directory / 'manifest.json'
    manifest_signature = _signature(manifest_path)
    manifest = manifest_snapshot(manifest_path.read_bytes())
    if _signature(manifest_path) != manifest_signature:
        raise DatabaseError('结构快照清单在读取期间变化。')
    files = {}
    signatures = {'manifest.json': manifest_signature}
    for row in manifest['files']:
        path = directory / row['name']
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes']:
            raise DatabaseError('结构快照文件大小或 SHA256 不匹配：' + row['name'])
        before = _signature(path)
        data = path.read_bytes()
        if _signature(path) != before:
            raise DatabaseError('结构快照文件在读取期间变化：' + row['name'])
        if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise DatabaseError('结构快照读取字节与 SHA256 不匹配：' + row['name'])
        files[row['name']] = data
        signatures[row['name']] = before
    d, a, c, b = (files['ICSD-All.' + extension] for extension in ('csd', 'csa', 'csc', 'csb'))
    g, y = files['jade9.SGC'], files['jade9.SYM']
    try:
        if any(data[:4] != b'jcsd' or len(data) < 1024 or struct.unpack_from('<I', data, 12)[0] != 3 for data in (d, a, c, b)):
            raise ValueError()
        count, maximum = struct.unpack_from('<II', d, 4)
        if (not count or len(d) != 1028 + count * 124 or d[-4:] != d[26:30]
                or len(b) != 1024 + count * 8 or struct.unpack_from('<I', b, 4)[0] != count
                or g[:2] != b'J6' or len(g) != 4 + (struct.unpack_from('<H', g, 2)[0] + 1) * 72
                or y[:2] != b'J6'):
            raise ValueError()
    except (ValueError, struct.error):
        raise DatabaseError('JCSD 结构数量、版本、完整文件边界或末尾哨兵无效。') from None
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.with_name(output.name + '.building')
    if stage.exists():
        raise DatabaseError('结构构建暂存文件已存在。')
    connection = sqlite3.connect(stage)
    try:
        connection.executescript('''
            PRAGMA journal_mode=DELETE;
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE structures(code TEXT PRIMARY KEY,accession INTEGER UNIQUE NOT NULL,index_no INTEGER UNIQUE NOT NULL,
                compound TEXT NOT NULL,mineral TEXT,formula TEXT,site_count INTEGER NOT NULL,unsupported TEXT NOT NULL);
            CREATE TABLE records(code TEXT PRIMARY KEY,csa_start INTEGER NOT NULL,csc_start INTEGER NOT NULL,
                csd BLOB NOT NULL,csa BLOB NOT NULL,csc BLOB NOT NULL,jade_j INTEGER NOT NULL);
            CREATE TABLE settings(jade_j INTEGER PRIMARY KEY,sgc_start INTEGER NOT NULL,sym_start INTEGER NOT NULL,sgc BLOB NOT NULL,sym BLOB NOT NULL);
        ''')
        unsupported = 0
        reasons = {}
        accession_previous = 0
        atom_count = 0
        settings = {}
        for index in range(count):
            record = d[1024 + index * 124:1024 + (index + 1) * 124]
            accession, ap, cp, site_count = struct.unpack_from('<4I', record)
            ap, cp = ap - 1, cp - 1
            ae = struct.unpack_from('<I', d, 1024 + (index + 1) * 124 + 4)[0] - 1 if index + 1 < count else len(a)
            ce = struct.unpack_from('<I', d, 1024 + (index + 1) * 124 + 8)[0] - 1 if index + 1 < count else len(c)
            if (accession <= accession_previous or not 1024 <= ap < ae <= len(a) or not 1024 <= cp < ce <= len(c)
                    or (index == 0 and (ap != 1024 or cp != 1024))):
                raise DatabaseError('结构原始编号或记录偏移不连续、有重复或越界。')
            accession_previous = accession
            jade_j = struct.unpack_from('<H', record, 16)[0]
            if jade_j not in settings:
                sgc_start = 4 + jade_j * 72
                sgc = g[sgc_start:sgc_start + 72]
                if len(sgc) != 72:
                    raise DatabaseError('结构引用的空间群表项越界。')
                sym_start = struct.unpack_from('<I', sgc)[0] - 1
                if sym_start < 2 or sym_start + 2 > len(y):
                    raise DatabaseError('结构原始对称操作偏移越界。')
                length = 2 + (struct.unpack_from('<h', y, sym_start)[0] + 1) * 24
                sym = y[sym_start:sym_start + length]
                decode_setting(sgc, sym)
                settings[jade_j] = sgc, sym
                connection.execute('INSERT INTO settings VALUES (?,?,?,?,?)', (jade_j, sgc_start, sym_start, sgc, sym))
            sgc, sym = settings[jade_j]
            payload = decode_payload(record, a[ap:ae], c[cp:ce], sgc, sym, index)
            if payload['unsupported_reasons']:
                unsupported += 1
                for reason in payload['unsupported_reasons']:
                    reasons[reason] = reasons.get(reason, 0) + 1
            code = canonical(accession)
            connection.execute('INSERT INTO structures VALUES (?,?,?,?,?,?,?,?)',
                               (code, accession, index, payload['compound_name'], payload['mineral_name'],
                                payload['chemical_formula'], site_count, _json(payload['unsupported_reasons'])))
            connection.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?)',
                               (code, ap, cp, record, a[ap:ae], c[cp:ce], jade_j))
            atom_count += site_count
        if accession_previous != maximum:
            raise DatabaseError('结构最大原始编号与源文件头不一致。')
        metadata = {'format': FORMAT, 'raw_format': RAW_FORMAT, 'structure_snapshot': manifest,
                    'structure_count': count, 'unsupported_count': unsupported,
                    'unsupported_reasons': reasons, 'site_count': atom_count, 'setting_count': len(settings)}
        connection.executemany('INSERT INTO metadata VALUES (?,?)', [(key, _json(value)) for key, value in metadata.items()])
        connection.executescript('CREATE INDEX structures_compound ON structures(compound COLLATE BINARY);'
                                 'CREATE INDEX structures_mineral ON structures(mineral COLLATE BINARY);'
                                 'CREATE INDEX structures_formula ON structures(formula COLLATE BINARY);')
        connection.commit()
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise DatabaseError('结构派生库完整性检查失败。')
        if any((directory / name).is_symlink() or _signature(directory / name) != signature
               for name, signature in signatures.items()):
            raise DatabaseError('结构快照文件在构建期间变化，拒绝发布派生库。')
        connection.close()
        connection = None
        stage.replace(output)
        return {'status': 'passed', 'format': FORMAT, 'sha256': file_sha(output), 'bytes': output.stat().st_size,
                'structure_count': count, 'supported_count': count - unsupported, 'unsupported_count': unsupported,
                'unsupported_reasons': reasons, 'site_count': atom_count, 'setting_count': len(settings),
                'source_content_fingerprint_sha256': manifest['content_fingerprint_sha256']}
    finally:
        if connection is not None:
            connection.close()
        if stage.exists():
            stage.unlink()
