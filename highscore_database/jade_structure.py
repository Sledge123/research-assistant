"""JADE JCSD 结构记录的只读读取；显式保存原 setting 与完整对称操作。"""
from contextlib import contextmanager
import base64
import csv
from fractions import Fraction
from functools import lru_cache
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import struct
import threading
import time

from .reader import CardMissing, DatabaseError, QueryTooBroad

FORMAT = 'jade_icsd_structure_v1'
RAW_FORMAT = 'jade_structure_card_records_v1'
SOURCE_NAMES = {'ICSD-All.csa', 'ICSD-All.csb', 'ICSD-All.csc', 'ICSD-All.csd', 'jade9.SGC', 'jade9.SYM'}
ELEMENTS = ('X H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn '
            'Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce '
            'Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn '
            'Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og').split()
AXIS_PERMUTATIONS = ((0, 1, 2), (2, 0, 1), (1, 2, 0), (0, 2, 1), (1, 0, 2), (2, 1, 0))


def canonical(accession):
    if type(accession) is not int or not 1 <= accession <= 999999:
        raise DatabaseError('ICSD 原始结构编号超出已核验范围。')
    return f'ICSD-{accession:06d}'


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def manifest_snapshot(manifest_bytes):
    try:
        source = json.loads(manifest_bytes.decode('utf-8-sig'))
        if source.get('type') != 'JADE_STRUCTURE_SNAPSHOT':
            raise ValueError()
        files = {}
        for row in source['files']:
            name, size, digest = row['name'], row['bytes'], row['sha256']
            if (name not in SOURCE_NAMES or name in files or type(size) is not int or size < 2
                    or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)):
                raise ValueError()
            files[name] = {'name': name, 'bytes': size, 'sha256': digest}
        if set(files) != SOURCE_NAMES:
            raise ValueError()
        digest = hashlib.sha256()
        for name, row in sorted(files.items()):
            digest.update(name.encode('utf-8') + b'\0' + bytes.fromhex(row['sha256']))
        return {'manifest_base64': base64.b64encode(manifest_bytes).decode('ascii'),
                'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                'content_fingerprint_sha256': digest.hexdigest(), 'files': list(files.values())}
    except (ValueError, TypeError, KeyError, UnicodeError, AttributeError):
        raise DatabaseError('JADE 结构快照清单不完整或无效。') from None


def _det(rotation):
    a, b, c, d, e, f, g, h, i = rotation
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _compose(left, right):
    a, t = left[:9], left[9:]
    b, u = right[:9], right[9:]
    return tuple(sum(a[i * 3 + k] * b[k * 3 + j] for k in range(3)) for i in range(3) for j in range(3)) + tuple((sum(a[i * 3 + k] * u[k] for k in range(3)) + t[i]) % 24 for i in range(3))


def _operation_text(operation):
    parts = []
    for i in range(3):
        text = ''
        for coefficient, axis in zip(operation[i * 3:i * 3 + 3], 'xyz'):
            if coefficient:
                text += ('+' if coefficient > 0 and text else '') + ('-' if coefficient < 0 else '') + (str(abs(coefficient)) if abs(coefficient) != 1 else '') + axis
        translation = operation[9 + i]
        if translation:
            text += ('+' if text else '') + str(Fraction(translation, 24))
        parts.append(text or '0')
    return ','.join(parts)


@lru_cache(maxsize=700)
def _decode_setting_cached(sgc, sym):
    """原表的轴置换、反演、点阵平移全部展开；不使用默认 setting。"""
    try:
        if len(sgc) != 72 or len(sym) < 26:
            raise ValueError()
        values = struct.unpack('<24h', sgc[:48])
        count = struct.unpack_from('<h', sym)[0] + 1
        if (not 1 <= count <= 192 or len(sym) != 2 + count * 24 or values[9] != count - 1
                or values[7] not in (0, 1) or not 1 <= values[8] <= 230
                or not 0 <= values[4] < 6 or not 1 <= values[5] <= 192):
            raise ValueError()
        symbol = sgc[48:56].decode('ascii').strip()
        setting = sgc[56:72].decode('ascii').strip()
        centres = [(0, 0, 0)]
        factor, remainder = divmod(values[5], count * (values[7] + 1))
        if remainder:
            raise ValueError()
        if factor != 1:
            extra = {'A': [(0, 12, 12)], 'B': [(12, 0, 12)], 'C': [(12, 12, 0)],
                     'I': [(12, 12, 12)], 'F': [(0, 12, 12), (12, 0, 12), (12, 12, 0)]}.get(symbol[0])
            if symbol[0] == 'R':
                vector = tuple(Fraction(value) * 24 for value in setting.split(','))
                if len(vector) != 3 or any(value.denominator != 1 for value in vector):
                    raise ValueError()
                extra = [tuple(map(int, vector)), tuple(int(value * 2) % 24 for value in vector)]
            if extra is None:
                raise ValueError()
            centres += extra
        if len(centres) != factor:
            raise ValueError()
        permutation = AXIS_PERMUTATIONS[values[4]]
        operations = []
        for index in range(count):
            row = struct.unpack_from('<12h', sym, 2 + index * 24)
            rotation = [row[i * 4 + 1:i * 4 + 4] for i in range(3)]
            translation = []
            for i in range(3):
                packed = row[i * 4]
                numerator, denominator = packed & 255, packed >> 8
                if packed and (denominator <= 0 or numerator >= denominator or 24 % denominator):
                    raise ValueError()
                translation.append(numerator * 24 // denominator if packed else 0)
            rotation = tuple(rotation[i][j] for i in permutation for j in permutation)
            translation = tuple(translation[i] for i in permutation)
            if _det(rotation) not in (-1, 1):
                raise ValueError()
            for inverted in range(values[7] + 1):
                sign = -1 if inverted else 1
                for centre in centres:
                    operations.append(tuple(sign * value for value in rotation) + tuple((sign * translation[i] + centre[i]) % 24 for i in range(3)))
        identity = (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)
        allowed = set(operations)
        if len(allowed) != values[5] or identity not in allowed:
            raise ValueError()
        # 以实际操作作为生成元扩张子群，所有乘积必须仍在完整原操作集中。
        generated, generators = {identity}, []
        for candidate in sorted(allowed):
            if candidate in generated:
                continue
            generators.append(candidate)
            pending = list(generated)
            while pending:
                current = pending.pop()
                for generator in generators:
                    product = _compose(current, generator)
                    if product not in allowed:
                        raise ValueError()
                    if product not in generated:
                        generated.add(product)
                        pending.append(product)
        if generated != allowed:
            raise ValueError()
        ordered = [identity] + sorted(allowed - {identity})
        return (values[8], symbol, setting, values[5], permutation,
                tuple(_operation_text(value) for value in ordered))
    except (ValueError, IndexError, struct.error, UnicodeError, ZeroDivisionError):
        raise DatabaseError('JADE 空间群 setting 或原始对称操作尚未完整核验。') from None


def decode_setting(sgc, sym):
    number, symbol, setting, multiplicity, permutation, operations = _decode_setting_cached(sgc, sym)
    return {'number': number, 'symbol': symbol, 'setting': setting,
            'general_multiplicity': multiplicity, 'axis_permutation': list(permutation),
            'symmetry_operations': list(operations)}


def decode_payload(csd, csa, csc, sgc, sym, index):
    try:
        if len(csd) != 124:
            raise ValueError()
        accession, _, _, count = struct.unpack_from('<4I', csd)
        canonical(accession)
        stored_accession, flags, stored_count = struct.unpack_from('<IHH', csa)
        if stored_accession != accession or stored_count != count or flags not in range(4) or not count or len(csa) != 8 + count * 108:
            raise ValueError()
        position, texts = 4, []
        if struct.unpack_from('<I', csc)[0] != accession:
            raise ValueError()
        for _ in range(4):
            length = struct.unpack_from('<H', csc, position)[0]
            position += 2
            end = csc.index(0, position)
            value = csc[position:end].decode('gbk', 'strict')
            if len(value) != length:
                raise ValueError()
            position = end + 1
            if struct.unpack_from('<I', csc, position)[0] != index + 1:
                raise ValueError()
            position += 4
            texts.append(value)
        if position != len(csc) or not texts[0].strip():
            raise ValueError()
        group = dict(decode_setting(sgc, sym))
        group['jade_j'] = struct.unpack_from('<H', csd, 16)[0]
        cell_values = struct.unpack_from('<6f', csd, 40)
        reasons = []
        if (not all(math.isfinite(value) for value in cell_values) or min(cell_values[:3]) <= 0
                or not all(0 < value < 180 for value in cell_values[3:])):
            reasons.append('晶胞长度或角度无效')
        else:
            ca, cb, cg = (math.cos(math.radians(value)) for value in cell_values[3:])
            if 1 + 2 * ca * cb * cg - ca * ca - cb * cb - cg * cg <= 1e-12:
                reasons.append('晶胞度量不产生正体积')
        sites, labels = [], set()
        for index_atom in range(count):
            offset = 8 + index_atom * 108
            atomic_number, charge, _, multiplicity = struct.unpack_from('<HhHH', csa, offset)
            occupancy = struct.unpack_from('<f', csa, offset + 8)[0]
            coordinates = struct.unpack_from('<3f', csa, offset + 32)
            label_raw = csa[offset + 104:offset + 108].decode('ascii', 'strict').rstrip(' \0')
            if not 1 <= atomic_number < len(ELEMENTS):
                reasons.append('元素序数未核验')
                element = '?'
            else:
                element = ELEMENTS[atomic_number]
            if not math.isfinite(occupancy) or not all(math.isfinite(value) for value in coordinates):
                reasons.append('原子坐标或占有率不是有限数值')
            elif not 0 <= occupancy <= 1:
                reasons.append('原始原子占有率超出0到1，暂不支持导出')
            if not 1 <= multiplicity <= group['general_multiplicity'] or group['general_multiplicity'] % multiplicity:
                reasons.append('原始位点重数未核验')
            label = re.sub(r'[^A-Za-z0-9_+-]', '', label_raw) or element + str(index_atom + 1)
            if label in labels:
                label += '_' + str(index_atom + 1)
            labels.add(label)
            sites.append({'index': index_atom + 1, 'label': label, 'label_raw': label_raw,
                          'element': element, 'atomic_number': atomic_number, 'charge': charge,
                          'multiplicity': multiplicity, 'occupancy': occupancy,
                          **dict(zip(('x', 'y', 'z'), coordinates))})
        mineral = None
        match = re.search(r'Compound with mineral name:\s*([^\.]+)', texts[3])
        if match:
            mineral = match.group(1).strip()
        elif 'Compound with mineral name.' in texts[3]:
            mineral = texts[0].strip()
        return {'structure_id': accession, 'phase_name': texts[0].strip(), 'compound_name': texts[0].strip(),
                'mineral_name': mineral, 'chemical_formula': texts[1].split('\t')[0].strip() or None,
                'reference': texts[2], 'comment': texts[3], 'unsupported_reasons': sorted(set(reasons)),
                'structure': {'cell': dict(zip(('a', 'b', 'c', 'alpha', 'beta', 'gamma'), cell_values)),
                              'space_group': group, 'sites': sites, 'site_count': count}, 'site_count': count}
    except DatabaseError:
        raise
    except (ValueError, TypeError, IndexError, struct.error, UnicodeError):
        raise DatabaseError('JADE 结构身份、晶胞、原子记录或文本块边界无效。') from None


def record_part(name, blob, start, files):
    item = files[name]
    return {'file_name': name, 'byte_start_zero': start, 'byte_end_zero_exclusive': start + len(blob),
            'source_file_sha256': item['sha256'], 'source_file_bytes': item['bytes'],
            'base64': base64.b64encode(blob).decode('ascii')}


def card_from_records(records, snapshot, index, *, source, database_sha256):
    blobs = {key: base64.b64decode(value['base64'], validate=True) for key, value in records.items()}
    payload = decode_payload(*(blobs[key] for key in ('csd', 'csa', 'csc', 'sgc', 'sym')), index)
    if payload['unsupported_reasons']:
        raise DatabaseError('；'.join(payload['unsupported_reasons']))
    return {'code': canonical(payload['structure_id']), **payload, 'source': source,
            'data_type': 'jade_structure_data', 'content_type': 'structure', 'peaks': [], 'peak_count': 0,
            'complete': True, 'complete_structure_records': True, 'complete_original_peak_table': False,
            'database_id': 'jadestr-' + database_sha256[:48], 'database_sha256': database_sha256,
            'database_raw': {'format': RAW_FORMAT, 'reader_profile': FORMAT, 'record_index_zero': index,
                             'snapshot': snapshot, 'records': records}}


def validate_structure_card(card):
    try:
        raw = card['database_raw']
        if raw['format'] != RAW_FORMAT or raw['reader_profile'] != FORMAT:
            raise ValueError()
        snapshot = raw['snapshot']
        if manifest_snapshot(base64.b64decode(snapshot['manifest_base64'], validate=True)) != snapshot:
            raise ValueError()
        sha = card['database_sha256']
        if not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha):
            raise ValueError()
        index, records = raw['record_index_zero'], raw['records']
        if type(index) is not int or index < 0 or set(records) != {'csd', 'csa', 'csc', 'sgc', 'sym'}:
            raise ValueError()
        files = {item['name']: item for item in snapshot['files']}
        blobs = {}
        for key, part in records.items():
            name = 'ICSD-All.' + key if key in ('csd', 'csa', 'csc') else 'jade9.' + key.upper()
            item = files[name]
            start, end = part['byte_start_zero'], part['byte_end_zero_exclusive']
            blob = base64.b64decode(part['base64'], validate=True)
            if (part['file_name'] != name or part['source_file_sha256'] != item['sha256']
                    or part['source_file_bytes'] != item['bytes'] or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= item['bytes'] or end - start != len(blob)):
                raise ValueError()
            blobs[key] = blob
        if records['csd']['byte_start_zero'] != 1024 + index * 124 or len(blobs['csd']) != 124:
            raise ValueError()
        _, ap, cp, _ = struct.unpack_from('<4I', blobs['csd'])
        jade_j = struct.unpack_from('<H', blobs['csd'], 16)[0]
        if (records['csa']['byte_start_zero'] != ap - 1 or records['csc']['byte_start_zero'] != cp - 1
                or records['sgc']['byte_start_zero'] != 4 + jade_j * 72
                or records['sym']['byte_start_zero'] != struct.unpack_from('<I', blobs['sgc'])[0] - 1):
            raise ValueError()
        expected = card_from_records(records, snapshot, index, source=card['source'], database_sha256=sha)
        for key, value in expected.items():
            if type(card.get(key)) is not type(value) or card.get(key) != value:
                raise ValueError()
        return expected
    except DatabaseError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, struct.error):
        raise DatabaseError('JADE 结构原始记录、身份、坐标或对称操作复验失败。') from None


