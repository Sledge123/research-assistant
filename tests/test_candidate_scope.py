"""候选数据范围必须与实际来源类型一致，不从编号或显示名称猜测。"""
import copy
import unittest


class CandidateScopeTests(unittest.TestCase):
    def scope(self, candidate, **settings):
        from highscore_database.candidate_scope import candidate_scope
        return candidate_scope(candidate, **settings)

    def test_index_profile_explains_metadata_limits_and_original_intensity(self):
        result = self.scope({'content_type': 'diffraction', 'peak_count': 67},
                            reader_profile='jade_pdf4_2009_index_v1')
        self.assertEqual(set(result), {'report_type_label', 'report_scope_detail', 'possible_truncation'})
        self.assertEqual(result['report_type_label'], '索引报告')
        for value in ('索引峰表', '晶胞', '空间群', '参考文献', '原始强度', '百分强度', '2θ'):
            self.assertIn(value, result['report_scope_detail'])
        self.assertNotIn('未提供百分强度', result['report_scope_detail'])
        self.assertNotIn('HKL', result['report_scope_detail'])
        self.assertFalse(result['possible_truncation'])

    def test_archived_index_data_type_matches_profile(self):
        value = {'data_type': 'jade_index_data', 'peak_count': 118}
        self.assertEqual(self.scope(value), self.scope(value, reader_profile='jade_pdf4_2009_index_v1'))
        self.assertEqual(self.scope({'peak_count': 118}, data_type='jade_index_data'), self.scope(value))

    def test_v2_index_scope_keeps_restored_metadata_and_does_not_claim_source_lacks_it(self):
        candidate = {'peak_count': 198, 'space_group': 'Pmmn',
                     'cell_parameters': {'a': '11.512', 'b': '3.564', 'c': '4.368'}}
        result = self.scope(candidate, reader_profile='jade_pdf4_2009_index_v2')
        self.assertEqual(result['report_type_label'], '索引报告')
        self.assertIn('晶体学参数', result['report_scope_detail'])
        self.assertNotIn('未提供晶胞', result['report_scope_detail'])
        self.assertNotIn('未读取晶胞', result['report_scope_detail'])
        self.assertNotIn('缺少晶胞', result['report_scope_detail'])

    def test_v1_scope_says_not_read_instead_of_claiming_original_source_missing(self):
        result = self.scope({'peak_count': 199}, reader_profile='jade_pdf4_2009_index_v1')
        self.assertIn('尚未读取', result['report_scope_detail'])
        self.assertNotIn('未提供晶胞', result['report_scope_detail'])

    def test_index_limit_warns_only_for_verified_integer_peak_count(self):
        for count, truncated in ((198, False), (199, True), (200, True),
                                 (None, False), ('199', False), (True, False), (-1, False)):
            with self.subTest(count=count):
                result = self.scope({'data_type': 'jade_index_data', 'peak_count': count})
                self.assertIs(result['possible_truncation'], truncated)
                self.assertEqual('可能截断' in result['report_scope_detail'], truncated)

    def test_hsrdb_long_peak_table_is_not_an_index_truncation(self):
        result = self.scope({'peak_count': 300, 'possible_truncation': True}, reader_profile='pdf2_2.3')
        self.assertEqual(result['report_type_label'], '数据库报告')
        self.assertFalse(result['possible_truncation'])

    def test_structure_is_distinct_and_mentions_separate_cif(self):
        for candidate, settings in (({'content_type': 'structure'}, {}),
                                    ({'data_type': 'jade_structure_data'}, {}),
                                    ({}, {'reader_profile': 'jade_icsd_structure_v1'})):
            with self.subTest(candidate=candidate, settings=settings):
                result = self.scope(candidate, **settings)
                self.assertEqual(result['report_type_label'], '晶体结构报告')
                self.assertIn('CIF', result['report_scope_detail'])
                self.assertFalse(result['possible_truncation'])

    def test_database_metadata_remains_intact_and_scope_never_claims_missing_values(self):
        candidate = {'peak_count': 62, 'space_group': 'Pmmn',
                     'cell_parameters': {'a': '11.516', 'b': '3.5656', 'c': '4.3727'},
                     'literature': [{'c4journal': 'Acta Crystallogr.'}]}
        original = copy.deepcopy(candidate)
        result = self.scope(candidate, reader_profile='pdf2_2.3')
        self.assertEqual(candidate, original)
        self.assertNotIn('未提供晶胞', result['report_scope_detail'])
        self.assertEqual(result['report_type_label'], '数据库报告')

    def test_display_names_and_card_codes_cannot_forge_source_type(self):
        result = self.scope({'source': 'PDF-4+ 2009（索引）', 'code': '04-007-0398',
                             'source_id': 'not-a-trusted-index-profile', 'peak_count': 199})
        self.assertEqual(result['report_type_label'], '数据库报告')
        self.assertFalse(result['possible_truncation'])

    def test_output_contains_no_arbitrary_source_text(self):
        result = self.scope({'source': '<script>unsafe</script>', 'support_message': 'internal-secret',
                             'cell_parameters': None})
        self.assertNotIn('unsafe', str(result))
        self.assertNotIn('internal-secret', str(result))


if __name__ == '__main__':
    unittest.main()
