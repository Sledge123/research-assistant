"""Crossref 权威题名候选；不在模糊结果中擅自选论文。"""
import hashlib
from html import unescape
import json
import re

from .engine import LiteratureError, clean_doi
from .pdf import normalize_title


def plain(value, limit):
    if not isinstance(value, str):
        return ''
    return ' '.join(re.sub(r'<[^>]*>', '', unescape(value)).split())[:limit]


def candidates_from_items(items):
    candidates = []
    known = set()
    if not isinstance(items, list):
        raise LiteratureError('权威元数据响应结构无法核验。', 'waiting')
    for item in items[:20]:
        if not isinstance(item, dict):
            continue
        # Crossref可能将同题补充材料排在正文前；按明确类型排除，不能猜测DOI后缀。
        kind = item.get('type')
        if kind is not None and (not isinstance(kind, str)
                or kind.strip().casefold() in {'component', 'peer-review'}):
            continue
        titles = item.get('title', [])
        title = plain(titles[0], 1000) if isinstance(titles, list) and titles else ''
        if len(normalize_title(title)) < 6:
            continue
        try:
            doi = clean_doi(item.get('DOI'))
        except LiteratureError:
            continue
        journal = item.get('container-title', [])
        journal = plain(journal[0], 500) if isinstance(journal, list) and journal else None
        year = None
        for field in ('published', 'published-print', 'published-online', 'issued'):
            parts = item.get(field, {}).get('date-parts', []) if isinstance(item.get(field, {}), dict) else []
            if parts and isinstance(parts[0], list) and parts[0] and isinstance(parts[0][0], int):
                year = parts[0][0]
                if 1500 <= year <= 2200:
                    break
                year = None
        # 无 DOI 的同题记录仍可能属于不同期刊/年份，不能合并或共享候选 ID。
        source = {'source': 'crossref', 'doi': doi} if doi else {'source': 'crossref',
            'title': normalize_title(title), 'journal': normalize_title(journal or ''), 'year': year,
            'record': str(item.get('URL') or item.get('id') or '')[:2000]}
        key = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if key in known:
            continue
        known.add(key)
        candidates.append({'id': 'crossref-'+key[:24],
            'title': title, 'doi': doi, 'journal': journal or None, 'year': year})
    return candidates


class MetadataResolver:
    def __init__(self, client=None):
        self.client = client

    def resolve(self, title, guard=None):
        if not isinstance(title, str) or len(normalize_title(title)) < 6:
            raise LiteratureError('请输入可完整核验的论文标题。')
        guard = guard or (lambda **_: None)
        guard()
        owned = None
        try:
            client = self.client
            if client is None:
                import httpx
                owned = client = httpx.Client(timeout=18.0, follow_redirects=False,
                    headers={'User-Agent': 'HighScore-Literature/1.0 (Crossref title verification)'})
            response = client.get('https://api.crossref.org/works', params={
                'query.bibliographic': title, 'rows': 10,
                'select': 'DOI,title,container-title,published,issued,type'})
            if response.status_code != 200 or len(response.content) > 2 * 1024 * 1024:
                raise LiteratureError('权威元数据暂时无法核验，将稍后重试。', 'waiting')
            body = response.json()
            if body.get('status') != 'ok' or not isinstance(body.get('message'), dict):
                raise LiteratureError('权威元数据响应无法核验，将稍后重试。', 'waiting')
            candidates = candidates_from_items(body['message'].get('items'))
        except LiteratureError:
            raise
        except Exception:
            raise LiteratureError('权威元数据暂时无法核验，将稍后重试。', 'waiting') from None
        finally:
            if owned is not None:
                owned.close()
        guard()
        # 只有规范化后完全同题、且只有一个论文身份时才可直接采用。
        exact = [item for item in candidates if normalize_title(item['title']) == normalize_title(title)]
        if len(exact) == 1:
            return {'canonical_title': exact[0]['title'], 'doi': exact[0]['doi'], 'candidate': exact[0]}
        if candidates:
            return {'state': 'needs_selection', 'message': '请根据标题、期刊、年份和 DOI 选择所求论文。',
                    'candidates': candidates[:10]}
        return {'state': 'needs_attention', 'message': '未找到可核验的权威论文记录，请核对完整题名。'}
