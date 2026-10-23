"""JADE PDF-4+ 2009 索引派生库；原始索引记录与生成报告明确区分。"""
from contextlib import contextmanager
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import struct
import threading
import time

from highscore_downloader.numbers import parse_code
from .reader import CardMissing, DatabaseError, QueryTooBroad

FORMAT = 'jade_pdf4_2009_index_v1'
RAW_FORMAT = 'jade_index_card_records_v1'
FORMAT_V2 = 'jade_pdf4_2009_index_v2'
RAW_FORMAT_V2 = 'jade_index_card_records_v2'
SUPPORTED_FORMATS = {FORMAT: RAW_FORMAT, FORMAT_V2: RAW_FORMAT_V2}
V2_FIXED_RECORD_SIZES = {'zid': 60, 'cid': 44, 'lid': 132}
EXPECTED_CARDS = 279588
SUBSET_HEADERS = {
    'RDB-Inorganics': (42, 59), 'RDB-Organics': (43, 59),
    'RDB-Minerals': (44, 59), 'RDB-Metallics': (45, 59),
    'RDB-ICSD-ALL': (46, 89), 'RDB-ICSD-MIN': (47, 89),
    'RDB-NIST-ALL': (48, 66), 'RDB-Deleted': (49, 89),
    'RDB-LP-File': (50, 11), 'RDB-LP-Mins': (51, 11),
}
SNAPSHOT_NAMES = {f'{prefix}.{extension}' for prefix in SUBSET_HEADERS
                  for extension in ('cid', 'fid', 'lid', 'rid', 'sid', 'xid', 'zid')}
SNAPSHOT_NAMES.update(f'{prefix}.{extension}' for prefix in
                      ('RDB-Minerals', 'RDB-ICSD-MIN', 'RDB-LP-Mins') for extension in ('mid', 'who'))
SNAPSHOT_NAMES.update({'RDB-Minerals.grp', *(f'RDB-PLU2009.{ext}' for ext in ('idx', 'mcc', 'mgs', 'run'))})


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def canonical(number):
    if type(number) is not int or not 0 <= number < 1000000000:
        raise DatabaseError('索引卡号超出已核验范围。')
    return f'{number // 10000000:02d}-{number // 10000 % 1000:03d}-{number % 10000:04d}'


def manifest_snapshot(manifest_bytes):
    """验证清单形状和完整集合；构建器另将每个文件字节与这些摘要核对。"""
    try:
        manifest = json.loads(manifest_bytes.decode('utf-8'))
        if manifest.get('type') != 'JADE_INDEX_SNAPSHOT' or not isinstance(manifest['files'], list):
            raise ValueError()
        files = {}
        for item in manifest['files']:
            name, size, digest = item['name'], item['bytes'], item['sha256']
            if (name not in SNAPSHOT_NAMES or name in files or type(size) is not int or size < 0
                    or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)):
                raise ValueError()
            files[name] = {'name': name, 'bytes': size, 'sha256': digest}
        if set(files) != SNAPSHOT_NAMES:
            raise ValueError()
        fingerprint = hashlib.sha256()
        for name, item in sorted(files.items()):
            fingerprint.update(name.encode('utf-8') + b'\0' + bytes.fromhex(item['sha256']))
        return {'manifest_base64': base64.b64encode(manifest_bytes).decode('ascii'),
                'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                'content_fingerprint_sha256': fingerprint.hexdigest(),
                'files': list(files.values())}
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        raise DatabaseError('JADE 索引快照清单不完整或格式无效。') from None


def decode_sid(raw, index):
    try:
        nul = raw.find(b'\0', 2)
        if nul < 2 or nul + 7 != len(raw):
            raise ValueError()
        length = struct.unpack_from('<H', raw)[0]
        text = raw[2:nul].decode('gbk', 'strict')
        fields = text.split('\x01')
        sequence, flags = struct.unpack_from('<IH', raw, nul + 1)
        if len(fields) != 3 or length != len(text) + 5 or sequence != index + 1 or not fields[1].strip():
            raise ValueError()
        return fields, flags
    except (ValueError, UnicodeError, struct.error):
        raise DatabaseError('JADE 索引 SID 文本、长度、序号或边界无效。') from None


