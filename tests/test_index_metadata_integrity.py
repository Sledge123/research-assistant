"""索引报告必须逐字段对应原始记录，不能接受格式一致的虚构元数据。"""
import copy
from pathlib import Path
import tempfile
import unittest

from highscore_database import reporting, reporting_v3
from highscore_database.jade_index import validate_index_card
from highscore_database.reader import DatabaseError
from highscore_downloader.archive import ArchiveError, ArchiveStore
from test_jade_index_reader import make_card_fixture


ROOT = Path(__file__).resolve().parents[1]


class IndexMetadataIntegrityTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.cache/tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='index-metadata-', dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def rendered_card(self, card, version):
        card = copy.deepcopy(card)
        renderer = reporting_v3 if version == 3 else reporting
        card['verification'] = {
            'method': reporting.INDEX_REPORT_METHOD,
            'rtf_origin': reporting.INDEX_REPORT_ORIGIN,
            'native_highscore_export': False,
            'report_version': version,
            'completeness_scope': 'verified_index_records',
        }
        card['report_fields'] = renderer.report_fields(card)
        card['raw_text'] = renderer.report_text(card)
        return card, renderer.report_rtf(card)

    def test_original_v3_and_v4_index_archives_are_still_valid(self):
        for version in (3, 4):
            with self.subTest(version=version):
                root = self.root / str(version)
                root.mkdir()
                card, payload = self.rendered_card(make_card_fixture(), version)
                rtf = root / 'synthetic.rtf'
                rtf.write_bytes(payload)
                store = ArchiveStore(root)
                receipt = store.save(card, rtf, card['code'])
                self.assertEqual(store.validate(receipt['receipt_path']), receipt)

    def test_reconstruction_rejects_injected_or_missing_scientific_metadata(self):
        mutations = (
            ('cell_parameters', lambda card: card['cell_parameters'].update(a='999')),
            ('space_group', lambda card: card.update(space_group='Invented group')),
            ('comments', lambda card: card.update(comments='Invented annotation')),
            ('literature', lambda card: card.update(literature=[{'c3authors': 'Invented author'}])),
            ('subfiles', lambda card: card.update(subfiles=['Invented subset'])),
            ('empirical_formula', lambda card: card.update(empirical_formula='Invented formula')),
            ('reference_alias', lambda card: card.update(reference='Invented reference')),
            ('comment_alias', lambda card: card.update(comment='Invented comment alias')),
            ('structure_alias', lambda card: card.update(structure={
                'cell': {'a': '999'}, 'space_group': {'symbol': 'Invented group', 'number': 1}})),
            ('missing_metadata', lambda card: card.pop('space_group')),
        )
        for name, mutate in mutations:
            with self.subTest(field=name):
                card = make_card_fixture()
                mutate(card)
                with self.assertRaises(DatabaseError):
                    validate_index_card(card)

    def test_peak_values_must_preserve_original_types_and_field_set(self):
        mutations = (
            ('database_index_bool', lambda peak: peak.update(database_index=False)),
            ('database_index_float', lambda peak: peak.update(database_index=0.0)),
            ('byte_start_float', lambda peak: peak.update(rid_byte_start_zero=float(peak['rid_byte_start_zero']))),
            ('unexpected_peak_field', lambda peak: peak.update(unverified_intensity='100')),
        )
        for name, mutate in mutations:
            with self.subTest(field=name):
                card = make_card_fixture()
                mutate(card['peaks'][0])
                with self.assertRaises(DatabaseError):
                    validate_index_card(card)

    def test_archive_rejects_false_metadata_even_when_readable_formats_match(self):
        for version in (3, 4):
            with self.subTest(version=version):
                root = self.root / ('changed-' + str(version))
                root.mkdir()
                source = make_card_fixture()
                source['cell_parameters']['a'] = '999'
                source['space_group'] = 'Invented group'
                source['comments'] = 'Invented annotation'
                card, payload = self.rendered_card(source, version)
                rtf = root / 'synthetic.rtf'
                rtf.write_bytes(payload)
                with self.assertRaises(ArchiveError):
                    ArchiveStore(root).save(card, rtf, card['code'])


if __name__ == '__main__':
    unittest.main()
