"""化学式计量比及项目内派生检索索引；不改写来源数据库或原始化学式。"""
from collections import defaultdict
from contextlib import closing
from fractions import Fraction
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALGORITHM = 'element-ratio-v1'
DEFAULT_CACHE = PROJECT_ROOT / '.cache/formula-index'
ELEMENTS = frozenset('H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og'.split())
NUMBER = re.compile(r'(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)')
ELEMENT = re.compile(r'[A-Z][a-z]?')
_LOCKS = {}
_LOCK_GUARD = threading.Lock()


class FormulaError(ValueError):
    pass


class FormulaQueryError(FormulaError):
    """可向用户说明的查询歧义，区别于缓存或来源完整性错误。"""


def literal_formula(value):
    if not isinstance(value, str):
        return None
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


@lru_cache(maxsize=65536)
def formula_key(value):
    """只接受可确定的元素计数；小数点始终属于数字，不猜作水合分隔。"""
    text = literal_formula(value)
    if not text or len(text) > 1000 or '\0' in text:
        return None
    position = 0

    def number():
        nonlocal position
        match = NUMBER.match(text, position)
        if match is None:
            return Fraction(1)
        position = match.end()
        if len(match[0]) > 30:
            raise ValueError()
        result = Fraction(match[0])
        if result <= 0:
            raise ValueError()
        return result

    def group(end=None, depth=0):
        nonlocal position
        if depth > 20:
            raise ValueError()
        result = defaultdict(Fraction)
        terms = 0
        while position < len(text):
            char = text[position]
            if end and char == end:
                position += 1
                if not terms:
                    raise ValueError()
                return result
            if char in '·•!*':
                if end:
                    raise ValueError()
                break
            if char in '([{':
                position += 1
                nested = group({'(': ')', '[': ']', '{': '}'}[char], depth + 1)
                multiplier = number()
                for atom, count in nested.items():
                    result[atom] += count * multiplier
            else:
                match = ELEMENT.match(text, position)
                if match is None or match[0] not in ELEMENTS:
                    raise ValueError()
                position = match.end()
                result[match[0]] += number()
            terms += 1
        if end or not terms:
            raise ValueError()
        return result

    try:
        counts = defaultdict(Fraction)
        while position < len(text):
            multiplier = number()
            component = group()
            for atom, count in component.items():
                counts[atom] += count * multiplier
            if position < len(text):
                position += 1
                if position == len(text):
                    raise ValueError()
        denominator = math.lcm(*(value.denominator for value in counts.values()))
        integers = {atom: value.numerator * (denominator // value.denominator) for atom, value in counts.items()}
        divisor = math.gcd(*integers.values())
        return '|'.join(atom + ':' + str(integers[atom] // divisor) for atom in sorted(integers))
    except (ValueError, ZeroDivisionError, OverflowError, RecursionError):
        return None


def validate_formula_query(query):
    if not isinstance(query, str) or not query.strip() or len(query) > 200 or '\0' in query:
        raise FormulaError('请输入不超过200字的化学式。')
    return query.strip()


@lru_cache(maxsize=1024)
def formula_query_keys(query):
    """仅在查询写法不规范时枚举大小写；源记录解析仍严格区分元素。"""
    strict = formula_key(query)
    if strict is not None:
        return (strict,)
    text = literal_formula(query)
    if not text or len(text) > 200:
        return ()

    @lru_cache(maxsize=None)
    def expand(position):
        if position == len(text):
            return ('',)
        char = text[position]
        choices = []
        if char.isascii() and char.isalpha():
            for length in (1, 2):
                part = text[position:position + length]
                if len(part) != length or not part.isascii() or not part.isalpha():
                    continue
                symbol = part.capitalize()
                if symbol in ELEMENTS:
                    choices.append((symbol, position + length))
        elif char.isalpha():
            return ()
        else:
            choices.append((char, position + 1))
        result = set()
        for prefix, end in choices:
            for suffix in expand(end):
                result.add(prefix + suffix)
                if len(result) > 128:
                    raise FormulaQueryError('化学式大小写可能有多种解释，请使用规范元素写法，例如 MnO2 或 SiO2。')
        return tuple(sorted(result))

    return tuple(sorted({key for value in expand(0) if (key := formula_key(value)) is not None}))


def _ambiguous_query():
    raise FormulaQueryError('化学式大小写对应多种已有物质，请使用规范元素写法，例如 CO 和 Co 表示不同物质。')


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _signature(path):
    info = path.stat()
    return info.st_size, info.st_mtime_ns


def _cache_path(value):
    path = Path(value if value is not None else DEFAULT_CACHE).resolve()
    try:
        path.relative_to(PROJECT_ROOT)
    except ValueError:
        raise FormulaError('化学式缓存必须位于项目目录内。') from None
    for parent in (path, *path.parents):
        if parent == PROJECT_ROOT:
            break
        if parent.is_symlink():
            raise FormulaError('化学式缓存不允许符号链接。')
    path.mkdir(parents=True, exist_ok=True)
    return path


class FormulaIndex:
    """缓存只保存检索键和源主键；展示值始终回到只读原库获取。"""
    def __init__(self, reader, directory=None):
        self.reader = reader
        self.directory = _cache_path(directory)
        self.path = self.directory / (ALGORITHM + '-' + reader.sha256 + '.sqlite3')
        self.receipt = self.path.with_suffix('.json')
        self.signature = None
        self.receipt_signature = None
        with _LOCK_GUARD:
            self.lock = _LOCKS.setdefault(str(self.path), threading.Lock())

    def _check(self):
        self.reader._check_stop()
        if self.reader._signature() != self.reader.signature:
            raise FormulaError('来源数据库已变化，必须重新核验。')

    def _build(self):
        self._check()
        descriptor, filename = tempfile.mkstemp(prefix='.formula-', suffix='.sqlite3', dir=self.directory)
        os.close(descriptor)
        staging = Path(filename)
        receipt_staging = staging.with_suffix('.json')
        connection = None
        began = time.monotonic()
        try:
            connection = sqlite3.connect(staging)
            connection.execute('PRAGMA journal_mode=OFF')
            connection.execute('PRAGMA temp_store=MEMORY')
            connection.executescript('CREATE TABLE keys(kind TEXT,key TEXT,record_id TEXT,PRIMARY KEY(kind,key,record_id)) WITHOUT ROWID; CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);')
            count = 0
            unparsed = 0
            for batch in self.reader._formula_batches():
                self._check()
                if time.monotonic() - began > 300:
                    raise FormulaError('化学式索引构建超时，未返回不完整结果。')
                entries = []
                for record_id, values in batch:
                    count += 1
                    for formula in set(values):
                        literal = literal_formula(formula)
                        if not literal:
                            continue
                        entries.append(('literal', literal, str(record_id)))
                        key = formula_key(formula)
                        if key is not None:
                            entries.append(('ratio', key, str(record_id)))
                        else:
                            unparsed += 1
                connection.executemany('INSERT OR IGNORE INTO keys VALUES (?,?,?)', entries)
            self._check()
            if count != self.reader.pattern_count:
                raise FormulaError('化学式索引未覆盖全部源记录。')
            entries = connection.execute('SELECT count(*) FROM keys').fetchone()[0]
            metadata = {'algorithm': ALGORITHM, 'source_sha256': self.reader.sha256,
                        'source_records': count, 'entries': entries, 'unparsed_formula_fields': unparsed, 'complete': True}
            connection.executemany('INSERT INTO metadata VALUES (?,?)', [(key, json.dumps(value)) for key, value in metadata.items()])
            connection.commit()
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise FormulaError('化学式索引构建完整性核验失败。')
            connection.close()
            connection = None
            metadata.update(bytes=staging.stat().st_size, sha256=_hash(staging))
            receipt_staging.write_text(json.dumps(metadata, sort_keys=True) + '\n', encoding='utf-8')
            self._check()
            # 最终计算路径在发布前再次确认，缓存永远不写入数据目录。
            staging.resolve().relative_to(PROJECT_ROOT)
            self.path.resolve().relative_to(PROJECT_ROOT)
            receipt_staging.resolve().relative_to(PROJECT_ROOT)
            self.receipt.resolve().relative_to(PROJECT_ROOT)
            os.replace(staging, self.path)
            os.replace(receipt_staging, self.receipt)
        finally:
            if connection is not None:
                connection.close()
            for path in (staging, receipt_staging):
                if path.exists():
                    path.resolve().relative_to(PROJECT_ROOT)
                    path.unlink()

    def ensure(self):
        self._check()
        with self.lock:
            if self.signature is not None:
                if _signature(self.path) != self.signature or _signature(self.receipt) != self.receipt_signature:
                    raise FormulaError('化学式索引在运行期间发生变化。')
                return
            if not self.path.exists() and not self.receipt.exists():
                self._build()
            if not self.path.is_file() or not self.receipt.is_file() or self.path.is_symlink() or self.receipt.is_symlink():
                raise FormulaError('化学式索引或完整性回执缺失。')
            expected = json.loads(self.receipt.read_text(encoding='utf-8'))
            before = _signature(self.path)
            if (expected.get('algorithm') != ALGORITHM or expected.get('source_sha256') != self.reader.sha256
                    or expected.get('source_records') != self.reader.pattern_count or expected.get('complete') is not True
                    or expected.get('bytes') != before[0] or expected.get('sha256') != _hash(self.path)
                    or before != _signature(self.path)):
                raise FormulaError('化学式索引来源、完整性或文件摘要不匹配。')
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
                connection.execute('PRAGMA query_only=ON')
                if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise FormulaError('化学式索引完整性核验失败。')
                metadata = {key: json.loads(value) for key, value in connection.execute('SELECT key,value FROM metadata')}
                if (set(metadata) != set(expected) - {'bytes','sha256'}
                        or any(metadata.get(key) != expected.get(key) for key in metadata)
                        or metadata.get('entries') != connection.execute('SELECT count(*) FROM keys').fetchone()[0]):
                    raise FormulaError('化学式索引元数据或覆盖记录数不一致。')
            self.signature = before
            self.receipt_signature = _signature(self.receipt)

    def ids(self, query):
        query = validate_formula_query(query)
        self.ensure()
        self._check()
        began = time.monotonic()
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.set_progress_handler(lambda: int(self.reader.stop_event.is_set() or time.monotonic() - began > self.reader.timeout), 1000)
            keys = formula_query_keys(query)
            values = [('literal', literal_formula(query))]
            values.extend(('ratio', key) for key in keys)
            result = set()
            matched_keys = set()
            for kind, key in values:
                found = {row[0] for row in connection.execute('SELECT record_id FROM keys WHERE kind=? AND key=?', (kind, key))}
                if kind == 'ratio' and found:
                    matched_keys.add(key)
                result.update(found)
            if len(matched_keys) > 1:
                _ambiguous_query()
        self._check()
        if _signature(self.path) != self.signature or _signature(self.receipt) != self.receipt_signature:
            raise FormulaError('化学式索引读取期间发生变化。')
        return sorted(result)

    def matching_ratio_keys(self, keys):
        self.ensure()
        self._check()
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
            connection.execute('PRAGMA query_only=ON')
            found = {key for key in keys if connection.execute(
                "SELECT 1 FROM keys WHERE kind='ratio' AND key=? LIMIT 1", (key,)).fetchone()}
        self._check()
        if _signature(self.path) != self.signature or _signature(self.receipt) != self.receipt_signature:
            raise FormulaError('化学式索引读取期间发生变化。')
        return found


def resolved_formula_query(readers, query):
    """多来源在查询前共同排除大小写歧义，不能各自选择不同的元素解释。"""
    try:
        query = validate_formula_query(query)
        keys = formula_query_keys(query)
        if len(keys) <= 1:
            return query
        found = set()
        for reader in readers:
            if getattr(reader, '_formula_index', None) is None:
                reader._formula_index = FormulaIndex(reader, reader.cache_directory)
            found.update(reader._formula_index.matching_ratio_keys(keys))
            if len(found) > 1:
                _ambiguous_query()
        if found:
            return ''.join(atom + (count if count != '1' else '')
                           for atom, count in (item.split(':') for item in next(iter(found)).split('|')))
        return query
    except FormulaQueryError as error:
        from .reader import DatabaseError
        raise DatabaseError(str(error)) from None
    except (FormulaError, sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        from .reader import DatabaseError
        raise DatabaseError('化学式检索索引构建、完整性检查失败或查询已停止；未返回不完整结果。') from None


def formula_record_ids(reader, query):
    try:
        if getattr(reader, '_formula_index', None) is None:
            reader._formula_index = FormulaIndex(reader, reader.cache_directory)
        return reader._formula_index.ids(query)
    except FormulaQueryError as error:
        from .reader import DatabaseError
        raise DatabaseError(str(error)) from None
    except (FormulaError, sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        from .reader import DatabaseError
        raise DatabaseError('化学式检索索引构建、完整性检查失败或查询已停止；未返回不完整结果。') from None


def chunks(values, size=400):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]
