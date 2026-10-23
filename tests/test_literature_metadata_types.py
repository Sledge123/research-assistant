"""正文候选必须排除Crossref明确标识的附件与审稿记录。"""
import unittest
from unittest.mock import Mock

import httpx

from highscore_literature.metadata import MetadataResolver, candidates_from_items


TITLE = 'Early Gas Evolution during Electrode Polarization in Water-in-Salt Electrolyte'


def record(doi, kind, journal=None):
    return {'title': [TITLE], 'DOI': doi, 'type': kind,
            'container-title': [journal] if journal else [],
            'issued': {'date-parts': [[2026]]}}


class MetadataTypeTests(unittest.TestCase):
    def resolve(self, items):
        client = Mock()
        client.get.return_value = httpx.Response(200, json={
            'status': 'ok', 'message': {'items': items}})
        return MetadataResolver(client).resolve(TITLE)

    def test_supporting_component_is_not_a_fulltext_candidate(self):
        items = [record('10.1021/jacs.6c13982.s001', 'component'),
                 record('10.1021/jacs.6c13982', 'journal-article', 'Journal of the American Chemical Society')]
        result = self.resolve(items)
        self.assertEqual(result.get('doi'), '10.1021/jacs.6c13982')
        self.assertNotIn('candidates', result)

    def test_peer_review_is_excluded_even_when_it_has_an_exact_title(self):
        result = self.resolve([record('10.1234/review', 'peer-review')])
        self.assertEqual(result['state'], 'needs_attention')

    def test_same_title_journal_and_conference_still_require_selection(self):
        result = self.resolve([
            record('10.1021/jacs.6c13982.s001', 'component'),
            record('10.1021/jacs.6c13982', 'journal-article', 'JACS'),
            record('10.1149/ma2026-01472284mtgabs', 'journal-article', 'ECS Meeting Abstracts')])
        self.assertEqual(result['state'], 'needs_selection')
        self.assertEqual({c['doi'] for c in result['candidates']},
                         {'10.1021/jacs.6c13982', '10.1149/ma2026-01472284mtgabs'})

    def test_excluded_record_does_not_hide_later_valid_record_with_same_doi(self):
        items = [record('10.1234/paper', 'component'), record('10.1234/paper', 'journal-article', 'Synthetic Journal')]
        result = self.resolve(items)
        self.assertEqual(result['candidate']['journal'], 'Synthetic Journal')

    def test_type_filter_uses_source_type_instead_of_guessing_doi_suffix(self):
        result = self.resolve([record('10.1234/legitimate.s001', 'journal-article')])
        self.assertEqual(result['doi'], '10.1234/legitimate.s001')

    def test_missing_type_remains_compatible_and_wrong_type_shape_is_rejected(self):
        item = record('10.1234/legacy', None)
        item.pop('type')
        self.assertEqual(len(candidates_from_items([item])), 1)
        self.assertEqual(candidates_from_items([record('10.1234/malformed', {'type': 'journal-article'})]), [])


if __name__ == '__main__':
    unittest.main()