def decode_rid(raw, *, byte_start=0, collect=True):
    try:
        if len(raw) < 4:
            raise ValueError()
        scale, count = struct.unpack_from('<HH', raw)
        if scale == 0 or not 1 <= count <= 199:
            raise ValueError()
        position = 4
        previous = math.inf
        peaks = []
        for index in range(count):
            origin = position
            if position + 7 > len(raw):
                raise ValueError()
            distance, intensity, h = struct.unpack_from('<fHB', raw, position)
            if not math.isfinite(distance) or not 0 < distance <= previous:
                raise ValueError()
            previous = distance
            position += 7
            if h == 255:
                h = k = l = None
            else:
                if position + 2 > len(raw):
                    raise ValueError()
                h = h if h < 128 else h - 256
                k, l = struct.unpack_from('<bb', raw, position)
                position += 2
            if collect:
                peaks.append({'no': str(index + 1), 'h': None if h is None else str(h),
                              'k': None if k is None else str(k), 'l': None if l is None else str(l),
                              'd_angstrom': repr(distance), 'two_theta_deg': None,
                              'intensity_percent': None, 'intensity_raw': str(intensity),
                              'intensity_scale_raw': str(scale), 'database_index': index,
                              'd_float32_le_hex': raw[origin:origin + 4].hex(),
                              'rid_byte_start_zero': byte_start + origin,
                              'rid_byte_end_zero_exclusive': byte_start + position,
                              'raw_hex': raw[origin:position].hex()})
        if position != len(raw):
            raise ValueError()
        return scale, count, peaks
    except (ValueError, struct.error):
        raise DatabaseError('JADE 索引 RID 峰数、数值或记录边界无效。') from None


def decode_zid(raw):
    """读取已逐库核验的60字节ZID；不猜测质量、密度类别和未知标志。

    系统码以原始群号区分共用码5的三方/六方，其余已确认码来自全量
    索引与独立HSRDB同号记录交叉核对。码6仅保存原值，不推定晶系。
    偏移12的密度没有已确认的实验/计算分类，不能借另一来源补分类。
    """
    try:
        if not isinstance(raw, bytes) or len(raw) != 60:
            raise ValueError()
        values = struct.unpack('<11f', raw[8:52])
        if any(not math.isfinite(value) for value in values):
            raise ValueError()
        group = raw[52:60].decode('ascii').rstrip('\0 ').strip()
        if any(ord(value) < 32 for value in group):
            raise ValueError()
        number = raw[2] if 1 <= raw[2] <= 230 else None
        system = {1: 'A', 2: 'M', 3: 'O', 4: 'T', 7: 'C'}.get(raw[1])
        if raw[1] == 5 and number is not None:
            system = 'R' if 143 <= number <= 167 else 'H' if 168 <= number <= 194 else None
        def positive(value):
            return repr(value) if value > 0 else None
        return {'crystal_system_raw_u8': raw[1], 'crystal_system_code': system,
                'space_group_number_raw_u8': raw[2], 'space_group_number': number,
                'space_group': group or None, 'setting_raw_u8': raw[3],
                'quality_raw_u8': raw[0], 'z': positive(values[0]),
                'density_raw': positive(values[1]), 'rir': positive(values[2]),
                'cell_volume': positive(values[3]),
                'cell_parameters': {key: positive(value) for key, value in zip(
                    ('a', 'b', 'c', 'alpha', 'beta', 'gamma'), values[5:])},
                'uninterpreted_prefix_hex': raw[:8].hex(),
                'uninterpreted_float32_offset24': repr(values[4]),
                'raw_float32_le_hex': {str(offset): raw[offset:offset + 4].hex()
                                       for offset in range(8, 52, 4)}}
    except (ValueError, UnicodeError, struct.error):
        raise DatabaseError('JADE 索引 ZID 长度、晶体学数值或文本无效。') from None


def _record_part(name, raw, start, files):
    item = files[name]
    return {'file_name': name, 'source_file_sha256': item['sha256'], 'source_file_bytes': item['bytes'],
            'byte_start_zero': start, 'byte_end_zero_exclusive': start + len(raw),
            'base64': base64.b64encode(raw).decode('ascii')}


