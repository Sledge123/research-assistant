"""有界文献流程及私有持久状态；导入本模块不会导入浏览器、联网或读取凭据。"""
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata

from .pdf import MAX_PDF_BYTES, normalize_title, title_matches, validate_pdf

RETENTION_SECONDS = 3 * 86400
STATE_DIRECTORY = 'output/literature-worker'
REFERENCE = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')


class LiteratureError(Exception):
    """只能传入固定、已脱敏的中文消息。"""
    def __init__(self, message='文献处理状态无法核验，请管理员检查。', state='needs_attention'):
        super().__init__(message)
        self.state = state


class LeaseLost(LiteratureError):
    def __init__(self):
        super().__init__('文献任务已取消、到期或租约失效，已停止站点操作。')


class SliceExpired(LiteratureError):
    def __init__(self):
        super().__init__('本轮处理时间已到，将在下一轮继续核验。', 'waiting')


def clean_doi(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise LiteratureError('论文 DOI 格式无法核验。')
    value = value.strip().removeprefix('https://doi.org/').removeprefix('http://doi.org/')
    if not re.fullmatch(r'10\.\d{4,9}/[^\s<>"?\x00-\x1f]{1,280}', value, re.I) or len(value) > 300:
        raise LiteratureError('论文 DOI 格式无法核验。')
    return value.lower()


def candidate_identity(value):
    """保存完整源候选身份；同题无 DOI 的期刊/年份不能在恢复中丢失。"""
    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not REFERENCE.fullmatch(value['id']):
        raise LiteratureError('所选论文的源候选身份无法核验。')
    title, journal, year = value.get('title'), value.get('journal'), value.get('year')
    if not isinstance(title, str) or not 6 <= len(normalize_title(title)) <= 1000:
        raise LiteratureError('所选论文题名无法核验。')
    if journal is not None and (not isinstance(journal, str) or not journal.strip() or len(journal) > 500):
        raise LiteratureError('所选论文期刊无法核验。')
    if year is not None and (type(year) is not int or not 1500 <= year <= 2200):
        raise LiteratureError('所选论文年份无法核验。')
    return {'id': value['id'], 'title': title.strip(), 'doi': clean_doi(value.get('doi')),
            'journal': ' '.join(journal.split()) if journal else None, 'year': year}


def identity_fingerprint(value):
    value = candidate_identity(value)
    value['title'] = normalize_title(value['title'])
    value['journal'] = normalize_title(value['journal'] or '')
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_selected_pdf(data, selected):
    """无 DOI 时还须由实际 PDF 出版页眉证实所选期刊及年份。"""
    if not selected or selected.get('doi'):
        return
    selected = candidate_identity(selected)
    if not selected['journal'] or not selected['year']:
        raise LiteratureError('所选论文没有 DOI 且缺少期刊或年份，无法区分同题论文。')
    from pypdf import PdfReader
    try:
        text = PdfReader(io.BytesIO(data), strict=True).pages[0].extract_text() or ''
    except Exception:
        raise LiteratureError('无法从实际 PDF 核验所选期刊与年份。') from None
    prefix = re.split(r'(?im)(?:^|\n)\s*(?:\d+[.\s]+)?(?:abstract|introduction|keywords|references|bibliography|摘要|引言|参考文献)\s*[:.\n]', text, maxsplit=1)[0]
    lines = [' '.join(line.split()) for line in unicodedata.normalize('NFKC', prefix[:4000]).splitlines() if line.strip()][:40]
    words = re.findall(r'\w+', unicodedata.normalize('NFKC', selected['journal']).casefold())
    journal_pattern = re.compile(r'^\s*'+r'[\W_]+'.join(re.escape(word) for word in words)+r'(?!\w)', re.I)
    year_pattern = re.compile(r'(?<!\d)'+str(selected['year'])+r'(?!\d)')
    for index, line in enumerate(lines):
        if normalize_title(line) == normalize_title(selected['title']):
            continue
        if journal_pattern.search(line) and year_pattern.search(line):
            return
        if normalize_title(line) == normalize_title(selected['journal']) and index+1 < len(lines):
            if re.fullmatch(r'(?:Year\s*[:：]?\s*)?\(?'+str(selected['year'])+r'\)?(?:\s*年)?', lines[index+1], re.I):
                return
    raise LiteratureError('PDF 首页出版页眉未证实所选期刊与年份，未采纳或返回下载。')


class StateStore:
    """发布意图先落盘；未知结果和已有请求绑定均不能被普通重试覆盖。"""
    def __init__(self, root, now=None):
        self.root = Path(root).resolve()
        self.now = now or time.time
        self.base = self.path(STATE_DIRECTORY)

    def path(self, relative):
        relative = Path(relative)
        if relative.is_absolute() or '..' in relative.parts:
            raise LiteratureError('执行器路径必须位于项目目录内。')
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise LiteratureError('执行器路径必须位于项目目录内。')
        for part in [path, *path.parents]:
            if part == self.root:
                break
            if part.is_symlink():
                raise LiteratureError('执行器路径包含符号链接，已停止写入。')
        return path

    def _directory(self, path):
        self.path(path.relative_to(self.root))
        path.mkdir(parents=True, exist_ok=True)
        current = path
        while current.is_relative_to(self.base):
            os.chmod(current, 0o700)
            if current == self.base:
                break
            current = current.parent

    @staticmethod
    def _key(title):
        key = normalize_title(title) if isinstance(title, str) else ''
        if not 6 <= len(key) <= 1000:
            raise LiteratureError('论文标题不足以核验身份。')
        return hashlib.sha256(key.encode('utf-8')).hexdigest()

    def entry(self, account_ref, title):
        if not isinstance(account_ref, str) or not REFERENCE.fullmatch(account_ref):
            raise LiteratureError('账号引用格式无效。')
        return self.path(f'{STATE_DIRECTORY}/accounts/{account_ref}/{self._key(title)}')

    def _atomic(self, path, data):
        self._directory(path.parent)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.pending-', delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self.path(path.relative_to(self.root))
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            if os.name != 'nt':
                descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError:
            raise LiteratureError('执行器私有状态写入失败，未执行后续站点操作。') from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _save(self, account_ref, title, record):
        target = self.entry(account_ref, title) / 'state.json'
        self._atomic(target, json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8'))

    def read(self, account_ref, title):
        target = self.entry(account_ref, title) / 'state.json'
        if not target.exists():
            return {'schema_version': 1, 'account_ref': account_ref, 'title_hash': self._key(title),
                    'canonical_title': title, 'phase': 'new'}
        try:
            if target.stat().st_size > 32768 or (os.name != 'nt' and target.stat().st_mode & 0o077):
                raise ValueError()
            record = json.loads(target.read_text(encoding='utf-8'))
            if (record['schema_version'] != 1 or record['account_ref'] != account_ref
                    or record['title_hash'] != self._key(title)
                    or normalize_title(record['canonical_title']) != normalize_title(title)
                    or record['phase'] not in {'new', 'publish_intent', 'waiting', 'download_verified', 'verified'}):
                raise ValueError()
            if record.get('external_request_id') and not REQUEST_ID.fullmatch(record['external_request_id']):
                raise ValueError()
            clean_doi(record.get('doi'))
            if record.get('metadata_identity'):
                selected = candidate_identity(record['metadata_identity'])
                if normalize_title(selected['title']) != normalize_title(title) or selected['doi'] != clean_doi(record.get('doi')):
                    raise ValueError()
            return record
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            raise LiteratureError('原文献状态无法校验，已停止自动发布以避免重复求助。') from None

    def write(self, account_ref, title, values):
        record = self.read(account_ref, title)
        allowed = {'phase', 'doi', 'external_request_id', 'intent_at', 'pdf_at', 'pdf_proof',
                   'verified_file_id', 'adopted', 'updated_at', 'metadata_identity', 'identity_proof',
                   'publish_baseline'}
        if set(values) - allowed:
            raise LiteratureError('执行器状态包含未允许字段。')
        external = values.get('external_request_id')
        if external is not None and (not isinstance(external, str) or not REQUEST_ID.fullmatch(external)):
            raise LiteratureError('科研通求助 ID 无法校验。')
        if record.get('external_request_id') and external is not None and record['external_request_id'] != external:
            raise LiteratureError('原科研通求助 ID 已绑定，禁止替换或重复发布。')
        if values.get('metadata_identity'):
            selected = candidate_identity(values['metadata_identity'])
            if record.get('metadata_identity') and identity_fingerprint(record['metadata_identity']) != identity_fingerprint(selected):
                raise LiteratureError('原求助已绑定另一源候选身份，禁止替换或复用全文。')
            values = {**values, 'metadata_identity': selected}
        record.update(values)
        record['updated_at'] = self.now()
        self._save(account_ref, title, record)
        return record

    def find(self, title, doi=None):
        records = []
        self.path(STATE_DIRECTORY)
        for target in self.base.glob('accounts/*/*/state.json'):
            self.path(target.relative_to(self.root))
            try:
                raw = json.loads(target.read_text(encoding='utf-8'))
                actual = raw['canonical_title']
                record = self.read(target.parent.parent.name, actual)
            except (OSError, ValueError, KeyError, TypeError, UnicodeError):
                raise LiteratureError('已有文献记录无法核验，已停止新发布。') from None
            if target != self.entry(record['account_ref'], actual) / 'state.json':
                raise LiteratureError('文献记录路径与身份不一致，已停止新发布。')
            if (normalize_title(actual) == normalize_title(title) or (doi and clean_doi(record.get('doi')) == doi)):
                records.append(record)
        return records

    def drop_pdf(self, account_ref, title):
        record = self.read(account_ref, title)
        for name in ('verified.pdf', 'incoming.pdf'):
            self.path((self.entry(account_ref, title) / name).relative_to(self.root)).unlink(missing_ok=True)
        for key in ('pdf_at', 'pdf_proof', 'verified_file_id', 'adopted', 'identity_proof'):
            record.pop(key, None)
        if record['phase'] in {'download_verified', 'verified'}:
            record['phase'] = 'waiting'
        self._save(account_ref, title, record)

    def cleanup(self):
        self.path(STATE_DIRECTORY)
        if not self.base.exists():
            return
        for store, account_ref, target in self.entries():
            try:
                raw = json.loads(target.read_text(encoding='utf-8'))
                record = store.read(account_ref, raw['canonical_title'])
                pdf_at = float(record['pdf_at']) if 'pdf_at' in record else None
                if pdf_at is not None and not math.isfinite(pdf_at):
                    continue
            except (OSError, ValueError, KeyError, TypeError, UnicodeError, LiteratureError):
                # 历史记录损坏不能阻断其他任务；实际使用某任务状态时仍严格拒绝损坏内容。
                continue
            if pdf_at is not None and self.now() - pdf_at >= RETENTION_SECONDS:
                store.drop_pdf(record['account_ref'], record['canonical_title'])
        # 浏览器中断可能留下无回执文件；它们也不能无限期留存。
        for target in self.base.rglob('*'):
            try:
                self.path(target.relative_to(self.root))
            except LiteratureError:
                continue
            # 常驻互斥锁必须保留同一 inode；清理时删除会允许第二个进程另建锁文件。
            if target == self.base/'worker.lock':
                continue
            if target.is_file() and target.name != 'state.json' and 'sessions' not in target.relative_to(self.base).parts:
                if self.now() - target.stat().st_mtime >= RETENTION_SECONDS:
                    target.unlink()

    def entries(self, title=None):
        """只按指定题名读取缓存候选；原账号历史与每任务状态分别解释，不混用绑定。"""
        key = self._key(title) if title is not None else '*'
        for pattern in (f'accounts/*/{key}/state.json', f'jobs/*/sources/*/accounts/*/{key}/state.json'):
            for target in self.base.glob(pattern):
                try:
                    self.path(target.relative_to(self.root))
                    parts = target.relative_to(self.base).parts
                    if parts[0] == 'accounts':
                        yield StateStore(self.root, now=self.now), parts[1], target
                    elif parts[3].isdigit():
                        yield TaskStateStore(self.root, parts[1], int(parts[3]), now=self.now), parts[5], target
                except (ValueError, LiteratureError):
                    continue

    def cached(self, account_ref, title, doi):
        record = self.read(account_ref, title)
        if 'pdf_at' not in record:
            return None
        if self.now() - float(record['pdf_at']) >= RETENTION_SECONDS:
            self.drop_pdf(account_ref, title)
            return None
        target = self.path((self.entry(account_ref, title) / 'verified.pdf').relative_to(self.root))
        try:
            if not target.is_file() or target.stat().st_size > MAX_PDF_BYTES:
                raise ValueError()
            data = target.read_bytes()
            proof = validate_pdf(data, title, doi)
            if proof != record.get('pdf_proof'):
                raise ValueError()
            selected = record.get('metadata_identity')
            if selected:
                validate_selected_pdf(data, selected)
                if not selected.get('doi'):
                    identity_proof = record.get('identity_proof', {})
                    if (identity_proof.get('fingerprint') != identity_fingerprint(selected)
                            or identity_proof.get('platform_verified') is not True
                            or identity_proof.get('pdf_sha256') != proof['sha256']):
                        raise LiteratureError('旧全文缺少所选源候选的完整核验记录，未返回下载。')
            return data
        except (OSError, ValueError):
            self.drop_pdf(account_ref, title)
            raise LiteratureError('已缓存全文的身份或校验值不一致，未返回下载。') from None

    def cache(self, account_ref, title, doi, data, file_id):
        try:
            proof = validate_pdf(data, title, doi)
        except ValueError:
            raise LiteratureError('应助 PDF 无法确认所求论文身份，未采纳或返回下载。') from None
        record = self.read(account_ref, title)
        selected = record.get('metadata_identity')
        validate_selected_pdf(data, selected)
        identity_proof = record.get('identity_proof', {})
        if selected and not selected.get('doi'):
            if identity_proof.get('platform_verified') is not True or identity_proof.get('fingerprint') != identity_fingerprint(selected):
                raise LiteratureError('平台未证实所选论文的期刊与年份，未缓存或返回全文。')
            identity_proof = {**identity_proof, 'pdf_sha256': proof['sha256']}
        path = self.entry(account_ref, title) / 'verified.pdf'
        self._atomic(path, data)
        return self.write(account_ref, title, {'phase': 'download_verified', 'pdf_at': self.now(),
            'pdf_proof': proof, 'verified_file_id': file_id, 'adopted': False,
            **({'identity_proof': identity_proof} if selected else {})})

    def session_path(self, account_ref):
        self.entry(account_ref, 'session-reference')
        return self.path(f'{STATE_DIRECTORY}/sessions/{account_ref}.json')

    def save_session(self, account_ref, state):
        self._atomic(self.session_path(account_ref), json.dumps(state, ensure_ascii=False).encode('utf-8'))

    def scan_path(self, account_ref):
        self.entry(account_ref, 'request-list-scan')
        return self.path(f'{STATE_DIRECTORY}/scans/{account_ref}.json')

    def read_scan(self, account_ref):
        target = self.scan_path(account_ref)
        if not target.exists():
            return None
        try:
            if target.stat().st_size > 24*1024*1024 or (os.name != 'nt' and target.stat().st_mode & 0o077):
                raise ValueError()
            value = json.loads(target.read_text(encoding='utf-8'))
            if (not isinstance(value, dict) or value.get('schema_version') != 1
                    or value.get('account_ref') != account_ref or not isinstance(value.get('started_at'), (int, float))):
                raise ValueError()
            if self.now()-value['started_at'] >= 6*3600:
                target.unlink()
                return None
            return value
        except (OSError, ValueError, TypeError, UnicodeError):
            raise LiteratureError('原账号的列表扫描游标无法核验，禁止新发布。') from None

    def save_scan(self, account_ref, value):
        value = {**value, 'schema_version': 1, 'account_ref': account_ref, 'updated_at': self.now()}
        self._atomic(self.scan_path(account_ref), json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))


class TaskStateStore(StateStore):
    """每个网站任务、获取轮次使用独立状态；账号会话仍是原独立账号的共享会话。"""
    def __init__(self, root, job_id, source_revision, now=None):
        super().__init__(root, now=now)
        if (not isinstance(job_id, str) or not REFERENCE.fullmatch(job_id)
                or type(source_revision) is not int or not 0 <= source_revision <= 1000000):
            raise LiteratureError('文献任务或获取轮次无法核验。')
        self.job_id, self.source_revision = job_id, source_revision

    def entry(self, account_ref, title):
        super().entry(account_ref, title)
        return self.path(f'{STATE_DIRECTORY}/jobs/{self.job_id}/sources/{self.source_revision}'
                         f'/accounts/{account_ref}/{self._key(title)}')

    def _save(self, account_ref, title, record):
        super()._save(account_ref, title, {**record, 'job_id': self.job_id,
                                         'source_revision': self.source_revision})

    def read(self, account_ref, title):
        record = super().read(account_ref, title)
        if self.entry(account_ref, title).joinpath('state.json').exists():
            if record.get('job_id') != self.job_id or record.get('source_revision') != self.source_revision:
                raise LiteratureError('本次取文状态与任务或获取轮次不一致。')
        return record

    def task_record(self):
        """首个服务器回调前也从本任务状态恢复账号与论文身份；获取轮次不改变它们。"""
        records = []
        base = self.path(f'{STATE_DIRECTORY}/jobs/{self.job_id}')
        for path in base.glob('sources/*/accounts/*/*/state.json'):
            try:
                parts = path.relative_to(base).parts
                previous = TaskStateStore(self.root, self.job_id, int(parts[1]), now=self.now)
                previous.path(path.relative_to(self.root))
                raw = json.loads(path.read_text(encoding='utf-8'))
                record = previous.read(parts[3], raw['canonical_title'])
                if path != previous.entry(parts[3], raw['canonical_title'])/'state.json':
                    raise ValueError()
                records.append((previous.source_revision, record))
            except (OSError, ValueError, KeyError, TypeError, UnicodeError):
                raise LiteratureError('本任务已有取文状态无法核验，未换账号或重新发布。') from None
        if not records:
            return None
        bindings = {(row['account_ref'], normalize_title(row['canonical_title']), clean_doi(row.get('doi')))
                    for _, row in records}
        selected = [row['metadata_identity'] for _, row in records if row.get('metadata_identity')]
        if len(bindings) != 1 or len({identity_fingerprint(value) for value in selected}) > 1:
            raise LiteratureError('本任务已有账号或论文身份冲突，未换账号或重新发布。')
        result = dict(max(records, key=lambda value: value[0])[1])
        if selected:
            result['metadata_identity'] = selected[0]
        return result


class LiteratureEngine:
    def __init__(self, root, accounts=None, resolver=None, browser_factory=None, now=None):
        self.store = StateStore(root, now=now)
        self.root = self.store.root
        self.accounts = accounts
        self.resolver = resolver
        self.browser_factory = browser_factory

    def __call__(self, job, guard):
        if 'source_revision' in job:
            from .task_engine import run_task
            return run_task(self, job, guard)
        identity = {}
        try:
            guard()
            self.store.cleanup()
            selected = candidate_identity(job['selected_candidate']) if job.get('selected_candidate') is not None else None
            title = selected['title'] if selected else job.get('canonical_title')
            doi = selected['doi'] if selected else clean_doi(job.get('doi'))
            if selected and job.get('canonical_title') and normalize_title(title or '') != normalize_title(job['canonical_title']):
                raise LiteratureError('所选候选与网站绑定题名不一致，已停止。')
            if not title:
                if self.resolver is None:
                    from .metadata import MetadataResolver
                    self.resolver = MetadataResolver()
                resolved = self.resolver.resolve(job['title'], guard=guard)
                if resolved.get('state'):
                    return resolved
                title, doi = resolved['canonical_title'], clean_doi(resolved.get('doi'))
                selected = candidate_identity(resolved['candidate']) if resolved.get('candidate') else None
            self.store._key(title)
            identity = {'canonical_title': title, 'doi': doi}
            accounts = self.accounts
            if accounts is None:
                from .credentials import load_accounts
                accounts = load_accounts(self.root)
            accounts = [item for item in accounts if item.get('enabled', True)]
            records = self.store.find(title, doi)
            if selected:
                for previous in records:
                    if previous.get('metadata_identity') and identity_fingerprint(previous['metadata_identity']) != identity_fingerprint(selected):
                        raise LiteratureError('同题原求助已绑定另一源候选身份，未复用全文或新发。')
            bound_ref = job.get('account_ref')
            if job.get('external_request_id') and not bound_ref:
                raise LiteratureError('已有科研通求助缺少原账号引用，禁止在其它账号新发。')
            if not bound_ref and records:
                refs = {item['account_ref'] for item in records}
                if len(refs) != 1 or len(records) != 1:
                    raise LiteratureError('同篇文献存在多个持久记录，需管理员核对原求助。')
                bound_ref = records[0]['account_ref']
                if not title_matches(title, records[0]['canonical_title']):
                    raise LiteratureError('同 DOI 已有不同题名的原求助，需核对原论文身份，未新发。')
                title = records[0]['canonical_title']
                identity['canonical_title'] = title
            account = next((item for item in accounts if item['account_ref'] == bound_ref), None) if bound_ref else next(iter(accounts), None)
            if account is None:
                raise LiteratureError('原科研通账号不可用，请管理员恢复账号配置。')
            ref = account['account_ref']
            identity['account_ref'] = ref
            record = self.store.read(ref, title)
            if selected is None and record.get('metadata_identity'):
                selected = candidate_identity(record['metadata_identity'])
            if selected:
                if not selected['doi'] and (not selected['journal'] or not selected['year']):
                    raise LiteratureError('所选论文没有 DOI 且缺少期刊或年份，无法区分同题论文。')
                if not record.get('metadata_identity') and record['phase'] != 'new' and not selected['doi']:
                    raise LiteratureError('原同题求助缺少源候选绑定，无法证明属于本次选择，未复用全文。')
            prior = record.get('external_request_id')
            external = job.get('external_request_id') or prior
            if external:
                identity['external_request_id'] = external
            if prior and external != prior:
                raise LiteratureError('网站任务与本地原求助绑定冲突，禁止重复发布。')
            if record.get('doi') and doi and clean_doi(record['doi']) != doi:
                raise LiteratureError('原求助 DOI 与本次论文身份冲突。')
            doi = doi or clean_doi(record.get('doi'))
            identity['doi'] = doi
            record = self.store.write(ref, title, {'doi': doi, **({'external_request_id': external} if external else {}),
                **({'metadata_identity': selected} if selected else {})})
            cached = self.store.cached(ref, title, doi)
            if cached is not None and record.get('adopted'):
                guard(force=True)
                return {**identity, 'state': 'succeeded', 'message': '全文已重新核验，可在三天保留期内下载。',
                        'pdf_base64': base64.b64encode(cached).decode('ascii')}
            if self.browser_factory is None:
                from .browser import AblesciBrowser
                self.browser_factory = AblesciBrowser
            with self.browser_factory(self.root, account, self.store, guard) as browser:
                browser.login()
                if selected:
                    browser.bind_identity(selected)
                listing = browser.list_requests()
                matches = [item for item in listing.records if title_matches(title, item.title)]
                if external:
                    request = next((item for item in listing.records if item.id == external), None)
                    if request is None and not listing.complete and getattr(listing, 'resumable', False):
                        return {**identity, 'state': 'waiting', 'message': '正在从原账号扫描游标继续核对求助编号，将在下一轮续查。'}
                    if request is None or not title_matches(title, request.title):
                        raise LiteratureError('未核验到原科研通求助 ID 与题名，保留原绑定并停止新发。')
                else:
                    if len(matches) > 1:
                        raise LiteratureError('原账号存在多个同题求助，需核对后绑定，未新发。')
                    request = matches[0] if matches else None
                    if request is not None and normalize_title(request.title) != normalize_title(title):
                        raise LiteratureError('原账号存在高度相似但不同的题名，需核对论文版本，未绑定或新发。')
                    if request is None:
                        if not listing.complete and getattr(listing, 'resumable', False):
                            return {**identity, 'state': 'waiting', 'message': '正在分段完整检查原账号求助列表，尚未新发布，将从已保存游标续查。'}
                        if record['phase'] == 'publish_intent':
                            raise LiteratureError('上次发布结果未知；原账号尚未核验到对应求助，禁止自动重发。')
                        if not listing.complete:
                            raise LiteratureError('尚未完整检查我的求助分页，禁止新发以避免重复。')
                        def before_submit():
                            guard(force=True)
                            self.store.write(ref, title, {'phase': 'publish_intent', 'intent_at': self.store.now()})
                        request = browser.publish(title, doi, before_submit=before_submit)
                    if not title_matches(title, request.title):
                        raise LiteratureError('科研通返回的求助题名与目标论文不符。')
                    external = request.id
                    identity['external_request_id'] = external
                self.store.write(ref, title, {'external_request_id': external,
                    'phase': 'download_verified' if cached is not None else 'waiting'})
                if selected and not doi:
                    if browser.verify_identity(external, title, doi) is not True:
                        raise LiteratureError('科研通详情未能证实所选期刊与年份，未下载或复用全文。')
                    self.store.write(ref, title, {'identity_proof': {'fingerprint': identity_fingerprint(selected),
                        'platform_verified': True, **({'pdf_sha256': record.get('pdf_proof', {}).get('sha256')} if cached is not None else {})}})
                guard()
                attachment = browser.attachment(external, title, doi)
                if attachment is None:
                    return {**identity, 'state': 'waiting', 'message': '原求助正在等待应助，将在下一轮继续检查。'}
                if cached is not None and record.get('verified_file_id') != attachment.id:
                    raise LiteratureError('应助附件 ID 已变化，需核对原全文与采纳状态。')
                if cached is None:
                    incoming = self.store.entry(ref, title) / 'incoming.pdf'
                    self.store._directory(incoming.parent)
                    try:
                        guard(force=True)
                        cached = browser.download(attachment, incoming)
                        guard()
                        self.store.cache(ref, title, doi, cached, attachment.id)
                    finally:
                        incoming.unlink(missing_ok=True)
                if attachment.needs_accept:
                    guard(force=True)
                    browser.accept(external, title, attachment)
                self.store.write(ref, title, {'phase': 'verified', 'adopted': True})
                guard(force=True)
                return {**identity, 'state': 'succeeded', 'message': '应助全文已核验，可在三天保留期内下载。',
                        'pdf_base64': base64.b64encode(cached).decode('ascii')}
        except LeaseLost:
            raise
        except LiteratureError as exc:
            return {**identity, 'state': exc.state, 'message': str(exc)}
        except ValueError:
            return {**identity, 'state': 'needs_attention', 'message': '文献身份或私有配置无法核验，请管理员检查。'}
        except Exception:
            # 第三方异常可能包含表单、Cookie、带 token 的 URL；不能回显或记录堆栈。
            return {**identity, 'state': 'waiting', 'message': '文献站点暂时无法完成核验，已保留原任务并等待重试。'}
