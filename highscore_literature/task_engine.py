"""任务级自动取正文；历史只作为已核验 PDF 缓存，不决定本次能否发布求助。"""
import base64
import math

from .engine import (LiteratureError, LeaseLost, RETENTION_SECONDS, TaskStateStore,
                     candidate_identity, clean_doi, identity_fingerprint, validate_selected_pdf)
from .pdf import MAX_PDF_BYTES, normalize_title, title_matches, validate_pdf


def _stage(guard, value):
    method = getattr(guard, 'set_stage', None)
    if callable(method):
        method(value)


def _cached(engine, title, doi, selected):
    """只复用仍有效、已采纳且身份完全相符的实际文件；坏历史留存但不阻断新取文。"""
    for store, ref, target in engine.store.entries(title):
        try:
            record = store.read(ref, title)
            if (record.get('phase') != 'verified' or record.get('adopted') is not True
                    or clean_doi(record.get('doi')) != doi
                    or not isinstance(record.get('pdf_at'), (int, float))
                    or not 0 <= store.now() - record['pdf_at'] < RETENTION_SECONDS):
                continue
            if selected and (not record.get('metadata_identity')
                             or identity_fingerprint(selected) != identity_fingerprint(record['metadata_identity'])):
                continue
            path = store.path((target.parent/'verified.pdf').relative_to(store.root))
            if not path.is_file() or path.stat().st_size > MAX_PDF_BYTES:
                continue
            data = path.read_bytes()
            if validate_pdf(data, title, doi) != record.get('pdf_proof'):
                continue
            validate_selected_pdf(data, selected)
            if selected and not doi:
                proof = record.get('identity_proof', {})
                if (proof.get('platform_verified') is not True
                        or proof.get('fingerprint') != identity_fingerprint(selected)
                        or proof.get('pdf_sha256') != record['pdf_proof']['sha256']):
                    continue
            return data
        except (LiteratureError, OSError, ValueError, TypeError, KeyError, UnicodeError):
            continue
    return None


def _legacy_state(engine, job, title, doi, selected):
    """精确迁入旧任务绑定；未绑定时仅接续本任务时间窗内的未知意图。"""
    if job['source_revision'] != 0:
        return None
    if job.get('external_request_id'):
        if not job.get('account_ref'):
            return None
        record = engine.store.read(job['account_ref'], title)
        if record.get('external_request_id') != job['external_request_id']:
            return None
        if clean_doi(record.get('doi')) != doi:
            raise LiteratureError('原任务旧绑定的 DOI 与网站论文不一致，未替换身份。')
        if selected and record.get('metadata_identity') and (
                identity_fingerprint(selected) != identity_fingerprint(record['metadata_identity'])):
            raise LiteratureError('原任务旧绑定的候选身份与网站论文不一致，未替换身份。')
        return record
    created, deadline = job.get('created_at'), job.get('deadline')
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (created, deadline)):
        return None
    matches = []
    for store, ref, target in engine.store.entries(title):
        if target.relative_to(engine.store.base).parts[0] != 'accounts':
            continue
        try:
            record = store.read(ref, title)
            intent_at = record.get('intent_at')
            if (record.get('phase') != 'publish_intent' or record.get('external_request_id')
                    or clean_doi(record.get('doi')) != doi
                    or not isinstance(intent_at, (int, float)) or not math.isfinite(intent_at)
                    or not created <= intent_at <= min(deadline, store.now()+2)
                    or (job.get('account_ref') and job['account_ref'] != ref)):
                continue
            if selected and record.get('metadata_identity') and (
                    identity_fingerprint(selected) != identity_fingerprint(record['metadata_identity'])):
                continue
            matches.append(record)
        except (LiteratureError, OSError, ValueError, TypeError, KeyError, UnicodeError):
            continue
    if len(matches) > 1:
        raise LiteratureError('本任务时间窗内存在多个旧未知提交，未重新发布。')
    return matches[0] if matches else None