def card_from_records(code, records, snapshot, *, source, database_sha256):
    """原始记录生成索引卡；不把强度原值转成百分比，不虚构缺少的字段。"""
    first = records[0]
    index = first['index_zero_based']
    sid = base64.b64decode(first['sid']['base64'], validate=True)
    rid = base64.b64decode(first['rid']['base64'], validate=True)
    fields, _ = decode_sid(sid, index)
    scale, count, peaks = decode_rid(rid, byte_start=first['rid']['byte_start_zero'])
    compound = fields[2] or fields[1]
    mineral = fields[1] if fields[2] else None
    zid = decode_zid(base64.b64decode(first['zid']['base64'], validate=True)) if 'zid' in first else None
    return {'code': code, 'phase_name': compound, 'chemical_formula': fields[0],
            'compound_name': compound, 'mineral_name': mineral, 'source': source,
            'database_id': 'jadeidx-' + database_sha256[:48], 'database_sha256': database_sha256,
            'data_type': 'jade_index_data', 'complete': True, 'complete_index_peak_table': True,
            'complete_original_peak_table': False, 'possible_truncation': count == 199,
            'index_peak_count': count, 'peak_count': count, 'peaks': peaks,
            'intensity_header_scale_raw_u16': scale, 'wavelength_angstrom': None,
            'cell_parameters': zid['cell_parameters'] if zid is not None else dict.fromkeys(('a', 'b', 'c', 'alpha', 'beta', 'gamma')),
            'space_group': zid['space_group'] if zid is not None else None,
            'comments': None, 'literature': [], 'subfiles': [], 'empirical_formula': None,
            **({'index_metadata': zid} if zid is not None else {}),
            'database_raw': {'format': RAW_FORMAT_V2 if zid is not None else RAW_FORMAT,
                             'reader_profile': FORMAT_V2 if zid is not None else FORMAT,
                             'index_snapshot': snapshot, 'records': records}}


def validate_index_card(card):
    """无需挂载私有数据库，从单卡原字节复验；返回重建的索引卡。"""
    try:
        raw = card['database_raw']
        if raw.get('reader_profile') not in SUPPORTED_FORMATS or raw['format'] != SUPPORTED_FORMATS[raw['reader_profile']]:
            raise ValueError()
        v2 = raw['reader_profile'] == FORMAT_V2
        sha = card['database_sha256']
        if (not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha)
                or card['database_id'] != 'jadeidx-' + sha[:48]
                or not isinstance(card['source'], str) or not card['source'].strip()):
            raise ValueError()
        snapshot = raw['index_snapshot']
        original_manifest = base64.b64decode(snapshot['manifest_base64'], validate=True)
        if manifest_snapshot(original_manifest) != snapshot:
            raise ValueError()
        files = {item['name']: item for item in snapshot['files']}
        records = raw['records']
        if not isinstance(records, list) or not 1 <= len(records) <= 10:
            raise ValueError()
        identities = set()
        content = None
        code = card['code']
        if parse_code(code).canonical_hint != code:
            raise ValueError()
        for record in records:
            subset, index = record['subset'], record['index_zero_based']
            if subset not in SUBSET_HEADERS or subset in identities or type(index) is not int or index < 0:
                raise ValueError()
            identities.add(subset)
            blobs = {}
            extensions = (('xid', 12), ('sid', 6), ('rid', 6)) + (tuple((name, 6) for name in V2_FIXED_RECORD_SIZES) if v2 else ())
            if set(record) != {'subset', 'index_zero_based', *(name for name, _ in extensions)}:
                raise ValueError()
            for extension, header_size in extensions:
                part = record[extension]
                name = f'{subset}.{extension}'
                entry = files[name]
                blob = base64.b64decode(part['base64'], validate=True)
                start, end = part['byte_start_zero'], part['byte_end_zero_exclusive']
                if (part['file_name'] != name or part['source_file_sha256'] != entry['sha256']
                        or part['source_file_bytes'] != entry['bytes'] or type(start) is not int or type(end) is not int
                        or not header_size <= start < end <= entry['bytes'] or end - start != len(blob)):
                    raise ValueError()
                blobs[extension] = blob
            if record['xid']['byte_start_zero'] != 12 + index * 16 or len(blobs['xid']) != 16:
                raise ValueError()
            number, sid1, rid1, source_id = struct.unpack('<IIII', blobs['xid'])
            if (canonical(number) != code or sid1 - 1 != record['sid']['byte_start_zero']
                    or rid1 - 1 != record['rid']['byte_start_zero']):
                raise ValueError()
            fields, flags = decode_sid(blobs['sid'], index)
            decode_rid(blobs['rid'], collect=False)
            if v2:
                for extension, size in V2_FIXED_RECORD_SIZES.items():
                    if record[extension]['byte_start_zero'] != 6 + index * size or len(blobs[extension]) != size:
                        raise ValueError()
                decode_zid(blobs['zid'])
            # GBK 原字节也必须一致；只排除由子集位置决定的局部序号。
            sid_nul = blobs['sid'].find(b'\0', 2)
            current = (blobs['sid'][:sid_nul + 1], flags, blobs['rid'], source_id,
                       blobs.get('zid'), blobs.get('cid'),
                       blobs['lid'][:2] + blobs['lid'][4:] if v2 else None)
            if content is not None and content != current:
                raise ValueError()
            content = current
        expected = card_from_records(code, records, snapshot, source=card['source'], database_sha256=sha)
        from .validation import same_values
        comparison = expected
        if card.get('verification', {}).get('report_version') == 6:
            from .exporter import prepare_card
            conversion = card['angular_conversion']
            if (conversion.get('method') != 'bragg_first_order'
                    or conversion.get('two_theta_origin') != 'calculated'
                    or conversion.get('wavelength_angstrom') != card.get('wavelength_angstrom')
                    or not isinstance(conversion.get('wavelength_source'), str)
                    or not conversion['wavelength_source'].strip()):
                raise ValueError()
            comparison = prepare_card(expected, {'wavelength_angstrom': conversion['wavelength_angstrom'],
                                                  'source': conversion['wavelength_source']})
        # 检查重建出的全部字段及嵌套类型；缺失元数据同样属于源记录的一部分。
        # 仅比较少量身份键会让自洽但虚构的空间群、晶胞和注释通过跨格式验收。
        for key, value in comparison.items():
            if key not in card or not same_values(card[key], value):
                raise ValueError()
        # 附加项只能是归档/排版回执，不能用 reference/comment/structure 等别名
        # 绕过原字段校验，再由共享模板读出未经原始索引支持的科学信息。
        if set(card) - set(comparison) - {'verification', 'report_fields', 'raw_text',
                                       'report_upgrade'}:
            raise ValueError()
        if comparison is expected and (card.get('wavelength_angstrom') is not None or card.get('angular_conversion') is not None):
            raise ValueError()
        return expected
    except DatabaseError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, struct.error):
        raise DatabaseError('JADE 索引卡身份、原始记录或峰表复验失败。') from None


