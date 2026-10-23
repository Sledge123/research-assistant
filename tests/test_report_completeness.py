"""跨来源通用报告栏目与元数据复验；不使用真实卡号白名单。"""
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from highscore_database.exporter import DatabaseExporter
from highscore_database.reader import DatabaseError, DatabaseReader
from highscore_database import reporting, reporting_v3
from highscore_database.validation import refresh_card, validate_database_card
from highscore_downloader.archive import ArchiveError, ArchiveStore
from test_database_reader import fixture
from test_jade_index_reader import make_card_fixture
from test_jade_structure_reader import fixture_source
from highscore_database.jade_structure import FORMAT, JadeStructureReader
from highscore_database.jade_structure_build import build_database

ROOT = Path(__file__).resolve().parents[1]


class FixedReader:
    def __init__(self, card): self.value = card
    def card(self, code): return copy.deepcopy(self.value)


class ReportCompletenessTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/tests'; cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=cache, prefix='report-completeness-')
        self.addCleanup(temporary.cleanup); self.root = Path(temporary.name)
        path = self.root / 'synthetic.hsrdb'; fixture(path)
        self.database_card = DatabaseReader(path).card('09-999-0002')
        structure_path = self.root / 'structure.sqlite3'
        built = build_database(fixture_source(self.root / 'source'), structure_path)
        structure_reader = JadeStructureReader(structure_path, profile={
            'format': FORMAT, 'source': 'Synthetic structure', 'sha256': built['sha256']})
        self.structure_card = structure_reader.card('ICSD-000001')
        self.index_card = make_card_fixture(code='09-999-0003')

    def export(self, card, suffix='current'):
        target = self.root / suffix; target.mkdir(exist_ok=True)
        receipt = DatabaseExporter(target, FixedReader(card)).export(card['code'])
        data = json.loads((target / receipt['files']['json']['path']).read_text(encoding='utf8'))
        return target, receipt, data

    def test_every_source_uses_same_metadata_sections_with_explicit_missing_values(self):
        headings = ('名称与化学式', '晶体学参数', '子库与质量', '注释', '参考文献')
        for card in (self.database_card, self.index_card, self.structure_card):
            with self.subTest(kind=card.get('data_type')):
                text = reporting.report_text(card)
                self.assertEqual([text.splitlines().count(title) for title in headings], [1] * len(headings))
                for field in ('矿物名称：', '化合物名称：', '化学式：', '经验式：', '空间群：', '空间群编号：', 'a (Å)：'):
                    self.assertIn(field, text)
                self.assertIn('未提供', text)
        self.assertIn('空间群：\t未提供', reporting.report_text(self.index_card))
        self.assertNotIn('峰表', reporting.report_text(self.structure_card))

    def test_structure_comment_and_full_reference_are_present_in_both_readable_formats(self):
        target, receipt, card = self.export(self.structure_card)
        fields = reporting.report_fields(card)
        self.assertEqual(fields['comments'], reporting.clean_text(self.structure_card['comment']))
        self.assertEqual(fields['references'][0]['text'], reporting.clean_text(self.structure_card['reference']))
        for expected in (fields['comments'], fields['references'][0]['text']):
            self.assertIn(expected, card['raw_text'])
            self.assertIn(expected, (target / receipt['files']['txt']['path']).read_text(encoding='utf8'))
        DatabaseExporter._verify_rtf(target / receipt['files']['rtf']['path'], card['raw_text'])
        self.assertEqual(card['database_raw'], self.structure_card['database_raw'])
        self.assertEqual(card['structure'], self.structure_card['structure'])

    def test_raw_reconstruction_covers_non_peak_scientific_metadata(self):
        self.assertEqual(refresh_card(self.database_card), self.database_card)
        mutations = (
            lambda card: card.update(phase_name='omitted original name'),
            lambda card: card.update(chemical_formula='Wrong'),
            lambda card: card.update(empirical_formula='Wrong'),
            lambda card: card.update(comments='truncated'),
            lambda card: card.update(space_group='P-1'),
            lambda card: card['cell_parameters'].update(a='99'),
            lambda card: card.update(literature=[]),
            lambda card: card.update(subfiles=['invented']),
        )
        for mutate in mutations:
            changed = copy.deepcopy(self.database_card); mutate(changed)
            with self.subTest(mutation=mutate), self.assertRaises(DatabaseError):
                validate_database_card(changed)

    def test_repeated_subfile_relations_keep_original_join_values_and_types(self):
        path = self.root / 'repeated-subfiles.hsrdb'; fixture(path)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO Subfiles VALUES (7,'Synthetic duplicate subset')")
            db.executemany('INSERT INTO SubFileRef VALUES (?,7)', [(2,), (2,)])
        source = DatabaseReader(path).card('09-999-0002')
        self.assertEqual(source['subfiles'], ['Synthetic duplicate subset'] * 2)
        self.assertEqual(refresh_card(source), source)

    def test_raw_display_cannot_diverge_from_physical_single_card_rows(self):
        for section, key, value in (('indexed', 'A', 99.0), ('text', 'c1compoundname', 'different'),
                                    ('stored', 'Comment', 'missing original annotation')):
            changed = copy.deepcopy(self.database_card); changed['database_raw'][section][key] = value
            with self.subTest(section=section), self.assertRaises(DatabaseError):
                refresh_card(changed)
        changed = copy.deepcopy(self.database_card)
        changed['database_raw']['source_records']['tables']['literature_text_content']['rows'] = []
        with self.assertRaises(DatabaseError): refresh_card(changed)

    def test_server_rejects_metadata_loss_even_when_file_digest_is_recomputed(self):
        target, receipt, card = self.export(self.database_card)
        card['literature'] = []
        path = target / receipt['files']['json']['path']
        payload = (json.dumps(card, ensure_ascii=False) + '\n').encode('utf8'); path.write_bytes(payload)
        receipt['files']['json'].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        (target / receipt['receipt_path']).write_text(json.dumps(receipt), encoding='utf8')
        with self.assertRaisesRegex(ArchiveError, '原始记录'):
            ArchiveStore(target).validate(receipt['receipt_path'])

    def test_all_new_hsrdb_reports_recompute_text_and_rtf(self):
        target, receipt, card = self.export(self.database_card)
        card['raw_text'] = 'lost all scientific sections'
        for kind, payload in (('json', (json.dumps(card, ensure_ascii=False)+'\n').encode('utf8')),
                              ('txt', card['raw_text'].encode('utf8'))):
            (target / receipt['files'][kind]['path']).write_bytes(payload)
            receipt['files'][kind].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        (target / receipt['receipt_path']).write_text(json.dumps(receipt), encoding='utf8')
        with self.assertRaisesRegex(ArchiveError, 'TXT 或 RTF'):
            ArchiveStore(target).validate(receipt['receipt_path'])

    def test_legacy_v3_index_and_structure_archives_remain_byte_exact_valid(self):
        for source in (self.index_card, self.structure_card):
            card = copy.deepcopy(source)
            structure = card.get('data_type') == 'jade_structure_data'
            method = reporting.STRUCTURE_REPORT_METHOD if structure else reporting.INDEX_REPORT_METHOD
            origin = reporting.STRUCTURE_REPORT_ORIGIN if structure else reporting.INDEX_REPORT_ORIGIN
            card['verification'] = {'method': method, 'rtf_origin': origin, 'native_highscore_export': False,
                                    'report_version': 3, 'completeness_scope': 'verified_structure_records' if structure else 'verified_index_records'}
            card['report_fields'] = reporting_v3.report_fields(card)
            card['raw_text'] = reporting_v3.report_text(card)
            target = self.root / ('legacy-structure' if structure else 'legacy-index'); target.mkdir()
            original = reporting_v3.report_rtf(card); path = target / 'legacy.rtf'; path.write_bytes(original)
            receipt = ArchiveStore(target).save(card, path, card['code'])
            self.assertEqual(ArchiveStore(target).validate(receipt['receipt_path']), receipt)
            self.assertEqual(reporting.report_rtf(card), original)
            self.assertEqual((target / receipt['files']['rtf']['path']).read_bytes(), original)

    def test_new_version_has_independent_directory_and_preserves_old_directories(self):
        self.assertEqual(reporting.REPORT_VERSION, 6)
        self.assertEqual(reporting.REPORT_DIRECTORY, 'report-v6')
        self.assertIn('report-v4', reporting.PREVIOUS_REPORT_DIRECTORIES)
        self.assertIn('report-v3', reporting.PREVIOUS_REPORT_DIRECTORIES)
        self.assertIn('report-v2', reporting.PREVIOUS_REPORT_DIRECTORIES)


if __name__ == '__main__': unittest.main()