def _cif_string(value):
    value = str(value).replace('\r', '')
    if '\n' in value or ("'" in value and '"' in value):
        if any(line.startswith(';') for line in value.splitlines()):
            raise DatabaseError('原始文本包含无法安全写入 CIF 的分隔行。')
        return '\n;' + value + '\n;'
    return '"' + value + '"' if "'" in value else "'" + value + "'"


def _number(value):
    return format(value, '.9g')


def structure_cif(card):
    card = validate_structure_card(card)
    structure = card['structure']
    group = structure['space_group']
    lines = ['data_' + card['code'].replace('-', '_'),
             '# Converted from preserved JADE crystal-structure records.',
             '_database_code_ICSD ' + str(card['structure_id']),
             '_chemical_name_common ' + _cif_string(card['phase_name'])]
    if card['chemical_formula']:
        lines.append('_chemical_formula_structural ' + _cif_string(card['chemical_formula']))
    for key, value in structure['cell'].items():
        lines.append(('_cell_length_' if key in ('a', 'b', 'c') else '_cell_angle_') + key + ' ' + _number(value))
    lines += ['_space_group_IT_number ' + str(group['number']),
              '_space_group_name_H-M_alt ' + _cif_string(group['symbol']),
              '# JADE setting: ' + group['setting'], 'loop_', '_space_group_symop_id', '_space_group_symop_operation_xyz']
    lines += [str(index) + ' ' + _cif_string(value) for index, value in enumerate(group['symmetry_operations'], 1)]
    lines += ['loop_', '_atom_site_label', '_atom_site_type_symbol', '_atom_site_symmetry_multiplicity',
              '_atom_site_occupancy', '_atom_site_fract_x', '_atom_site_fract_y', '_atom_site_fract_z']
    lines += [' '.join((site['label'], site['element'], str(site['multiplicity']), *(_number(site[key]) for key in ('occupancy', 'x', 'y', 'z')))) for site in structure['sites']]
    return '\n'.join(lines) + '\n'


