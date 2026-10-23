"""发布前的标题/DOI回退：模拟网页边界，运行真实提取与身份核验。"""
from pathlib import Path
import unittest
from unittest.mock import Mock, call, patch

from highscore_literature.browser import AblesciBrowser, EXTRACTION_LABELS
from highscore_literature.engine import LiteratureError

TITLE = 'Hydration channel engineering in Tröger’s base-POP membranes for mono/multivalent cation and anion discrimination'
DOI = '10.1126/sciadv.aef9368'
EMPTY = '标题 undefined\nDOI undefined\n期刊\n作者\n出版日期\n网址 undefined'
VALID = '标题 '+TITLE+'\nDOI '+DOI+'\n期刊 Science Advances\n出版日期 2026-10-09'


class LiteratureDoiFallbackTests(unittest.TestCase):
    def adapter(self, messages):
        browser = AblesciBrowser(Path(__file__).resolve().parents[1], {}, None, Mock())
        browser.page = Mock()
        form, query, search = Mock(), Mock(), Mock()
        form.count.return_value = 1
        query.is_visible.return_value = True
        controls = {'#assist-create-form': form, '#onekey': query, '.onekey-search': search}
        browser.page.locator.side_effect = controls.__getitem__
        browser._visible = Mock(return_value=[search])
        current = [None]
        def goto(url, *_):
            browser.page.url = url
            current[0] = None
        def fill(control, value):
            self.assertIs(control, query)
            current[0] = value
        browser._goto = Mock(side_effect=goto)
        browser._clear_notification = Mock()
        browser._confirm_scan_before_publish = Mock()
        browser._wait_for_doi_retry = Mock()
        browser._fill = Mock(side_effect=fill)
        browser._click = Mock()
        browser._layer = Mock(side_effect=lambda: (object(), messages[current[0]], list(EXTRACTION_LABELS)))
        def wait(predicate, **kwargs):
            value = predicate()
            if not value:
                raise LiteratureError(kwargs['message'])
            return value
        browser._wait = Mock(side_effect=wait)
        before_submit = Mock()
        def publish(label):
            self.assertEqual(before_submit.call_count, 1)
            self.assertEqual(label, EXTRACTION_LABELS[0])
            browser.page.url = 'https://www.ablesci.com/assist/detail?id=synthetic-original'
        browser._click_layer = Mock(side_effect=publish)
        browser._verify_detail = Mock()
        return browser, before_submit, query

    def test_title_undefined_falls_back_to_bound_doi_before_single_publish(self):
        browser, before, query = self.adapter({TITLE: EMPTY, DOI: VALID})
        request = browser.publish(TITLE, DOI, before)
        self.assertEqual(request.id, 'synthetic-original')
        self.assertEqual(browser._fill.call_args_list, [call(query, TITLE), call(query, DOI)])
        browser._wait_for_doi_retry.assert_called_once_with()
        before.assert_called_once_with()
        browser._click_layer.assert_called_once_with(EXTRACTION_LABELS[0])
        browser._verify_detail.assert_called_once_with('synthetic-original', TITLE, DOI)

    def test_successful_title_does_not_repeat_extraction(self):
        browser, before, query = self.adapter({TITLE: VALID})
        browser.publish(TITLE, DOI, before)
        browser._fill.assert_called_once_with(query, TITLE)
        browser._wait_for_doi_retry.assert_not_called()
        before.assert_called_once_with()

    def test_fallback_supports_other_papers_without_a_title_allowlist(self):
        title='A different publication with an independent identity'
        doi='10.1234/independent-paper'
        browser,before,query=self.adapter({title:EMPTY,doi:'标题 '+title+'\nDOI '+doi+'\n期刊 Example'})
        browser.publish(title,doi,before)
        self.assertEqual(browser._fill.call_args_list,[call(query,title),call(query,doi)])
        browser._verify_detail.assert_called_once_with('synthetic-original',title,doi)
        before.assert_called_once_with()

    def test_title_or_doi_mismatch_retries_doi_and_preserves_expected_identity(self):
        for incorrect in (VALID.replace(TITLE, 'Another publication'), VALID.replace(DOI, DOI+'2')):
            with self.subTest(incorrect=incorrect):
                browser, before, query = self.adapter({TITLE: incorrect, DOI: VALID})
                browser.publish(TITLE, DOI, before)
                self.assertEqual(browser._fill.call_args_list, [call(query, TITLE), call(query, DOI)])
                browser._verify_detail.assert_called_once_with('synthetic-original', TITLE, DOI)

    def test_missing_extracted_title_can_retry_bound_doi(self):
        browser, before, _ = self.adapter({TITLE: 'DOI undefined', DOI: VALID})
        browser.publish(TITLE, DOI, before)
        before.assert_called_once_with()

    def test_fallback_must_match_both_title_and_full_doi(self):
        for incorrect in (VALID.replace(TITLE, 'Another publication'), VALID.replace(DOI, DOI+'2'), EMPTY):
            with self.subTest(incorrect=incorrect):
                browser, before, query = self.adapter({TITLE: EMPTY, DOI: incorrect})
                with self.assertRaises(LiteratureError):
                    browser.publish(TITLE, DOI, before)
                self.assertEqual(browser._fill.call_args_list, [call(query, TITLE), call(query, DOI)])
                before.assert_not_called()
                browser._click_layer.assert_not_called()

    def test_no_bound_doi_cannot_invent_fallback_or_publish(self):
        browser, before, query = self.adapter({TITLE: EMPTY})
        with self.assertRaises(LiteratureError):
            browser.publish(TITLE, None, before)
        browser._fill.assert_called_once_with(query, TITLE)
        before.assert_not_called()
        browser._click_layer.assert_not_called()

    def test_security_challenge_does_not_trigger_fallback(self):
        browser, before, query = self.adapter({TITLE: EMPTY, DOI: VALID})
        browser._fill.side_effect = LiteratureError('站点要求验证码，请人工处理。')
        with self.assertRaisesRegex(LiteratureError, '验证码'):
            browser.publish(TITLE, DOI, before)
        browser._fill.assert_called_once_with(query, TITLE)
        before.assert_not_called()
        browser._click_layer.assert_not_called()

    def test_rate_limit_stops_round_and_requests_delayed_retry(self):
        browser,before,query=self.adapter({TITLE:EMPTY,DOI:VALID})
        browser._layer.return_value=None
        browser._layer.side_effect=lambda:(object(),'对不起，您的查询频率太快，请休息一下',['确定'])
        with self.assertRaises(LiteratureError) as raised:
            browser.publish(TITLE,DOI,before)
        self.assertEqual(raised.exception.state,'waiting')
        browser._fill.assert_called_once_with(query,TITLE)
        browser._wait_for_doi_retry.assert_not_called()
        before.assert_not_called()
        browser._click_layer.assert_not_called()

    def test_doi_retry_wait_checks_security_and_uses_thirty_second_interval(self):
        browser,_,_=self.adapter({TITLE:EMPTY,DOI:VALID})
        browser._security=Mock()
        browser._pause=Mock()
        with patch('highscore_literature.browser.time.monotonic',side_effect=[100,100,131]):
            AblesciBrowser._wait_for_doi_retry(browser)
        browser._security.assert_called_once_with()
        browser._pause.assert_called_once_with(200,130)


if __name__ == '__main__':
    unittest.main()