class JadeIndexReader:
    content_type = 'diffraction'

    def __init__(self, path, *, timeout=5, stop_event=None, profile=None, cache_directory=None):
        self.path = Path(path).resolve()
        self.cache_directory = cache_directory
        self.stop_event = stop_event or threading.Event()
        self.timeout = float(timeout)
        if not self.path.is_file() or self.path.is_symlink() or not 0 < self.timeout <= 60:
            raise DatabaseError('JADE 索引派生库路径或超时配置无效。')
        if any(Path(str(self.path) + suffix).exists() for suffix in ('-wal', '-journal')):
            raise DatabaseError('JADE 索引派生库存在未合并事务。')
        self.signature = self._signature()
        digest = hashlib.sha256()
        with self.path.open('rb') as stream:
            if stream.read(16) != b'SQLite format 3\0':
                raise DatabaseError('JADE 索引派生库不是 SQLite。')
            stream.seek(0)
            while chunk := stream.read(1024 * 1024):
                self._check_stop()
                digest.update(chunk)
        self.sha256 = digest.hexdigest()
        if (not isinstance(profile, dict) or set(profile) != {'format', 'source', 'sha256'}
                or profile['format'] not in SUPPORTED_FORMATS or profile['sha256'] != self.sha256
                or not isinstance(profile['source'], str) or not profile['source'].strip() or len(profile['source']) > 300):
            raise DatabaseError('JADE 索引配置必须明确格式、来源并绑定派生库 SHA256。')
        self.source = profile['source'].strip()
        self.reader_profile = profile['format']
        self.database_id = 'jadeidx-' + self.sha256[:48]
        with self.connection() as connection:
            required = {'metadata': {'key', 'value'}, 'cards': {'code', 'suffix', 'formula', 'compound', 'mineral', 'peak_count'},
                        'records': {'code', 'subset', 'index_no', 'xid_start', 'sid_start', 'rid_start', 'xid', 'sid', 'rid'}}
            if self.reader_profile == FORMAT_V2:
                required['records'].update(name for extension in V2_FIXED_RECORD_SIZES
                                           for name in (extension + '_start', extension))
            for name, columns in required.items():
                kind = connection.execute('SELECT type FROM sqlite_master WHERE name=?', (name,)).fetchone()
                if kind is None or kind[0] != 'table' or not columns.issubset({row['name'] for row in connection.execute(f'PRAGMA table_info("{name}")')}):
                    raise DatabaseError('JADE 索引派生库表结构无效。')
            rows = connection.execute('SELECT key,value FROM metadata').fetchall()
            try:
                metadata = {row['key']: json.loads(row['value']) for row in rows}
            except (ValueError, TypeError):
                raise DatabaseError('JADE 索引派生库元数据 JSON 无效。') from None
            if (len(metadata) != len(rows) or metadata.get('format') != self.reader_profile
                    or metadata.get('raw_format') != SUPPORTED_FORMATS[self.reader_profile]):
                raise DatabaseError('JADE 索引派生库元数据无效。')
            try:
                self.snapshot = metadata['index_snapshot']
                if manifest_snapshot(base64.b64decode(self.snapshot['manifest_base64'], validate=True)) != self.snapshot:
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                raise DatabaseError('JADE 索引派生库原始清单无效。') from None
            self.pattern_count = connection.execute('SELECT count(*) FROM cards').fetchone()[0]
            records = connection.execute('SELECT count(*) FROM records').fetchone()[0]
            coverage = {row['subset']: row['n'] for row in connection.execute('SELECT subset,count(*) AS n FROM records GROUP BY subset')}
            files = {item['name']: item for item in self.snapshot['files']}
            expected_coverage = {name: (files[name + '.xid']['bytes'] - 12) // 16 for name in SUBSET_HEADERS}
            if (self.pattern_count != EXPECTED_CARDS or metadata.get('pattern_count') != self.pattern_count
                    or records != metadata.get('source_record_count') or coverage != expected_coverage
                    or metadata.get('subset_counts') != coverage):
                raise DatabaseError('JADE 索引派生库记录数或十子集覆盖不完整。')
            if (connection.execute('SELECT 1 FROM cards c WHERE NOT EXISTS (SELECT 1 FROM records r WHERE r.code=c.code) LIMIT 1').fetchone()
                    or connection.execute('SELECT 1 FROM records r WHERE NOT EXISTS (SELECT 1 FROM cards c WHERE c.code=r.code) LIMIT 1').fetchone()):
                raise DatabaseError('JADE 索引派生库含未关联的卡片或原始记录。')

    def _signature(self):
        stat = self.path.stat()
        return stat.st_size, stat.st_mtime_ns

    def _check_stop(self):
        if self.stop_event.is_set():
            raise DatabaseError('任务已停止，未继续读取 JADE 索引。')

    @contextmanager
    def connection(self):
        self._check_stop()
        if self._signature() != self.signature:
            raise DatabaseError('JADE 索引派生库已变化，必须重新核验。')
        connection = None
        deadline = time.monotonic() + self.timeout
        try:
            connection = sqlite3.connect(self.path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=self.timeout)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.set_progress_handler(lambda: int(self.stop_event.is_set() or time.monotonic() > deadline), 1000)
            yield connection
            self._check_stop()
            if self._signature() != self.signature:
                raise DatabaseError('读取期间 JADE 索引派生库已变化。')
        except sqlite3.Error:
            raise DatabaseError('JADE 索引只读查询失败、停止或超时。') from None
        finally:
            if connection is not None:
                connection.close()

    def metadata(self):
        return {'database_id': self.database_id, 'sha256': self.sha256, 'source': self.source,
                'reader_profile': self.reader_profile, 'format_version': 2 if self.reader_profile == FORMAT_V2 else 1,
                'pattern_count': self.pattern_count,
                'data_type': 'jade_index_data', 'content_type': self.content_type, 'complete_original_peak_table': False,
                'source_content_fingerprint_sha256': self.snapshot['content_fingerprint_sha256']}

    def _candidate(self, row):
        return {'code': row['code'], 'phase_name': row['compound'], 'chemical_formula': row['formula'], 'source': self.source}

    def search(self, mode, query):
        column = {'compound_name': 'compound', 'mineral_name': 'mineral', 'formula': 'formula'}.get(mode)
        if column is None or not isinstance(query, str) or not query.strip() or len(query) > 200 or '\0' in query:
            raise DatabaseError('请输入不超过 200 字的原文物相、矿物名或化学式。')
        with self.connection() as connection:
            rows = connection.execute(f'SELECT code,compound,formula FROM cards WHERE {column}=? COLLATE BINARY ORDER BY code LIMIT 101', (query.strip(),)).fetchall()
        if len(rows) > 100:
            raise QueryTooBroad('检索结果超过 100 条，请细化条件；未截断为完整结果。')
        return [self._candidate(row) for row in rows]

    def _formula_batches(self):
        last = ''
        while True:
            with self.connection() as connection:
                rows = connection.execute('SELECT code,formula FROM cards WHERE code>? ORDER BY code LIMIT 2000', (last,)).fetchall()
            if not rows:
                return
            yield [(row['code'], (row['formula'],)) for row in rows]
            last = rows[-1]['code']

    def browse(self, mode, query):
        from highscore_downloader.numbers import parse_codes
        from .formula import chunks, formula_record_ids
        rows = []
        if mode == 'codes':
            try:
                requests = parse_codes(query)
            except ValueError as error:
                raise DatabaseError(str(error)) from None
            for request in requests:
                if request.canonical_hint and not re.fullmatch(r'[0-9]{2}-[0-9]{3}-[0-9]{4}', request.canonical_hint):
                    continue
                field, value = ('code', request.canonical_hint) if request.canonical_hint else ('suffix', request.query)
                with self.connection() as connection:
                    rows.extend(connection.execute('SELECT code,compound,mineral,formula,peak_count FROM cards WHERE '+field+'=? ORDER BY code', (value,)).fetchall())
        elif mode == 'formula':
            for batch in chunks(formula_record_ids(self, query)):
                with self.connection() as connection:
                    rows.extend(connection.execute('SELECT code,compound,mineral,formula,peak_count FROM cards WHERE code IN ('+','.join('?' for _ in batch)+') ORDER BY code', batch).fetchall())
        else:
            raise DatabaseError('检索方式必须为编号或化学式。')
        candidates = {}
        for row in rows:
            item = self._candidate(row)
            zid = None
            if self.reader_profile == FORMAT_V2:
                with self.connection() as connection:
                    raw = connection.execute('SELECT zid FROM records WHERE code=? ORDER BY subset LIMIT 1', (row['code'],)).fetchone()
                if raw is None:
                    raise DatabaseError('JADE 索引候选缺少 ZID 原始记录。')
                zid = decode_zid(raw['zid'])
            item.update(mineral_name=row['mineral'], space_group=zid['space_group'] if zid else None,
                cell_parameters=zid['cell_parameters'] if zid else None,
                peak_count=row['peak_count'], site_count=None, content_type='diffraction',
                exportable=True, support_message=None)
            candidates[row['code']] = item
        return [candidates[code] for code in sorted(candidates)]

    def card(self, code):
        if isinstance(code, str) and re.match(r'(?i)^(?:ICSD|JCSD)[-:]', code.strip()):
            raise CardMissing('该编号属于结构数据来源。')
        request = parse_code(code)
        if request.canonical_hint and not re.fullmatch(r'[0-9]{2}-[0-9]{3}-[0-9]{4}', request.canonical_hint):
            raise CardMissing('该编号不是 PDF 卡片编号。')
        with self.connection() as connection:
            column, value = ('code', request.canonical_hint) if request.canonical_hint else ('suffix', request.query)
            matches = connection.execute(f'SELECT code FROM cards WHERE {column}=? LIMIT 2', (value,)).fetchall()
            if not matches:
                raise CardMissing('未发现该卡片信息')
            if len(matches) != 1:
                raise DatabaseError('短编号对应多张完整索引卡，请提供完整编号。')
            actual = matches[0]['code']
            rows = connection.execute('SELECT * FROM records WHERE code=? ORDER BY subset', (actual,)).fetchall()
        files = {item['name']: item for item in self.snapshot['files']}
        records = []
        for row in rows:
            record = {'subset': row['subset'], 'index_zero_based': row['index_no']}
            for extension in ('xid', 'sid', 'rid') + (tuple(V2_FIXED_RECORD_SIZES) if self.reader_profile == FORMAT_V2 else ()):
                record[extension] = _record_part(row['subset'] + '.' + extension, row[extension], row[extension + '_start'], files)
            records.append(record)
        if not records:
            raise DatabaseError('JADE 索引卡缺少原始记录。')
        card = card_from_records(actual, records, self.snapshot, source=self.source, database_sha256=self.sha256)
        return validate_index_card(card)