def structure_csv(card):
    card = validate_structure_card(card)
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    keys = ('index', 'label', 'element', 'atomic_number', 'charge', 'multiplicity', 'occupancy', 'x', 'y', 'z')
    writer.writerow(('序号', '位点', '元素', '原子序数', '电荷', '重数', '占有率', '分数坐标x', '分数坐标y', '分数坐标z'))
    for site in card['structure']['sites']:
        writer.writerow([_number(site[key]) if type(site[key]) is float else site[key] for key in keys])
    return stream.getvalue()


class JadeStructureReader:
    content_type = 'structure'

    def __init__(self, path, *, timeout=5, stop_event=None, profile=None, cache_directory=None):
        self.path = Path(path).resolve()
        self.cache_directory = cache_directory
        self.timeout = float(timeout)
        self.stop_event = stop_event or threading.Event()
        if not self.path.is_file() or self.path.is_symlink() or not 0 < self.timeout <= 60:
            raise DatabaseError('JADE 结构派生库路径或超时无效。')
        if any(Path(str(self.path) + suffix).exists() for suffix in ('-wal', '-journal')):
            raise DatabaseError('JADE 结构派生库存在未合并事务。')
        self.signature = self._signature()
        digest = hashlib.sha256()
        with self.path.open('rb') as stream:
            if stream.read(16) != b'SQLite format 3\0':
                raise DatabaseError('JADE 结构派生库不是 SQLite。')
            stream.seek(0)
            while chunk := stream.read(1024 * 1024):
                self._check_stop()
                digest.update(chunk)
        self.sha256 = digest.hexdigest()
        if (not isinstance(profile, dict) or set(profile) != {'format', 'source', 'sha256'}
                or profile['format'] != FORMAT or profile['sha256'] != self.sha256
                or not isinstance(profile['source'], str) or not profile['source'].strip() or len(profile['source']) > 300):
            raise DatabaseError('JADE 结构配置必须明确格式、来源并绑定 SHA256。')
        self.source = profile['source'].strip()
        self.reader_profile = FORMAT
        self.database_id = 'jadestr-' + self.sha256[:48]
        with self.connection() as connection:
            required = {'metadata': {'key', 'value'}, 'structures': {'code', 'accession', 'index_no', 'compound', 'mineral', 'formula', 'site_count', 'unsupported'},
                        'records': {'code', 'csa_start', 'csc_start', 'csd', 'csa', 'csc', 'jade_j'},
                        'settings': {'jade_j', 'sgc_start', 'sym_start', 'sgc', 'sym'}}
            for name, fields in required.items():
                kind = connection.execute('SELECT type FROM sqlite_master WHERE name=?', (name,)).fetchone()
                if kind is None or kind[0] != 'table' or not fields.issubset({row['name'] for row in connection.execute(f'PRAGMA table_info("{name}")')}):
                    raise DatabaseError('JADE 结构派生库表结构无效。')
            rows = connection.execute('SELECT key,value FROM metadata').fetchall()
            metadata = {row['key']: json.loads(row['value']) for row in rows}
            self.snapshot = metadata['structure_snapshot']
            if (len(metadata) != len(rows) or metadata['format'] != FORMAT or metadata['raw_format'] != RAW_FORMAT
                    or manifest_snapshot(base64.b64decode(self.snapshot['manifest_base64'], validate=True)) != self.snapshot):
                raise DatabaseError('JADE 结构派生库清单或元数据无效。')
            self.pattern_count = connection.execute('SELECT count(*) FROM structures').fetchone()[0]
            self.unsupported_count = connection.execute("SELECT count(*) FROM structures WHERE unsupported!='[]'").fetchone()[0]
            files = {item['name']: item for item in self.snapshot['files']}
            if (self.pattern_count != metadata['structure_count'] or self.unsupported_count != metadata['unsupported_count']
                    or connection.execute('SELECT count(*) FROM records').fetchone()[0] != self.pattern_count
                    or files['ICSD-All.csd']['bytes'] != 1028 + self.pattern_count * 124
                    or files['ICSD-All.csb']['bytes'] != 1024 + self.pattern_count * 8):
                raise DatabaseError('JADE 结构派生库源记录覆盖不完整。')

    def _signature(self):
        stat = self.path.stat()
        return stat.st_size, stat.st_mtime_ns

    def _check_stop(self):
        if self.stop_event.is_set():
            raise DatabaseError('结构查询任务已停止。')

    @contextmanager
    def connection(self):
        self._check_stop()
        if self.signature != self._signature():
            raise DatabaseError('JADE 结构派生库已变化，必须重新核验。')
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
            if self.signature != self._signature():
                raise DatabaseError('结构数据库读取期间发生变化。')
        except sqlite3.Error:
            raise DatabaseError('JADE 结构只读查询失败、停止或超时。') from None
        finally:
            if connection is not None:
                connection.close()

    def metadata(self):
        return {'database_id': self.database_id, 'sha256': self.sha256, 'source': self.source,
                'reader_profile': FORMAT, 'format_version': 1, 'pattern_count': self.pattern_count,
                'structure_count': self.pattern_count, 'supported_structure_count': self.pattern_count - self.unsupported_count,
                'unsupported_structure_count': self.unsupported_count, 'data_type': 'jade_structure_data', 'content_type': 'structure',
                'source_content_fingerprint_sha256': self.snapshot['content_fingerprint_sha256']}

    def search(self, mode, query):
        field = {'compound_name': 'compound', 'mineral_name': 'mineral', 'formula': 'formula'}.get(mode)
        if field is None or not isinstance(query, str) or not query.strip() or len(query) > 200 or '\0' in query:
            raise DatabaseError('请输入不超过200字的原文物相、矿物名或化学式。')
        with self.connection() as connection:
            rows = connection.execute(f'SELECT code,compound,formula FROM structures WHERE {field}=? COLLATE BINARY ORDER BY accession LIMIT 101', (query.strip(),)).fetchall()
        if len(rows) > 100:
            raise QueryTooBroad('检索结果超过100条，请细化条件。')
        return [{'code': row['code'], 'phase_name': row['compound'], 'chemical_formula': row['formula'], 'source': self.source} for row in rows]

    def _formula_batches(self):
        last = 0
        while True:
            with self.connection() as connection:
                rows = connection.execute('SELECT accession,code,formula FROM structures WHERE accession>? ORDER BY accession LIMIT 2000', (last,)).fetchall()
            if not rows:
                return
            yield [(row['code'], (row['formula'],)) for row in rows]
            last = rows[-1]['accession']

    def browse(self, mode, query):
        from highscore_downloader.numbers import parse_codes
        from .formula import chunks, formula_record_ids
        rows = []
        if mode == 'codes':
            try:
                requests = parse_codes(query)
            except ValueError as error:
                raise DatabaseError(str(error)) from None
            codes = [request.canonical_hint for request in requests if request.canonical_hint and request.canonical_hint.startswith('ICSD-')]
            if codes:
                with self.connection() as connection:
                    rows = connection.execute('SELECT code,compound,mineral,formula,site_count,unsupported FROM structures WHERE code IN ('+','.join('?' for _ in codes)+') ORDER BY code', codes).fetchall()
        elif mode == 'formula':
            for batch in chunks(formula_record_ids(self, query)):
                with self.connection() as connection:
                    rows.extend(connection.execute('SELECT code,compound,mineral,formula,site_count,unsupported FROM structures WHERE code IN ('+','.join('?' for _ in batch)+') ORDER BY code', batch).fetchall())
        else:
            raise DatabaseError('检索方式必须为编号或化学式。')
        candidates = {}
        for row in rows:
            try:
                reasons = json.loads(row['unsupported'])
                if not isinstance(reasons, list) or any(not isinstance(reason, str) or not reason for reason in reasons):
                    raise ValueError()
            except (ValueError, TypeError):
                raise DatabaseError('结构候选的支持状态字段无效。') from None
            candidates[row['code']] = {'code':row['code'], 'phase_name':row['compound'], 'chemical_formula':row['formula'],
                'source':self.source, 'mineral_name':row['mineral'], 'space_group':None, 'cell_parameters':None,
                'peak_count':None, 'site_count':row['site_count'], 'content_type':'structure',
                'exportable':not reasons, 'support_message':'；'.join(reasons) if reasons else None}
        return [candidates[code] for code in sorted(candidates)]

    def card(self, code):
        match = re.fullmatch(r'(?i)ICSD-([0-9]{1,6})', code.strip()) if isinstance(code, str) else None
        if not match or not 1 <= int(match.group(1)) <= 999999:
            raise CardMissing('未发现该卡片信息')
        code = canonical(int(match.group(1)))
        with self.connection() as connection:
            row = connection.execute('SELECT s.index_no,s.unsupported,r.* FROM structures s JOIN records r USING(code) WHERE s.code=?', (code,)).fetchone()
            if row is None:
                raise CardMissing('未发现该卡片信息')
            if row['unsupported'] != '[]':
                raise DatabaseError('；'.join(json.loads(row['unsupported'])))
            setting = connection.execute('SELECT * FROM settings WHERE jade_j=?', (row['jade_j'],)).fetchone()
        if setting is None:
            raise DatabaseError('结构缺少原始空间群记录。')
        files = {item['name']: item for item in self.snapshot['files']}
        parts = {key: record_part('ICSD-All.' + key, row[key], 1024 + row['index_no'] * 124 if key == 'csd' else row[key + '_start'], files) for key in ('csd', 'csa', 'csc')}
        parts.update({key: record_part('jade9.' + key.upper(), setting[key], setting[key + '_start'], files) for key in ('sgc', 'sym')})
        card = card_from_records(parts, self.snapshot, row['index_no'], source=self.source, database_sha256=self.sha256)
        return validate_structure_card(card)
