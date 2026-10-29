"""文献执行器离线回归：只模拟站点边界，真实核验本地状态与 PDF。"""
import base64
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

ROOT = Path(__file__).resolve().parents[1]
TITLE = 'Synthetic literature request for offline verification'
ACCOUNT = {'account_ref': 'synthetic-a', 'username': 'synthetic@example.test',
           'password': 'synthetic-private-password', 'enabled': True}


def sample_pdf(title=TITLE, header=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
        NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    content = f'BT /F1 12 Tf 20 750 Td ({title}) Tj'
    if header:
        content += f' 0 -20 Td ({header}) Tj'
    stream.set_data((content+' ET').encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class LiteratureWorkerTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / '.cache/tests'
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='literature-worker-', dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.network = patch.object(socket, 'create_connection', side_effect=AssertionError('离线禁止联网'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def job(self, **values):
        return {'id': 'synthetic-job', 'title': TITLE, 'canonical_title': TITLE, 'doi': None,
                'external_request_id': None, 'account_ref': None, 'deadline': time.time()+3600, **values}

    def engine(self, **values):
        from highscore_literature.engine import LiteratureEngine
        self.browser = Mock()
        self.browser.__enter__ = Mock(return_value=self.browser)
        self.browser.__exit__ = Mock(return_value=False)
        self.factory = Mock(return_value=self.browser)
        self.resolver = Mock()
        self.resolver.resolve.return_value = {'canonical_title': TITLE, 'doi': None}
        engine = LiteratureEngine(self.root, accounts=[ACCOUNT], browser_factory=self.factory,
                                  resolver=self.resolver, **values)
        from highscore_literature.browser import Listing
        self.browser.list_requests.return_value = Listing([], True)
        self.browser.attachment.return_value = None
        return engine

    def record(self, request_id='12345', title=TITLE):
        from highscore_literature.browser import RequestRecord
        return RequestRecord(request_id, title, '待应助')

    def test_incomplete_pagination_cannot_publish(self):
        from highscore_literature.browser import Listing
        engine = self.engine()
        self.browser.list_requests.return_value = Listing([], False)
        result = engine(self.job(), Mock())
        self.assertEqual(result['state'], 'needs_attention')
        self.browser.publish.assert_not_called()

    def test_bound_id_is_never_replaced_or_republished(self):
        from highscore_literature.browser import Listing
        engine = self.engine()
        self.browser.list_requests.return_value = Listing([self.record('different')], True)
        result = engine(self.job(external_request_id='12345', account_ref=ACCOUNT['account_ref']), Mock())
        self.assertEqual(result['state'], 'needs_attention')
        self.assertEqual(result['external_request_id'], '12345')
        self.browser.publish.assert_not_called()

    def test_publish_intent_precedes_click_and_uncertain_result_is_not_replayed(self):
        engine = self.engine()
        def publish(title, doi, before_submit):
            before_submit()
            state = engine.store.read(ACCOUNT['account_ref'], TITLE)
            self.assertEqual(state['phase'], 'publish_intent')
            raise OSError('https://example.test/?token=synthetic-private-secret')
        self.browser.publish.side_effect = publish
        first = engine(self.job(), Mock())
        second = engine(self.job(id='next-job'), Mock())
        self.assertEqual(first['state'], 'waiting')
        self.assertEqual(second['state'], 'needs_attention')
        self.assertEqual(self.browser.publish.call_count, 1)
        self.assertNotIn('synthetic-private-secret', json.dumps([first, second]))

    def test_uncertain_publish_is_recovered_from_same_account(self):
        from highscore_literature.browser import Listing
        engine = self.engine()
        engine.store.write(ACCOUNT['account_ref'], TITLE, {'phase': 'publish_intent'})
        self.browser.list_requests.return_value = Listing([self.record()], True)
        result = engine(self.job(), Mock())
        self.assertEqual((result['state'], result['external_request_id']), ('waiting', '12345'))
        self.browser.publish.assert_not_called()
        self.assertEqual(engine.store.read(ACCOUNT['account_ref'], TITLE)['external_request_id'], '12345')

    def test_selected_canonical_title_does_not_repeat_metadata_selection(self):
        engine = self.engine()
        result = engine(self.job(title='User original abbreviated input'), Mock())
        self.resolver.resolve.assert_not_called()
        self.assertEqual(result['canonical_title'], TITLE)

    def test_ambiguous_metadata_returns_selection_without_opening_browser(self):
        engine = self.engine()
        self.resolver.resolve.return_value = {'state': 'needs_selection', 'message': '请选择论文',
            'candidates': [{'id': 'candidate', 'title': TITLE, 'doi': None, 'journal': None, 'year': None}]}
        result = engine(self.job(canonical_title=None), Mock())
        self.assertEqual(result['state'], 'needs_selection')
        self.factory.assert_not_called()

    def test_wrong_pdf_cannot_be_accepted_or_reported_success(self):
        from highscore_literature.browser import Attachment, Listing
        engine = self.engine()
        self.browser.list_requests.return_value = Listing([self.record()], True)
        self.browser.attachment.return_value = Attachment('f123', 'synthetic.pdf', True)
        self.browser.download.return_value = sample_pdf('A different unrelated document')
        result = engine(self.job(), Mock())
        self.assertEqual(result['state'], 'needs_attention')
        self.browser.accept.assert_not_called()
        self.assertNotIn('pdf_base64', result)
        self.assertFalse(list((self.root/'output/literature-worker').rglob('verified.pdf')))

    def test_validated_cache_reused_for_new_job_and_deleted_after_three_days(self):
        from highscore_literature.browser import Attachment, Listing
        now = [time.time()]
        engine = self.engine(now=lambda: now[0])
        self.browser.list_requests.return_value = Listing([self.record()], True)
        self.browser.attachment.return_value = Attachment('f123', 'synthetic.pdf', True)
        payload = sample_pdf()
        self.browser.download.return_value = payload
        first = engine(self.job(), Mock())
        self.assertEqual(first['state'], 'succeeded')
        self.assertEqual(base64.b64decode(first['pdf_base64']), payload)
        self.browser.accept.assert_called_once()
        self.factory.reset_mock()
        second = engine(self.job(id='next-job'), Mock())
        self.assertEqual(second['state'], 'succeeded')
        self.factory.assert_not_called()
        now[0] += 3*86400+1
        engine.store.cleanup()
        self.assertFalse(list((self.root/'output/literature-worker').rglob('verified.pdf')))
        state = engine.store.read(ACCOUNT['account_ref'], TITLE)
        self.assertEqual(state['external_request_id'], '12345')
        self.assertNotIn('pdf_at', state)

    def test_cache_corruption_cannot_be_returned(self):
        from highscore_literature.browser import Attachment, Listing
        engine = self.engine()
        self.browser.list_requests.return_value = Listing([self.record()], True)
        self.browser.attachment.return_value = Attachment('f123', 'synthetic.pdf', False)
        self.browser.download.return_value = sample_pdf()
        self.assertEqual(engine(self.job(), Mock())['state'], 'succeeded')
        path = next((self.root/'output/literature-worker').rglob('verified.pdf'))
        path.write_bytes(sample_pdf('A different unrelated document'))
        result = engine(self.job(id='next-job'), Mock())
        self.assertEqual(result['state'], 'needs_attention')
        self.assertNotIn('pdf_base64', result)

    def test_private_state_rejects_path_escape_and_rebinding(self):
        from highscore_literature.engine import StateStore, LiteratureError
        store = StateStore(self.root)
        with self.assertRaises(LiteratureError):
            store.read('../outside', TITLE)
        store.write(ACCOUNT['account_ref'], TITLE, {'external_request_id': '12345', 'phase': 'waiting'})
        with self.assertRaises(LiteratureError):
            store.write(ACCOUNT['account_ref'], TITLE, {'external_request_id': 'different'})
        state = next((self.root/'output/literature-worker').rglob('state.json')).read_text(encoding='utf-8')
        self.assertNotIn(ACCOUNT['username'], state)
        self.assertNotIn(ACCOUNT['password'], state)

    def test_cleanup_keeps_old_live_worker_lock_while_deleting_expired_pdf(self):
        from highscore_literature.engine import RETENTION_SECONDS, StateStore
        from highscore_literature.worker import worker_mutex
        store = StateStore(self.root)
        with worker_mutex(store) as acquired:
            self.assertTrue(acquired)
            lock = store.base/'worker.lock'
            orphan = store.base/'orphan.pdf'
            orphan.write_bytes(sample_pdf())
            old = time.time()-RETENTION_SECONDS-10
            os.utime(lock, (old, old))
            os.utime(orphan, (old, old))
            store.cleanup()
            self.assertTrue(lock.exists())
            self.assertFalse(orphan.exists())

    def test_api_connection_failure_still_deletes_expired_local_pdf_before_claim(self):
        from highscore_literature.engine import RETENTION_SECONDS, StateStore
        from highscore_literature.worker import Worker, WorkerError
        store = StateStore(self.root)
        store._directory(store.base)
        orphan = store.base/'expired-offline.pdf'
        orphan.write_bytes(sample_pdf())
        old = time.time()-RETENTION_SECONDS-10
        os.utime(orphan, (old, old))
        client = Mock()
        def offline(**kwargs):
            self.assertFalse(orphan.exists())
            raise WorkerError('离线模拟，不包含秘密', 'waiting')
        client.claim.side_effect = offline
        worker = Worker(self.root, client=client, accounts_provider=lambda: [ACCOUNT])
        self.assertFalse(worker.run_once())
        self.assertFalse(orphan.exists())

    def test_browser_doi_field_rejects_prefix_and_reference_only_identity(self):
        from highscore_literature.browser import AblesciBrowser
        from highscore_literature.engine import LiteratureError
        for body in (TITLE+'\nDOI: 10.1234/a2', TITLE+'\nDOI: 10.1234/b\nReferences:\nDOI: 10.1234/a',
                     TITLE+'\nReferences:\nDOI: 10.1234/a'):
            browser = AblesciBrowser(self.root, ACCOUNT, None, Mock())
            browser.page = Mock()
            browser.page.url = 'https://www.ablesci.com/assist/detail?id=12345'
            browser.page.locator.return_value.inner_text.return_value = body
            with self.subTest(body=body), self.assertRaises(LiteratureError):
                browser._verify_detail('12345', TITLE, '10.1234/a')

    def test_browser_explicit_own_doi_field_accepts_complete_identifier(self):
        from highscore_literature.browser import AblesciBrowser
        browser = AblesciBrowser(self.root, ACCOUNT, None, Mock())
        browser.page = Mock()
        browser.page.url = 'https://www.ablesci.com/assist/detail?id=12345'
        browser.page.locator.return_value.inner_text.return_value = TITLE+'\nDOI\nhttps://doi.org/10.1234/a\nReferences:\nDOI: 10.1234/b'
        browser._verify_detail('12345', TITLE, '10.1234/a')

    def test_browser_explicit_doi_field_accepts_exact_copy_control_text(self):
        from highscore_literature.browser import AblesciBrowser, detail_publication_dois
        title = 'Chemical etching of manganese oxides for electrocatalytic oxygen reduction reaction'
        doi = '10.1039/c5cc03155a'
        for value in (doi+' 复制 doi', 'https://doi.org/'+doi+' 复制 doi'):
            with self.subTest(value=value):
                body = title+'\nDOI\n'+value+'\nReferences:\nDOI: 10.1234/other'
                self.assertEqual(detail_publication_dois(body), {doi})
                browser = AblesciBrowser(self.root, ACCOUNT, None, Mock())
                browser.page = Mock()
                browser.page.url = 'https://www.ablesci.com/assist/detail?id=12345'
                browser.page.locator.return_value.inner_text.return_value = body
                browser._verify_detail('12345', title, doi)

    def test_browser_copy_control_does_not_weaken_doi_identity_boundaries(self):
        from highscore_literature.browser import AblesciBrowser
        from highscore_literature.engine import LiteratureError
        doi, other = '10.1039/c5cc03155a', '10.1234/other'
        fields = [doi+'2 复制 doi', other+' 复制 doi',
                  doi+' additional explanatory text 复制 doi', doi+' 复制 doi additional text',
                  doi+' '+other+' 复制 doi', doi+' 复制 doi\nDOI\n'+other+' 复制 doi',
                  doi+' 复制 DOI', doi+' 复制doi']
        bodies = [TITLE+'\nDOI\n'+value for value in fields]
        bodies += [TITLE+'\n'+section+'\nDOI\n'+doi+' 复制 doi'
                   for section in ('References:', 'Abstract:', 'Introduction:', '参考文献', '摘要', '评论')]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(LiteratureError):
                browser = AblesciBrowser(self.root, ACCOUNT, None, Mock())
                browser.page = Mock()
                browser.page.url = 'https://www.ablesci.com/assist/detail?id=12345'
                browser.page.locator.return_value.inner_text.return_value = body
                browser._verify_detail('12345', TITLE, doi)

    def test_navigation_uses_remaining_timeout_instead_of_full_page_timeout(self):
        from highscore_literature.browser import AblesciBrowser
        from highscore_literature.engine import SliceExpired
        from highscore_literature.worker import LeaseGuard
        client = Mock()
        client.heartbeat.return_value = {'cancel_requested': False}
        guard = LeaseGuard(client, self.job(), 'synthetic-lease', max_seconds=.025)
        browser = AblesciBrowser(self.root, ACCOUNT, None, guard)
        browser.page = Mock()
        browser._security = Mock(side_effect=guard)
        def blocking_navigation(*args, **kwargs):
            timeout = kwargs.get('timeout', 80)
            time.sleep(min(timeout/1000, .08))
            raise TimeoutError('合成浏览器导航超时')
        browser.page.goto.side_effect = blocking_navigation
        started = time.monotonic()
        with self.assertRaises(Exception):
            browser._goto('https://www.ablesci.com/my/assist-my')
        self.assertLess(time.monotonic()-started, .065)
        self.assertIn('timeout', browser.page.goto.call_args.kwargs)
        self.assertLessEqual(browser.page.goto.call_args.kwargs['timeout'], 25)

    def test_click_focus_fill_and_evaluate_all_receive_remaining_timeout(self):
        from highscore_literature.browser import AblesciBrowser
        from highscore_literature.worker import LeaseGuard
        client = Mock()
        client.heartbeat.return_value = {'cancel_requested': False}
        guard = LeaseGuard(client, self.job(), 'synthetic-lease', max_seconds=1)
        browser = AblesciBrowser(self.root, ACCOUNT, None, guard)
        browser._security = Mock(side_effect=guard)
        control = Mock()
        control.is_visible.return_value = control.is_enabled.return_value = control.evaluate.return_value = True
        browser._click(control)
        browser._fill(control, '合成题名')
        for method in (control.click, control.focus, control.evaluate, control.fill):
            self.assertGreater(method.call_args.kwargs['timeout'], 0)
            self.assertLessEqual(method.call_args.kwargs['timeout'], 1000)

    def pagination_adapter(self, pages=51, head_generation=0, calls=None, malformed=False, missing_disabled=False,
                           last_active_per_page=10, pagination_visible=True):
        from highscore_literature.browser import AblesciBrowser, RequestRecord
        from highscore_literature.engine import StateStore
        from urllib.parse import parse_qs, urlsplit
        calls = calls if calls is not None else []
        adapter = AblesciBrowser(self.root, ACCOUNT, StateStore(self.root), Mock())
        page = [1]
        class Locator:
            def __init__(self, nodes=()): self.nodes = list(nodes)
            def count(self): return len(self.nodes)
            def nth(self, index): return self.nodes[index]
            @property
            def first(self): return self.nodes[0]
        class Node:
            def __init__(self, role, text='', href=None, classes=''):
                self.role, self.text, self.href, self.classes = role, text, href, classes
            def is_visible(self): return pagination_visible
            def get_attribute(self, name, **kwargs): return {'href': self.href, 'class': self.classes}.get(name)
            def inner_text(self, **kwargs): return self.text
            def locator(self, selector):
                number = page[0]
                if self.role == 'pager':
                    if selector == 'li.active': return Locator([Node('active', classes='active')])
                    if selector == 'li.next': return Locator([Node('next', classes='next disabled' if number == pages and not missing_disabled else 'next')])
                    if selector == 'li.last': return Locator([Node('last', classes='last disabled' if number == pages else 'last')])
                    return Locator()
                if selector == 'a':
                    if self.role == 'active':
                        active_size = last_active_per_page if number == pages else 10
                        return Locator([Node('anchor', str(number), f'/my/assist-my?page={number}&per-page={active_size}')])
                    if self.role == 'next' and number < pages:
                        step = number+2 if malformed else number+1
                        return Locator([Node('anchor', '下页', f'/my/assist-my?page={step}&per-page=10')])
                    if self.role == 'last' and number < pages: return Locator([Node('anchor', '尾页', f'/my/assist-my?page={pages}&per-page=10')])
                return Locator()
        adapter.page = Mock()
        adapter.page.url = 'https://www.ablesci.com/my/assist-my'
        adapter.page.locator.side_effect = lambda selector: Locator([Node('pager')]) if selector == '.pagination' else Locator()
        def goto(url, *_):
            number = int(parse_qs(urlsplit(url).query).get('page', [1])[0])
            page[0] = number
            adapter.page.url = url
            calls.append(number)
        adapter._goto = Mock(side_effect=goto)
        adapter._security = Mock()
        adapter._clear_notification = Mock()
        adapter._records = Mock(side_effect=lambda: [RequestRecord(
            f'req-{head_generation if page[0] == 1 else 0}-{page[0]}-{index}', f'完整题名 {page[0]} {index}', '待应助') for index in range(10)])
        return adapter, calls

    def test_unpainted_two_page_list_recovers_original_published_request_without_republish(self):
        engine = self.engine()
        engine.store.write(ACCOUNT['account_ref'], TITLE, {'phase': 'publish_intent'})
        adapter, calls = self.pagination_adapter(pages=2, pagination_visible=False)
        head = [self.record(f'history-{index}', f'历史论文完整题名 {index}') for index in range(10)]
        published = self.record('synthetic-original-new-request')
        adapter._records.side_effect = lambda: head if calls[-1] == 1 else [published]
        self.browser.list_requests.side_effect = adapter.list_requests
        result = engine(self.job(), Mock())
        self.assertEqual((result['state'], result.get('external_request_id')), ('waiting', published.id))
        self.assertEqual(calls, [1, 2, 1])
        scan = adapter.store.read_scan(ACCOUNT['account_ref'])
        self.assertEqual(scan['phase'], 'complete')
        self.assertEqual(len(scan['records']), 11)
        self.assertIn(published.id, [value['id'] for value in scan['records']])
        self.assertEqual(engine.store.read(ACCOUNT['account_ref'], TITLE)['external_request_id'], published.id)
        self.browser.publish.assert_not_called()
        self.browser.accept.assert_not_called()

    def test_hidden_and_visible_html_pagers_are_rejected_as_ambiguous(self):
        from highscore_literature.engine import LiteratureError
        adapter, _ = self.pagination_adapter(pages=1)
        original = adapter.page.locator.side_effect
        html = original('.pagination')
        hidden = Mock(wraps=html.first)
        hidden.is_visible.return_value = False
        duplicate = type(html)([html.first, hidden])
        adapter.page.locator.side_effect = lambda selector: duplicate if selector == '.pagination' else original(selector)
        with self.assertRaisesRegex(LiteratureError, '不唯一'):
            adapter.list_requests()
        self.assertIsNone(adapter.store.read_scan(ACCOUNT['account_ref']))

    def test_hidden_and_visible_active_markers_are_rejected_as_ambiguous(self):
        from highscore_literature.engine import LiteratureError
        adapter, _ = self.pagination_adapter(pages=1)
        pager = adapter.page.locator('.pagination').first
        original = pager.locator
        active = original('li.active')
        hidden = Mock(wraps=active.first)
        hidden.is_visible.return_value = False
        duplicate = type(active)([active.first, hidden])
        pager.locator = Mock(side_effect=lambda selector: duplicate if selector == 'li.active' else original(selector))
        # 固定同一个 DOM 容器，让当前页标记的歧义保留到读取时。
        page_locator = adapter.page.locator.side_effect
        html = type(active)([pager])
        adapter.page.locator.side_effect = lambda selector: html if selector == '.pagination' else page_locator(selector)
        with self.assertRaisesRegex(LiteratureError, '当前页'):
            adapter.list_requests()
        self.assertIsNone(adapter.store.read_scan(ACCOUNT['account_ref']))

    def test_hidden_layui_pager_cannot_be_ignored_beside_visible_html_pager(self):
        from highscore_literature.engine import LiteratureError
        adapter, _ = self.pagination_adapter(pages=1)
        original = adapter.page.locator.side_effect
        html = original('.pagination')
        hidden = Mock(wraps=html.first)
        hidden.is_visible.return_value = False
        layui = type(html)([hidden])
        adapter.page.locator.side_effect = lambda selector: layui if selector == '.layui-laypage' else original(selector)
        with self.assertRaisesRegex(LiteratureError, '两套'):
            adapter.list_requests()
        self.assertIsNone(adapter.store.read_scan(ACCOUNT['account_ref']))

    def test_hidden_unknown_pager_cannot_certify_a_single_page(self):
        adapter, _ = self.pagination_adapter(pages=2, pagination_visible=False)
        hidden = adapter.page.locator('.pagination')
        empty = type(hidden)()
        adapter.page.locator.side_effect = lambda selector: hidden if selector == '[aria-label="分页"], [aria-label="pagination"]' else empty
        result = adapter.list_requests()
        self.assertFalse(result.complete)
        self.assertIsNone(adapter.store.read_scan(ACCOUNT['account_ref']))

    def test_html_fifty_one_pages_resume_from_persisted_cursor_across_slices(self):
        calls = []
        result = None
        pieces = 0
        for _ in range(10):
            adapter, _ = self.pagination_adapter(calls=calls)
            result = adapter.list_requests()
            pieces += 1
            if result.complete: break
            self.assertTrue(result.resumable)
        self.assertTrue(result.complete)
        self.assertEqual(pieces, 7)
        self.assertEqual(len(result.records), 510)
        self.assertEqual(calls.count(1), 2*pieces)
        self.assertEqual(calls.count(2), 1)
        self.assertEqual(set(calls), set(range(1, 52)))

    def test_html_scan_restarts_if_head_changed_and_does_not_certify_old_tail(self):
        first, _ = self.pagination_adapter(pages=12)
        self.assertFalse(first.list_requests().complete)
        second, calls = self.pagination_adapter(pages=12, head_generation=1)
        result = second.list_requests()
        self.assertFalse(result.complete)
        self.assertIn(2, calls)
        self.assertTrue(result.resumable)

    def test_html_skipped_next_page_is_rejected_instead_of_guessed(self):
        from highscore_literature.engine import LiteratureError
        adapter, _ = self.pagination_adapter(malformed=True)
        with self.assertRaises(LiteratureError):
            adapter.list_requests()

    def test_html_no_next_anchor_without_disabled_state_cannot_certify_last_page(self):
        from highscore_literature.engine import LiteratureError
        adapter, _ = self.pagination_adapter(pages=1, missing_disabled=True)
        with self.assertRaises(LiteratureError):
            adapter.list_requests()

    def test_html_cursor_survives_slice_expiration_during_navigation(self):
        from highscore_literature.engine import SliceExpired
        first, _ = self.pagination_adapter(pages=12)
        original = first._goto.side_effect
        def stop_at_five(url, *args):
            if '?page=5&' in url: raise SliceExpired()
            original(url, *args)
        first._goto.side_effect = stop_at_five
        with self.assertRaises(SliceExpired):
            first.list_requests()
        self.assertIn('?page=5&', first.store.read_scan(ACCOUNT['account_ref'])['next_href'])
        resumed, calls = self.pagination_adapter(pages=12)
        self.assertTrue(resumed.list_requests().complete)
        self.assertEqual(calls[:2], [1, 5])
        self.assertNotIn(2, calls)

    def test_html_cursor_redirect_to_last_page_is_rejected_before_merge(self):
        from highscore_literature.engine import LiteratureError
        adapter, calls = self.pagination_adapter()
        original = adapter._goto.side_effect
        adapter._goto.side_effect = lambda url, *args: original(url.replace('?page=2&', '?page=51&'), *args)
        with self.assertRaises(LiteratureError):
            adapter.list_requests()
        scan = adapter.store.read_scan(ACCOUNT['account_ref'])
        self.assertEqual(scan['phase'], 'scanning')
        self.assertEqual(len(scan['records']), 10)
        self.assertEqual(scan['visited'], ['/my/assist-my'])
        self.assertIn('?page=2&', scan['next_href'])
        self.assertEqual(calls, [1, 51])

    def test_html_cursor_redirect_changing_per_page_is_rejected(self):
        from highscore_literature.engine import LiteratureError
        adapter, _ = self.pagination_adapter()
        original = adapter._goto.side_effect
        adapter._goto.side_effect = lambda url, *args: original(url.replace('page=2&per-page=10', 'page=2&per-page=20'), *args)
        with self.assertRaises(LiteratureError):
            adapter.list_requests()
        scan = adapter.store.read_scan(ACCOUNT['account_ref'])
        self.assertEqual(len(scan['records']), 10)
        self.assertEqual(scan['phase'], 'scanning')

    def test_html_last_active_per_page_mismatch_is_rejected_before_merge(self):
        from highscore_literature.engine import LiteratureError
        adapter, calls = self.pagination_adapter(pages=2, last_active_per_page=20)
        with self.assertRaises(LiteratureError):
            adapter.list_requests()
        scan = adapter.store.read_scan(ACCOUNT['account_ref'])
        self.assertEqual(scan['phase'], 'scanning')
        self.assertEqual(len(scan['records']), 10)
        self.assertEqual(scan['visited'], ['/my/assist-my'])
        self.assertEqual(scan['next_href'], '/my/assist-my?page=2&per-page=10')
        self.assertEqual(calls, [1, 2])
        self.assertEqual(adapter._records.call_count, 1)

    def test_html_slice_end_head_change_discards_old_records_and_cursor(self):
        adapter, calls = self.pagination_adapter(pages=12)
        original = adapter._records.side_effect
        new_head = [self.record(f'new-head-{index}', f'新头页题名 {index}') for index in range(10)]
        adapter._records.side_effect = lambda: new_head if calls[-1] == 1 and calls.count(1) > 1 else original()
        result = adapter.list_requests()
        self.assertEqual(result.records, new_head)
        self.assertFalse(result.complete)
        self.assertTrue(result.resumable)
        self.assertEqual(calls, list(range(1, 10))+[1])
        scan = adapter.store.read_scan(ACCOUNT['account_ref'])
        self.assertEqual(scan['phase'], 'scanning')
        self.assertEqual([value['id'] for value in scan['records']], [value.id for value in new_head])
        self.assertEqual(scan['visited'], ['/my/assist-my'])
        self.assertEqual(scan['next_href'], '/my/assist-my?page=2&per-page=10')
        self.assertIsNone(adapter._scan_proof)

    def test_html_head_redirect_to_non_first_page_cannot_start_scan(self):
        from highscore_literature.engine import LiteratureError
        adapter, _ = self.pagination_adapter()
        original = adapter._goto.side_effect
        adapter._goto.side_effect = lambda url, *args: original('https://www.ablesci.com/my/assist-my?page=51&per-page=10', *args)
        with self.assertRaises(LiteratureError):
            adapter.list_requests()
        adapter._records.assert_not_called()
        self.assertIsNone(adapter.store.read_scan(ACCOUNT['account_ref']))

    def test_publish_head_recheck_stops_before_form_if_page_changed(self):
        from highscore_literature.engine import LiteratureError
        original, _ = self.pagination_adapter(pages=1)
        self.assertTrue(original.list_requests().complete)
        changed, calls = self.pagination_adapter(pages=1, head_generation=1)
        changed._scan_proof = original._scan_proof
        submit = Mock()
        with self.assertRaises(LiteratureError) as raised:
            changed.publish(TITLE, None, submit)
        self.assertEqual(raised.exception.state, 'waiting')
        self.assertEqual(calls, [1])
        submit.assert_not_called()

    def test_resumable_incomplete_list_waits_for_new_and_unknown_publish_jobs(self):
        from highscore_literature.browser import Listing
        engine = self.engine()
        self.browser.list_requests.return_value = Listing([], False, True)
        self.assertEqual(engine(self.job(), Mock())['state'], 'waiting')
        engine.store.write(ACCOUNT['account_ref'], TITLE, {'phase': 'publish_intent'})
        self.assertEqual(engine(self.job(), Mock())['state'], 'waiting')
        self.assertEqual(engine(self.job(account_ref=ACCOUNT['account_ref'], external_request_id='12345'), Mock())['state'], 'waiting')
        self.browser.publish.assert_not_called()

    def test_cancelled_lease_prevents_every_site_write(self):
        from highscore_literature.engine import LeaseLost
        from highscore_literature.worker import Worker
        engine = Mock()
        client = Mock()
        client.claim.return_value = {'job': self.job(), 'lease_token': 'synthetic-lease', 'lease_seconds': 180}
        client.heartbeat.return_value = {'cancel_requested': True}
        worker = Worker(self.root, client=client, engine=engine, accounts_provider=lambda: [ACCOUNT])
        self.assertFalse(worker.run_once())
        engine.assert_not_called()
        client.update.assert_not_called()

    def test_lost_lease_during_work_never_submits_late_pdf(self):
        from highscore_literature.engine import LeaseLost
        from highscore_literature.worker import Worker
        client = Mock()
        client.claim.return_value = {'job': self.job(), 'lease_token': 'synthetic-lease', 'lease_seconds': 180}
        client.heartbeat.side_effect = [{'cancel_requested': False}, LeaseLost()]
        def work(job, guard):
            guard(force=True)
            return {'state': 'succeeded', 'pdf_base64': base64.b64encode(sample_pdf()).decode()}
        worker = Worker(self.root, client=client, engine=work, accounts_provider=lambda: [ACCOUNT])
        self.assertFalse(worker.run_once())
        client.update.assert_not_called()

    def test_no_accounts_still_claims_maintenance_without_processing(self):
        from highscore_literature.worker import Worker
        client = Mock()
        client.claim.return_value = {'job': None}
        engine = Mock()
        worker = Worker(self.root, client=client, engine=engine, accounts_provider=lambda: [])
        self.assertFalse(worker.run_once())
        client.claim.assert_called_once_with(accounts_ready=False)
        engine.assert_not_called()

    def test_cli_default_never_loads_credentials_or_browser(self):
        from highscore_literature.__main__ import main
        with patch('highscore_literature.credentials.load_accounts', side_effect=AssertionError('禁止读取凭据')):
            self.assertEqual(main([]), 0)

    def test_selected_candidate_without_doi_is_respected_even_without_canonical_field(self):
        from highscore_literature.browser import Listing
        engine = self.engine()
        self.browser.list_requests.return_value = Listing([self.record()], True)
        self.browser.verify_identity.return_value = True
        selected = {'id': 'chosen', 'title': TITLE, 'doi': None, 'journal': 'Synthetic', 'year': 2026}
        result = engine(self.job(canonical_title=None, selected_candidate=selected), Mock())
        self.resolver.resolve.assert_not_called()
        self.assertEqual((result['state'], result['canonical_title'], result['doi']), ('waiting', TITLE, None))
        self.browser.attachment.assert_called_once_with('12345', TITLE, None)

    def test_metadata_distinct_journal_year_without_doi_keeps_stable_separate_candidates(self):
        import httpx
        from highscore_literature.metadata import MetadataResolver, candidates_from_items
        items = [{'title': [TITLE], 'container-title': ['Journal A'], 'issued': {'date-parts': [[2025]]}},
                 {'title': [TITLE], 'container-title': ['Journal B'], 'issued': {'date-parts': [[2026]]}}]
        candidates = candidates_from_items(items)
        self.assertEqual(len(candidates), 2)
        self.assertNotEqual(candidates[0]['id'], candidates[1]['id'])
        self.assertEqual({item['journal']: item['id'] for item in candidates},
                         {item['journal']: item['id'] for item in candidates_from_items(list(reversed(items)))})
        client = Mock()
        client.get.return_value = httpx.Response(200, json={'status': 'ok', 'message': {'items': items}})
        self.assertEqual(MetadataResolver(client).resolve(TITLE)['state'], 'needs_selection')

    def test_selected_identity_is_persisted_and_other_same_title_pdf_cannot_be_reused(self):
        from highscore_literature.browser import Attachment, Listing
        engine = self.engine()
        self.browser.list_requests.return_value = Listing([self.record()], True)
        self.browser.attachment.return_value = Attachment('f123', 'synthetic.pdf', False)
        self.browser.verify_identity.return_value = True
        self.browser.download.return_value = sample_pdf(header='Journal A 2025')
        first_identity = {'id': 'identity-a', 'title': TITLE, 'doi': None, 'journal': 'Journal A', 'year': 2025}
        second_identity = {'id': 'identity-b', 'title': TITLE, 'doi': None, 'journal': 'Journal B', 'year': 2026}
        first = engine(self.job(selected_candidate=first_identity), Mock())
        self.assertEqual(first['state'], 'succeeded')
        self.assertEqual(engine.store.read(ACCOUNT['account_ref'], TITLE)['metadata_identity'], first_identity)
        same = engine(self.job(id='same-selected-paper', selected_candidate=first_identity), Mock())
        self.assertEqual(same['state'], 'succeeded')
        self.assertEqual(same['pdf_base64'], first['pdf_base64'])
        self.factory.reset_mock()
        second = engine(self.job(id='another-selected-paper', selected_candidate=second_identity), Mock())
        self.assertEqual(second['state'], 'needs_attention')
        self.assertNotIn('pdf_base64', second)
        self.factory.assert_not_called()

    def test_selected_without_doi_requires_real_platform_identity_evidence(self):
        from highscore_literature.browser import Attachment, Listing
        engine = self.engine()
        self.browser.list_requests.return_value = Listing([self.record()], True)
        self.browser.attachment.return_value = Attachment('f123', 'synthetic.pdf', True)
        self.browser.download.return_value = sample_pdf(header='Journal A 2025')
        selected = {'id': 'identity-a', 'title': TITLE, 'doi': None, 'journal': 'Journal A', 'year': 2025}
        result = engine(self.job(selected_candidate=selected), Mock())
        self.assertEqual(result['state'], 'needs_attention')
        self.browser.download.assert_not_called()
        self.browser.accept.assert_not_called()

    def test_selected_without_doi_rejects_wrong_journal_year_in_actual_pdf(self):
        from highscore_literature.browser import Attachment, Listing
        engine = self.engine()
        self.browser.list_requests.return_value = Listing([self.record()], True)
        self.browser.attachment.return_value = Attachment('f123', 'synthetic.pdf', True)
        self.browser.verify_identity.return_value = True
        self.browser.download.return_value = sample_pdf(header='Journal A 2025')
        selected = {'id': 'identity-b', 'title': TITLE, 'doi': None, 'journal': 'Journal B', 'year': 2026}
        result = engine(self.job(selected_candidate=selected), Mock())
        self.assertEqual(result['state'], 'needs_attention')
        self.assertNotIn('pdf_base64', result)
        self.browser.accept.assert_not_called()

    def test_platform_year_footer_cannot_prove_no_doi_publication_identity(self):
        from highscore_literature.browser import platform_identity_matches
        selected = {'id': 'identity-a', 'title': TITLE, 'doi': None, 'journal': 'Journal A', 'year': 2025}
        self.assertTrue(platform_identity_matches(TITLE+'\n期刊\nJournal A\n年份\n2025', selected))
        self.assertTrue(platform_identity_matches(TITLE+'\n期刊： Journal A\n年份： 2025', selected))
        self.assertFalse(platform_identity_matches(TITLE+'\nJournal A\nCopyright 2025', selected))
        self.assertFalse(platform_identity_matches(TITLE+'\n期刊 Journal B\n年份 2025', selected))

    def test_no_doi_pdf_reference_or_received_year_cannot_prove_selected_header(self):
        from highscore_literature.engine import LiteratureError, validate_selected_pdf
        selected = {'id': 'identity-a', 'title': TITLE, 'doi': None, 'journal': 'Journal A', 'year': 2025}
        for header in ('Journal B 2026\nReferences:\nJournal A 2025', 'Journal A\nReceived 2025'):
            with self.subTest(header=header), self.assertRaises(LiteratureError):
                validate_selected_pdf(sample_pdf(header=header), selected)

    def test_title_only_legacy_cache_cannot_be_promoted_to_selected_identity(self):
        from highscore_literature.browser import Attachment, Listing
        engine = self.engine()
        self.browser.list_requests.return_value = Listing([self.record()], True)
        self.browser.attachment.return_value = Attachment('f123', 'synthetic.pdf', False)
        self.browser.download.return_value = sample_pdf()
        self.assertEqual(engine(self.job(), Mock())['state'], 'succeeded')
        selected = {'id': 'identity-a', 'title': TITLE, 'doi': None, 'journal': 'Journal A', 'year': 2025}
        result = engine(self.job(id='chosen-later', selected_candidate=selected), Mock())
        self.assertEqual(result['state'], 'needs_attention')
        self.assertNotIn('pdf_base64', result)

    def download_adapter(self, *, complete=False, oversized=False, max_seconds=.06, confirmation=False,
                         highspeed=False, highspeed_dialog=True):
        from highscore_literature.browser import AblesciBrowser
        from highscore_literature.engine import StateStore
        from highscore_literature.worker import LeaseGuard
        store = StateStore(self.root)
        client = Mock()
        client.heartbeat.return_value = {'cancel_requested': False}
        guard = LeaseGuard(client, self.job(), 'synthetic-lease', max_seconds=max_seconds)
        adapter = AblesciBrowser(self.root, ACCOUNT, store, guard)
        adapter.runtime = store.path('output/literature-worker/browser/synthetic-download')
        store._directory(adapter.runtime)
        adapter.browser = Mock()
        adapter.context = Mock()
        adapter.page = Mock()
        adapter._security = Mock(side_effect=guard)
        handlers = {}
        page_handlers = {}
        calls = []
        directory = []
        payload = sample_pdf()
        guid = 'synthetic-download-guid'
        download = Mock(url='https://www.ablesci.com/normal-download')
        download.save_as.side_effect = AssertionError('禁止未完成下载进入无界 save_as')
        continue_button = Mock()
        continue_button.is_visible.return_value = confirmation
        continue_button.is_enabled.return_value = True
        continue_button.inner_text.return_value = '继续当前下载'
        paid_buttons = {selector: Mock() for selector in ('#download-highspeed-direct', '#download-switch-vip')}
        for selector, button in paid_buttons.items():
            button.is_visible.return_value = highspeed and selector == '#download-highspeed-direct'
            button.is_enabled.return_value = True
            button.inner_text.return_value = '⚡ 高速通道 推荐 高速节点秒下载 🚀'
            button.click.side_effect = AssertionError('禁止触及高速或换线路按钮')
        class Locator:
            def __init__(self, controls=()): self.controls = list(controls)
            def count(self): return len(self.controls)
            def nth(self, index): return self.controls[index]
            @property
            def first(self): return self.controls[0]
        adapter.page.locator.side_effect = lambda selector: Locator([continue_button]) if selector == 'button#download-continue' else Locator([paid_buttons[selector]]) if selector in paid_buttons else Locator()
        cdp = Mock()
        def send(method, params=None):
            calls.append((method, params))
            if method == 'Target.getTargetInfo': return {'targetInfo': {'browserContextId': 'owned-context'}}
            if method == 'Page.getFrameTree': return {'frameTree': {'frame': {'id': 'owned-frame'}}}
            if method == 'Browser.setDownloadBehavior': directory.append(Path(params['downloadPath']))
            return {}
        cdp.send.side_effect = send
        cdp.on.side_effect = lambda name, handler: handlers.__setitem__(name, handler)
        adapter.browser.new_browser_cdp_session.return_value = cdp
        adapter.context.new_cdp_session.return_value = cdp
        adapter.page.on.side_effect = lambda name, handler: page_handlers.__setitem__(name, handler)
        started = [False]
        def begin_download():
            started[0] = True
            if 'Browser.downloadWillBegin' in handlers:
                handlers['Browser.downloadWillBegin']({'guid': guid, 'frameId': 'owned-frame', 'url': download.url})
            if 'download' in page_handlers: page_handlers['download'](download)
        def goto(url, **kwargs):
            adapter.page.url = url
            if not confirmation and not highspeed:
                begin_download()
        adapter.page.goto.side_effect = goto
        continue_button.click.side_effect = lambda **kwargs: begin_download()
        popup = [False]
        def select_highspeed(**kwargs):
            if highspeed_dialog:
                popup[0] = True
            else:
                begin_download()
        if highspeed:
            paid_buttons['#download-highspeed-direct'].click.side_effect = select_highspeed
        adapter._layer = Mock(side_effect=lambda: (Mock(), '高速通道积分规则\n用户积分＜500：单个文件高速下载扣 2 积分。\n用户积分≥500：免积分。',
                                                  ['确认使用高速通道', '取消']) if popup[0] else (None, '', []))
        def confirm_highspeed(label):
            self.assertEqual(label, '确认使用高速通道')
            self.assertTrue(popup[0])
            popup[0] = False
            begin_download()
        adapter._click_layer = Mock(side_effect=confirm_highspeed)
        completed = [False]
        def pump(milliseconds):
            if started[0] and not completed[0] and 'Browser.downloadProgress' in handlers:
                if complete:
                    directory[0].mkdir(parents=True, exist_ok=True)
                    (directory[0]/guid).write_bytes(payload)
                    state = 'completed'
                    completed[0] = True
                else:
                    state = 'inProgress'
                size = 100*1024*1024 if oversized else len(payload)
                handlers['Browser.downloadProgress']({'guid': guid, 'state': state, 'receivedBytes': size, 'totalBytes': size})
            time.sleep(min(.005, milliseconds/1000))
        adapter.page.wait_for_timeout.side_effect = pump
        target = store.entry(ACCOUNT['account_ref'], TITLE)/'incoming.pdf'
        return adapter, target, calls, download, payload

    def test_highspeed_download_is_preferred_and_confirmation_is_automatic(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LiteratureError
        adapter, target, _, _, payload = self.download_adapter(
            complete=True, max_seconds=1, confirmation=True, highspeed=True)
        try:
            data = adapter.download(Attachment('f123', 'synthetic.pdf', True), target)
        except LiteratureError as exc:
            self.fail('高速下载入口未能自动完成：' + str(exc))
        self.assertEqual(data, payload)
        adapter.page.locator('#download-highspeed-direct').first.click.assert_called_once()
        adapter._click_layer.assert_called_once_with('确认使用高速通道')
        adapter.page.locator('button#download-continue').first.click.assert_not_called()
        adapter.page.locator('#download-switch-vip').first.click.assert_not_called()

    def test_highspeed_without_reminder_still_requires_completed_download(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LiteratureError
        adapter, target, _, _, payload = self.download_adapter(
            complete=True, max_seconds=1, highspeed=True, highspeed_dialog=False)
        try:
            data = adapter.download(Attachment('f123', 'synthetic.pdf', True), target)
        except LiteratureError as exc:
            self.fail('已记住高速选择时仍应完成下载：' + str(exc))
        self.assertEqual(data, payload)
        adapter.page.locator('#download-highspeed-direct').first.click.assert_called_once()
        adapter._click_layer.assert_not_called()

    def test_highspeed_confirmation_is_not_repeated_during_slow_transfer(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LiteratureError
        adapter, target, _, _, _ = self.download_adapter(
            confirmation=True, highspeed=True, max_seconds=.2)
        with self.assertRaises(LiteratureError):
            adapter.download(Attachment('f123', 'synthetic.pdf', True), target)
        adapter.page.locator('#download-highspeed-direct').first.click.assert_called_once()
        adapter._click_layer.assert_called_once_with('确认使用高速通道')
        adapter.page.locator('button#download-continue').first.click.assert_not_called()
        self.assertFalse(target.exists())

    def test_highspeed_unknown_confirmation_stops_without_confirming(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LiteratureError
        adapter, target, _, _, _ = self.download_adapter(
            complete=True, confirmation=True, highspeed=True, max_seconds=1)
        adapter._layer.side_effect = lambda: (Mock(), '开通会员，请支付费用', ['立即支付', '取消'])
        with self.assertRaises(LiteratureError):
            adapter.download(Attachment('f123', 'synthetic.pdf', True), target)
        adapter._click_layer.assert_not_called()
        self.assertFalse(target.exists())

    def test_pending_download_is_cancelled_within_slice_without_save_as(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import SliceExpired
        real_monotonic = time.monotonic
        now = [1000.0]
        with patch('highscore_literature.worker.time.monotonic', side_effect=lambda: now[0]):
            adapter, target, calls, download, _ = self.download_adapter()
            original = adapter.page.wait_for_timeout.side_effect
            def expire_after_progress(milliseconds):
                original(milliseconds)
                now[0] = adapter.guard.until
            adapter.page.wait_for_timeout.side_effect = expire_after_progress
            started = real_monotonic()
            with self.assertRaises(SliceExpired):
                adapter.download(Attachment('f123', 'synthetic.pdf', False), target)
        self.assertLess(real_monotonic()-started, .3)
        self.assertIn(('Browser.cancelDownload', {'guid': 'synthetic-download-guid', 'browserContextId': 'owned-context'}), calls)
        download.save_as.assert_not_called()
        self.assertFalse(target.exists())

    def test_only_browser_completed_event_allows_project_file_to_be_read(self):
        from highscore_literature.browser import Attachment
        adapter, target, calls, download, payload = self.download_adapter(complete=True, max_seconds=1)
        self.assertEqual(adapter.download(Attachment('f123', 'synthetic.pdf', False), target), payload)
        self.assertEqual(target.read_bytes(), payload)
        settings = next(params for method, params in calls if method == 'Browser.setDownloadBehavior')
        self.assertEqual(settings['browserContextId'], 'owned-context')
        self.assertTrue(settings['eventsEnabled'])
        self.assertTrue(Path(settings['downloadPath']).is_relative_to(self.root))
        download.save_as.assert_not_called()
        for selector in ('button#download-continue', '#download-highspeed-direct', '#download-switch-vip'):
            adapter.page.locator(selector).first.click.assert_not_called()

    def test_normal_download_confirmation_clicks_once_then_returns_completed_pdf(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LiteratureError
        adapter, target, calls, download, payload = self.download_adapter(complete=True, max_seconds=.6, confirmation=True)
        try:
            data = adapter.download(Attachment('f123', 'synthetic.pdf', True), target)
        except LiteratureError as error:
            self.fail('普通下载确认未能进入正常 CDP 完成流程：'+str(error))
        self.assertEqual(data, payload)
        self.assertEqual(target.read_bytes(), payload)
        adapter.page.locator('button#download-continue').first.click.assert_called_once()
        for selector in ('#download-highspeed-direct', '#download-switch-vip'):
            adapter.page.locator(selector).first.click.assert_not_called()
        download.save_as.assert_not_called()
        self.assertFalse(any(method == 'Browser.cancelDownload' for method, _ in calls))

    def test_download_confirmation_is_not_reclicked_while_waiting_for_download(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LiteratureError
        adapter, target, _, _, _ = self.download_adapter(confirmation=True)
        button = adapter.page.locator('button#download-continue').first
        button.click.side_effect = None
        with self.assertRaises(LiteratureError):
            adapter.download(Attachment('f123', 'synthetic.pdf', True), target)
        button.click.assert_called_once()
        self.assertFalse(target.exists())
        for selector in ('#download-highspeed-direct', '#download-switch-vip'):
            adapter.page.locator(selector).first.click.assert_not_called()

    def test_expired_download_deadline_never_clicks_continue(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LiteratureError
        adapter, target, _, _, _ = self.download_adapter(confirmation=True, max_seconds=1800)
        original = adapter.page.goto.side_effect
        now = [time.monotonic()]
        def goto(url, **kwargs):
            original(url, **kwargs)
            now[0] += 901
        adapter.page.goto.side_effect = goto
        with patch('highscore_literature.browser.time.monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(LiteratureError):
                adapter.download(Attachment('f123', 'synthetic.pdf', True), target)
        self.assertFalse(target.exists())
        for selector in ('button#download-continue', '#download-highspeed-direct', '#download-switch-vip'):
            adapter.page.locator(selector).first.click.assert_not_called()

    def test_slow_normal_download_completes_after_more_than_45_seconds(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LiteratureError
        start = 1000.0
        now = [start]
        with patch('highscore_literature.worker.time.monotonic', side_effect=lambda: now[0]):
            adapter, target, _, _, payload = self.download_adapter(complete=True, confirmation=True, max_seconds=1800)
            button = adapter.page.locator('button#download-continue').first
            emit_download = button.click.side_effect
            button.click.side_effect = None
            original = adapter.page.wait_for_timeout.side_effect
            emitted = [False]
            def pump(milliseconds):
                now[0] += 60
                # 复现网页先接收文件，十分钟后才触发普通 CDP 下载。
                if now[0]-start >= 600 and not emitted[0]:
                    emitted[0] = True
                    emit_download()
                original(milliseconds)
            adapter.page.wait_for_timeout.side_effect = pump
            try:
                data = adapter.download(Attachment('f123', 'synthetic.pdf', True), target)
            except LiteratureError as error:
                self.fail('普通线路仍在接收文件时提前中断：'+str(error))
        self.assertEqual(data, payload)
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(now[0]-start, 600)
        button.click.assert_called_once()
        self.assertLessEqual(adapter.page.goto.call_args.kwargs['timeout'], 20000)
        for selector in ('#download-highspeed-direct', '#download-switch-vip'):
            adapter.page.locator(selector).first.click.assert_not_called()

    def test_normal_download_stops_at_900_seconds_without_extending_budget(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LiteratureError
        adapter, target, _, _, _ = self.download_adapter(confirmation=True, max_seconds=1800)
        button = adapter.page.locator('button#download-continue').first
        button.click.side_effect = None
        start = time.monotonic()
        now = [start]
        steps = iter((899, 2))
        adapter.page.wait_for_timeout.side_effect = lambda milliseconds: now.__setitem__(0, now[0]+next(steps))
        with patch('highscore_literature.browser.time.monotonic', side_effect=lambda: now[0]):
            with self.assertRaisesRegex(LiteratureError, '本轮站内下载未完成'):
                adapter.download(Attachment('f123', 'synthetic.pdf', True), target)
        self.assertEqual(now[0]-start, 901)
        button.click.assert_called_once()
        self.assertFalse(target.exists())

    def test_earlier_job_deadline_or_cancellation_stops_slow_download(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LeaseLost, LiteratureError, SliceExpired
        from highscore_literature.worker import LeaseGuard
        for stop in ('deadline', 'cancel'):
            with self.subTest(stop=stop):
                adapter, target, _, _, _ = self.download_adapter(confirmation=True, max_seconds=1800)
                button = adapter.page.locator('button#download-continue').first
                button.click.side_effect = None
                client = adapter.guard.client
                start, wall = time.monotonic(), time.time()
                now = [start]
                def pump(milliseconds):
                    now[0] += 60
                    if stop == 'cancel' and now[0]-start >= 240:
                        client.heartbeat.return_value = {'cancel_requested': True}
                        adapter.guard(force=True)
                adapter.page.wait_for_timeout.side_effect = pump
                with patch('highscore_literature.worker.time.monotonic', side_effect=lambda: now[0]), patch('highscore_literature.worker.time.time', return_value=wall):
                    adapter.guard = LeaseGuard(client, self.job(deadline=wall+(240 if stop == 'deadline' else 3600)),
                        'synthetic-lease', max_seconds=900)
                    adapter._security.side_effect = adapter.guard
                    with self.assertRaises(LiteratureError) as raised:
                        adapter.download(Attachment('f123', 'synthetic.pdf', True), target)
                self.assertIsInstance(raised.exception, SliceExpired if stop == 'deadline' else LeaseLost)
                self.assertEqual(now[0]-start, 240)
                self.assertFalse(target.exists())
                button.click.assert_called_once()

    def test_default_lease_guard_uses_900_seconds_and_honors_earlier_deadline(self):
        from highscore_literature.engine import SliceExpired
        from highscore_literature.worker import LeaseGuard
        client = Mock()
        client.heartbeat.return_value = {'cancel_requested': False}
        start, wall = 1000.0, 1_700_000_000.0
        now = [start]
        with patch('highscore_literature.worker.time.monotonic', side_effect=lambda: now[0]), patch('highscore_literature.worker.time.time', return_value=wall):
            long = LeaseGuard(client, self.job(deadline=wall+3600), 'synthetic-lease')
            short = LeaseGuard(client, self.job(deadline=wall+90), 'synthetic-lease')
            self.assertEqual(long.remaining_seconds(), 900)
            self.assertEqual(short.remaining_seconds(), 90)
            now[0] = short.until
            with self.assertRaises(SliceExpired): short()
            long()
            now[0] = long.until
            with self.assertRaises(SliceExpired): long()

    def test_default_worker_passes_900_seconds_to_real_guard_and_keeps_heartbeats(self):
        from highscore_literature.worker import LeaseGuard, Worker
        client = Mock()
        start, wall = time.monotonic(), time.time()
        now = [start]
        client.claim.return_value = {'job': self.job(deadline=wall+3600), 'lease_token': 'synthetic-lease', 'lease_seconds': .03}
        background = threading.Event()
        def heartbeat(*args):
            if threading.current_thread().name == 'literature-lease': background.set()
            return {'cancel_requested': False}
        client.heartbeat.side_effect = heartbeat
        seen = {}
        def work(job, guard):
            seen.update(kind=type(guard), remaining=guard.remaining_seconds(), interval=guard.interval)
            now[0] = start+120
            guard(force=True)
            seen['remaining_after_120'] = guard.remaining_seconds()
            background.clear()
            seen['background_renewed'] = background.wait(.5)
            return {'state': 'waiting', 'message': '离线慢速普通下载继续等待'}
        with patch('highscore_literature.worker.time.monotonic', side_effect=lambda: now[0]), patch('highscore_literature.worker.time.time', return_value=wall):
            worker = Worker(self.root, client=client, engine=work, accounts_provider=lambda: [ACCOUNT])
            processed = worker.run_once()
        self.assertEqual(seen['remaining'], 900)
        self.assertIs(seen['kind'], LeaseGuard)
        self.assertEqual(seen['remaining_after_120'], 780)
        self.assertEqual(seen['interval'], .01)
        self.assertTrue(seen['background_renewed'])
        self.assertTrue(processed)
        self.assertGreaterEqual(client.heartbeat.call_count, 3)
        client.update.assert_called_once()

    def test_download_confirmation_wrong_page_or_label_never_clicks_any_route(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LiteratureError
        cases = [('https://www.ablesci.com/assist/download?id=other', '继续当前下载'),
                 ('https://www.ablesci.com/assist/detail?id=f123', '继续当前下载'),
                 ('https://other.example.test/assist/download?id=f123', '继续当前下载'),
                 ('https://www.ablesci.com/assist/download?id=f123&token=other', '继续当前下载'),
                 (None, '继续当前下载（高速）'), (None, '重试当前下载'), (None, '切换线路')]
        for url, label in cases:
            with self.subTest(url=url, label=label):
                adapter, target, _, _, _ = self.download_adapter(confirmation=True)
                button = adapter.page.locator('button#download-continue').first
                button.inner_text.return_value = label
                original = adapter.page.goto.side_effect
                def goto(value, **kwargs):
                    original(value, **kwargs)
                    if url is not None:
                        adapter.page.url = url
                adapter.page.goto.side_effect = goto
                with self.assertRaises(LiteratureError):
                    adapter.download(Attachment('f123', 'synthetic.pdf', True), target)
                self.assertFalse(target.exists())
                for selector in ('button#download-continue', '#download-highspeed-direct', '#download-switch-vip'):
                    adapter.page.locator(selector).first.click.assert_not_called()

    def test_disabled_hidden_or_ambiguous_download_confirmation_is_not_clicked(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LiteratureError
        for state in ('disabled', 'hidden', 'duplicate'):
            with self.subTest(state=state):
                adapter, target, _, _, _ = self.download_adapter(confirmation=True)
                control = adapter.page.locator('button#download-continue')
                button = control.first
                if state == 'disabled': button.is_enabled.return_value = False
                if state == 'hidden': button.is_visible.return_value = False
                if state == 'duplicate':
                    original = adapter.page.locator.side_effect
                    duplicate = type(control)([button, button])
                    adapter.page.locator.side_effect = lambda selector: duplicate if selector == 'button#download-continue' else original(selector)
                with self.assertRaises(LiteratureError):
                    adapter.download(Attachment('f123', 'synthetic.pdf', True), target)
                self.assertFalse(target.exists())
                for selector in ('button#download-continue', '#download-highspeed-direct', '#download-switch-vip'):
                    adapter.page.locator(selector).first.click.assert_not_called()

    def test_oversized_inflight_download_is_cancelled_before_completion(self):
        from highscore_literature.browser import Attachment
        from highscore_literature.engine import LiteratureError
        adapter, target, calls, download, _ = self.download_adapter(oversized=True, max_seconds=1)
        with self.assertRaisesRegex(LiteratureError, '大小'):
            adapter.download(Attachment('f123', 'synthetic.pdf', False), target)
        self.assertTrue(any(method == 'Browser.cancelDownload' for method, _ in calls))
        download.save_as.assert_not_called()
        self.assertFalse(target.exists())

    def test_same_paper_reuses_original_account_even_if_account_order_changes(self):
        from highscore_literature.browser import Listing
        engine = self.engine()
        engine.store.write(ACCOUNT['account_ref'], TITLE, {'phase': 'waiting', 'external_request_id': '12345'})
        engine.accounts = [{**ACCOUNT, 'account_ref': 'synthetic-b'}, ACCOUNT]
        self.browser.list_requests.return_value = Listing([self.record()], True)
        result = engine(self.job(id='another-website-job'), Mock())
        self.assertEqual(result['account_ref'], ACCOUNT['account_ref'])
        self.assertEqual(self.factory.call_args.args[1]['account_ref'], ACCOUNT['account_ref'])
        self.browser.publish.assert_not_called()

    def test_near_title_variant_is_not_silently_bound_as_the_requested_paper(self):
        from highscore_literature.browser import Listing
        engine = self.engine()
        self.browser.list_requests.return_value = Listing([self.record(title=TITLE+' II')], True)
        result = engine(self.job(), Mock())
        self.assertEqual(result['state'], 'needs_attention')
        self.assertNotIn('external_request_id', result)
        self.browser.publish.assert_not_called()

    def test_removed_bound_account_does_not_switch_to_another_account(self):
        engine = self.engine()
        engine.accounts = [{**ACCOUNT, 'account_ref': 'synthetic-b'}]
        result = engine(self.job(account_ref=ACCOUNT['account_ref'], external_request_id='12345'), Mock())
        self.assertEqual(result['state'], 'needs_attention')
        self.factory.assert_not_called()

    def test_acceptance_retry_reuses_verified_bytes_and_never_downloads_again(self):
        from highscore_literature.browser import Attachment, Listing
        engine = self.engine()
        self.browser.list_requests.return_value = Listing([self.record()], True)
        self.browser.attachment.return_value = Attachment('f123', 'synthetic.pdf', True)
        self.browser.download.return_value = sample_pdf()
        def accept(*args):
            self.assertIsNotNone(engine.store.cached(ACCOUNT['account_ref'], TITLE, None))
            if self.browser.accept.call_count == 1:
                raise OSError('synthetic-private-password')
        self.browser.accept.side_effect = accept
        first = engine(self.job(), Mock())
        self.assertEqual(first['state'], 'waiting')
        second = engine(self.job(), Mock())
        self.assertEqual(second['state'], 'succeeded')
        self.assertEqual(self.browser.download.call_count, 1)
        self.assertEqual(self.browser.accept.call_count, 2)

    def test_publish_bind_survives_later_download_failure(self):
        engine = self.engine()
        def publish(title, doi, before_submit):
            before_submit()
            return self.record()
        self.browser.publish.side_effect = publish
        self.browser.attachment.side_effect = OSError('token=synthetic-private-password')
        result = engine(self.job(), Mock())
        self.assertEqual((result['state'], result['external_request_id']), ('waiting', '12345'))
        self.assertEqual(engine.store.read(ACCOUNT['account_ref'], TITLE)['external_request_id'], '12345')
        self.assertNotIn('synthetic-private-password', result['message'])

    def test_background_heartbeat_loss_aborts_long_operation(self):
        from highscore_literature.engine import LeaseLost
        from highscore_literature.worker import Worker
        client = Mock()
        client.claim.return_value = {'job': self.job(), 'lease_token': 'synthetic-lease', 'lease_seconds': .03}
        client.heartbeat.side_effect = [{'cancel_requested': False}, OSError('private cookie')]
        stopped = threading.Event()
        def work(job, guard):
            for _ in range(100):
                time.sleep(.005)
                try:
                    guard()
                except LeaseLost:
                    stopped.set()
                    raise
            self.fail('续租失败后未停止')
        worker = Worker(self.root, client=client, engine=work, accounts_provider=lambda: [ACCOUNT])
        self.assertFalse(worker.run_once())
        self.assertTrue(stopped.is_set())
        client.update.assert_not_called()

    def test_api_transport_uses_only_independent_bearer_and_preserves_lease(self):
        import httpx
        from highscore_literature.worker import ApiClient, load_config
        calls = []
        def handle(request):
            calls.append((request.url.path, request.headers.get('Authorization'), json.loads(request.content)))
            return httpx.Response(200, json={'cancel_requested': False})
        config = load_config(self.root, {'LITERATURE_WORKER_TOKEN': 'synthetic-private-literature-token-123456',
            'HIGHSCORE_WORKER_TOKEN': 'never-use-the-xrd-token'})
        client = ApiClient(config, transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        self.assertFalse(client.heartbeat('synthetic-job', 'synthetic-lease')['cancel_requested'])
        self.assertEqual(calls[0], ('/highscore/api/worker/literature/jobs/synthetic-job/heartbeat',
            'Bearer synthetic-private-literature-token-123456', {'lease_token': 'synthetic-lease'}))
        self.assertNotIn(config.token, repr(config))

    def test_api_conflict_stops_lease_and_does_not_echo_server_secrets(self):
        import httpx
        from highscore_literature.engine import LeaseLost
        from highscore_literature.worker import ApiClient, load_config
        config = load_config(self.root, {'LITERATURE_WORKER_TOKEN': 'synthetic-private-literature-token-123456'})
        client = ApiClient(config, transport=httpx.MockTransport(lambda _: httpx.Response(409,
            json={'detail': 'Cookie=synthetic-private-secret'})))
        self.addCleanup(client.close)
        with self.assertRaises(LeaseLost) as raised:
            client.heartbeat('synthetic-job', 'synthetic-lease')
        self.assertNotIn('synthetic-private-secret', str(raised.exception))

    def test_config_rejects_missing_independent_token_and_embedded_secrets(self):
        from highscore_literature.worker import WorkerError, load_config
        for values in ({'HIGHSCORE_WORKER_TOKEN': 'synthetic-private-token-12345678901234567890'},
                       {'LITERATURE_SERVER_URL': 'https://user:secret@example.test/highscore/api'},
                       {'LITERATURE_SERVER_URL': 'https://example.test/highscore/api?token=secret'},
                       {'LITERATURE_SERVER_URL': 'ftp://example.test/highscore/api'}):
            with self.subTest(values=list(values)), self.assertRaises(WorkerError):
                load_config(self.root, values)

    def test_metadata_multiple_distinct_dois_require_selection(self):
        import httpx
        from highscore_literature.metadata import MetadataResolver
        client = Mock()
        client.get.return_value = httpx.Response(200, json={'status': 'ok', 'message': {'items': [
            {'title': [TITLE], 'DOI': '10.1234/a', 'container-title': ['Synthetic'], 'issued': {'date-parts': [[2026]]}},
            {'title': [TITLE], 'DOI': '10.1234/b', 'container-title': ['Synthetic'], 'issued': {'date-parts': [[2025]]}}]}})
        result = MetadataResolver(client).resolve(TITLE)
        self.assertEqual(result['state'], 'needs_selection')
        self.assertEqual(len(result['candidates']), 2)
        self.assertNotEqual(result['candidates'][0]['id'], result['candidates'][1]['id'])

    def test_metadata_exact_unique_identity_and_html_cleanup(self):
        import httpx
        from highscore_literature.metadata import MetadataResolver
        client = Mock()
        client.get.return_value = httpx.Response(200, json={'status': 'ok', 'message': {'items': [
            {'title': ['<i>'+TITLE+'</i>'], 'DOI': 'https://doi.org/10.1234/test'},
            {'title': ['An unrelated synthetic title'], 'DOI': '10.1234/another'}]}})
        result = MetadataResolver(client).resolve(TITLE)
        self.assertEqual((result['canonical_title'], result['doi']), (TITLE, '10.1234/test'))
        self.assertEqual(result['candidate']['title'], TITLE)
        self.assertTrue(result['candidate']['id'].startswith('crossref-'))

    def test_metadata_connection_error_never_echoes_query_token(self):
        from highscore_literature.engine import LiteratureError
        from highscore_literature.metadata import MetadataResolver
        client = Mock()
        client.get.side_effect = OSError('https://example.test/?token=synthetic-private-secret')
        with self.assertRaises(LiteratureError) as raised:
            MetadataResolver(client).resolve(TITLE)
        self.assertEqual(raised.exception.state, 'waiting')
        self.assertNotIn('synthetic-private-secret', str(raised.exception))

    def test_site_urls_reject_offsite_tokens_and_duplicate_binding(self):
        from highscore_literature.engine import LiteratureError
        from highscore_literature.browser import request_id, site_url
        self.assertEqual(request_id('https://www.ablesci.com/assist/detail?id=12345'), '12345')
        for url in ('https://evil.example/assist/download?id=f123',
                    'https://www.ablesci.com/assist/download?id=f123&token=secret',
                    'https://www.ablesci.com/assist/download?id=f123&id=other',
                    'https://user:secret@www.ablesci.com/assist/download?id=f123',
                    'http://www.ablesci.com/assist/download?id=f123'):
            with self.subTest(url=url), self.assertRaises(LiteratureError):
                site_url(url, '/assist/download', 'f123')

    def test_extracted_popup_title_is_parsed_without_similarity_suffix(self):
        from highscore_literature.browser import extracted_identity
        self.assertEqual(extracted_identity('标题 '+TITLE+' 相似度 100% DOI 10.1234/test 期刊 Synthetic'),
                         (TITLE, '10.1234/test'))

    def test_browser_cleanup_stops_driver_and_keeps_all_temporary_files_in_project(self):
        from highscore_literature.browser import AblesciBrowser
        from highscore_literature.engine import StateStore
        manager = Mock()
        driver = manager.start.return_value
        page = driver.chromium.launch.return_value.new_context.return_value.new_page.return_value
        module = types.ModuleType('playwright.sync_api')
        module.sync_playwright = Mock(return_value=manager)
        modules = {'playwright': types.ModuleType('playwright'), 'playwright.sync_api': module}
        original = os.environ.get('TMP')
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {}, clear=False):
            with AblesciBrowser(self.root, ACCOUNT, StateStore(self.root), Mock()) as browser:
                runtime = browser.runtime
                self.assertTrue(runtime.is_relative_to(self.root))
                self.assertEqual(os.environ['TMP'], str(runtime))
            self.assertFalse(runtime.exists())
            self.assertEqual(os.environ.get('TMP'), original)
        driver.stop.assert_called_once()
        page.set_default_timeout.assert_called_once()

    def test_page_adapter_refuses_multiple_attachments(self):
        from highscore_literature.browser import AblesciBrowser
        from highscore_literature.engine import LiteratureError
        browser = AblesciBrowser(self.root, ACCOUNT, None, Mock())
        browser.page = Mock()
        browser._goto = Mock()
        browser._clear_notification = Mock()
        browser._verify_detail = Mock()
        browser.page.locator.return_value.count.return_value = 2
        with self.assertRaisesRegex(LiteratureError, '多个附件'):
            browser.attachment('12345', TITLE, None)

    def test_page_adapter_inspects_next_page_before_certifying_completeness(self):
        from highscore_literature.browser import AblesciBrowser
        from highscore_literature.engine import StateStore
        browser = AblesciBrowser(self.root, ACCOUNT, StateStore(self.root), Mock())
        browser.page = Mock()
        browser.page.url = 'https://www.ablesci.com/my/assist-my'
        browser._clear_notification = Mock()
        browser._security = Mock()
        index = [0]
        def normal_goto(url, *_):
            index[0] = 1 if 'page=2' in url else 0
            browser.page.url = url
        browser._goto = Mock(side_effect=normal_goto)
        browser._records = Mock(side_effect=lambda: [self.record('12345' if index[0] else 'other')])
        class Control:
            def is_visible(self): return True
            def get_attribute(self, name, **kwargs):
                return {'href': '/my/assist-my?page=2', 'class': 'layui-disabled' if index[0] else ''}.get(name)
        class Locator:
            def __init__(self, count=1): self.size=count
            def count(self): return self.size
            def nth(self, _): return Control()
        browser.page.locator.side_effect = lambda selector: Locator(0 if selector == '.pagination' else 1)
        result = browser.list_requests()
        self.assertTrue(result.complete)
        self.assertEqual({item.id for item in result.records}, {'other', '12345'})
        self.assertEqual(browser._records.call_count, 3)


if __name__ == '__main__':
    unittest.main()