def run_task(engine, job, guard):
    identity = {'source_revision': job.get('source_revision')}
    try:
        guard()
        store = TaskStateStore(engine.root, job['id'], job['source_revision'], now=engine.store.now)
        saved = store.task_record()
        selected = candidate_identity(job['selected_candidate']) if job.get('selected_candidate') is not None else None
        if saved and saved.get('metadata_identity'):
            previous = candidate_identity(saved['metadata_identity'])
            if selected and identity_fingerprint(selected) != identity_fingerprint(previous):
                raise LiteratureError('本任务已绑定另一论文候选身份，未替换。')
            selected = selected or previous
        title = selected['title'] if selected else (job.get('canonical_title') or (saved or {}).get('canonical_title'))
        doi = selected['doi'] if selected else (clean_doi(job.get('doi')) or clean_doi((saved or {}).get('doi')))
        if selected and job.get('canonical_title') and normalize_title(title) != normalize_title(job['canonical_title']):
            raise LiteratureError('所选论文与网站绑定题名不一致。')
        if saved and job.get('canonical_title') and normalize_title(saved['canonical_title']) != normalize_title(job['canonical_title']):
            raise LiteratureError('本任务的持久题名与网站绑定不一致。')
        if saved and clean_doi(saved.get('doi')) != doi:
            raise LiteratureError('本任务的持久 DOI 与本次获取身份不一致。')
        if job.get('doi') and clean_doi(job['doi']) != doi:
            raise LiteratureError('本任务的持久 DOI 与网站绑定不一致。')
        _stage(guard, 'fetching')
        if not title:
            if engine.resolver is None:
                from .metadata import MetadataResolver
                engine.resolver = MetadataResolver()
            result = engine.resolver.resolve(job['title'], guard=guard)
            if result.get('state'):
                return {**identity, **result}
            title, doi = result['canonical_title'], clean_doi(result.get('doi'))
            selected = candidate_identity(result['candidate']) if result.get('candidate') else None
        store._key(title)
        legacy = _legacy_state(engine, job, title, doi, selected) if saved is None else None
        if legacy is not None:
            saved = legacy
            if selected is None and saved.get('metadata_identity'):
                selected = candidate_identity(saved['metadata_identity'])
        identity.update(canonical_title=title, doi=doi)
        if selected and not doi and (not selected['journal'] or not selected['year']):
            raise LiteratureError('所选论文缺少 DOI 及可核验的期刊年份。')
        accounts = engine.accounts
        if accounts is None:
            from .credentials import load_accounts
            accounts = load_accounts(engine.root)
        accounts = [item for item in accounts if item.get('enabled', True)]
        if saved and job.get('account_ref') and saved['account_ref'] != job['account_ref']:
            raise LiteratureError('本任务原账号与服务器绑定不一致。')
        bound_ref = job.get('account_ref') or (saved or {}).get('account_ref')
        if job.get('external_request_id') and not bound_ref:
            raise LiteratureError('原任务的求助编号缺少账号绑定，未在其他账号发布。')
        account = (next((item for item in accounts if item['account_ref'] == bound_ref), None)
                   if bound_ref else next(iter(accounts), None))
        if account is None:
            raise LiteratureError('原取文账号暂不可用，请管理员检查配置。')
        ref = account['account_ref']
        identity['account_ref'] = ref
        record = store.read(ref, title)
        if record.get('doi') and clean_doi(record['doi']) != doi:
            raise LiteratureError('本次取文状态的 DOI 与网站论文不一致。')
        external = job.get('external_request_id') or record.get('external_request_id')
        if (job.get('external_request_id') and record.get('external_request_id')
                and job['external_request_id'] != record['external_request_id']):
            raise LiteratureError('本次网站任务与执行器求助编号不一致。')
        if external:
            identity['external_request_id'] = external
        record = store.write(ref, title, {'doi': doi,
            **({'metadata_identity': selected} if selected else {}),
            **({'external_request_id': external} if external else {}),
            **({'phase': 'publish_intent', 'intent_at': legacy['intent_at'],
                'publish_baseline': {'ids': [], 'signature': '0'*64, 'legacy_intent': True}}
               if legacy is not None and not external else {})})
        cached = _cached(engine, title, doi, selected)
        if cached is not None:
            _stage(guard, 'verifying')
            guard(force=True)
            return {**identity, 'state': 'succeeded', 'stage': 'ready',
                    'pdf_base64': base64.b64encode(cached).decode('ascii')}
        if engine.browser_factory is None:
            from .browser import AblesciBrowser
            engine.browser_factory = AblesciBrowser
        with engine.browser_factory(engine.root, account, store, guard) as browser:
            browser.login()
            if selected:
                browser.bind_identity(selected)
            if not external:
                if record['phase'] == 'publish_intent':
                    baseline, intent_at = record.get('publish_baseline'), record.get('intent_at')
                    if not isinstance(baseline, dict) or not isinstance(intent_at, (int, float)):
                        raise LiteratureError('本次发布意图缺少可核验的提交记录，未重复发布。')
                    request = browser.recover_publish(baseline, title, doi, intent_at)
                    if request is None:
                        raise LiteratureError('本次发布结果尚未唯一确认，保留意图继续核查，未重复发布。')
                else:
                    baseline = browser.prepare_publish()
                    def before_submit():
                        guard(force=True)
                        store.write(ref, title, {'phase': 'publish_intent', 'intent_at': store.now(),
                                                'publish_baseline': baseline})
                    request = browser.publish(title, doi, before_submit=before_submit)
                if not title_matches(title, request.title) or normalize_title(title) != normalize_title(request.title):
                    raise LiteratureError('本次发布返回了不同题名，未绑定或下载。')
                external = request.id
                identity['external_request_id'] = external
                record = store.write(ref, title, {'external_request_id': external, 'phase': 'waiting'})
            source = browser.source(external, title, doi)
            if source.status in {'expired', 'supplement_only', 'finished_without_file'}:
                if not job.get('external_request_id'):
                    # 初次回调先绑定本次已发布编号；下一轮服务器才能按该编号原子接续。
                    return {**identity, 'state': 'waiting', 'stage': 'renewing',
                            'message': '本次来源已确认需要重新获取，正在保存绑定后接续。'}
                # 旧绑定保留在本轮状态；只有服务器提交审计并增加轮次后才允许新发布。
                return {**identity, 'state': 'waiting', 'stage': 'renewing', 'source_reset': {
                    'previous_request_id': external, 'source_revision': job['source_revision'],
                    'reason': source.status, 'evidence_sha256': source.evidence_sha256}}
            if source.status == 'awaiting':
                return {**identity, 'state': 'waiting', 'stage': 'awaiting_fulltext',
                        'message': '本次正文求助已核对，正在等待全文。'}
            if source.status != 'available' or source.attachment is None:
                raise LiteratureError('正文来源的附件状态无法核验。')
            attachment = source.attachment
            cached = store.cached(ref, title, doi)
            if selected and not doi:
                if browser.verify_identity(external, title, doi) is not True:
                    raise LiteratureError('平台未证实所选论文的期刊与年份。')
                store.write(ref, title, {'identity_proof': {'fingerprint': identity_fingerprint(selected),
                    'platform_verified': True,
                    **({'pdf_sha256': record.get('pdf_proof', {}).get('sha256')} if cached is not None else {})}})
            if cached is not None and record.get('verified_file_id') != attachment.id:
                raise LiteratureError('本次正文附件编号已变化，需核对后继续。')
            if cached is None:
                incoming = store.entry(ref, title)/'incoming.pdf'
                store._directory(incoming.parent)
                try:
                    _stage(guard, 'fetching')
                    guard(force=True)
                    cached = browser.download(attachment, incoming)
                    _stage(guard, 'verifying')
                    guard(force=True)
                    store.cache(ref, title, doi, cached, attachment.id)
                finally:
                    incoming.unlink(missing_ok=True)
            if attachment.needs_accept:
                guard(force=True)
                browser.accept(external, title, attachment)
            store.write(ref, title, {'phase': 'verified', 'adopted': True})
            guard(force=True)
            return {**identity, 'state': 'succeeded', 'stage': 'ready',
                    'pdf_base64': base64.b64encode(cached).decode('ascii')}
    except LeaseLost:
        raise
    except LiteratureError as exc:
        return {**identity, 'state': exc.state, 'stage': 'retrying', 'message': str(exc)}
    except (ValueError, KeyError, TypeError):
        return {**identity, 'state': 'needs_attention', 'stage': 'retrying',
                'message': '本次论文身份或取文状态无法核验，请管理员检查。'}
    except Exception:
        return {**identity, 'state': 'waiting', 'stage': 'retrying',
                'message': '本轮取文暂未完成，保留本次提交记录继续核查。'}
