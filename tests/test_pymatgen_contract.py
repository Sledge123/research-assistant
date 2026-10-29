"""HTTP 可在没有科学依赖的环境验证任务与结果包。"""
import copy
import io
import json
import math
import unittest
import zipfile

from highscore_pymatgen.contract import validate_input_bytes, validate_inputs, validate_parameters


CIF = b'''data_test
_cell_length_a 5.43
_cell_length_b 5.43
_cell_length_c 5.43
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_space_group_IT_number 1
loop_
_space_group_symop_operation_xyz
'x,y,z'
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
Si1 Si 0 0 0
'''
POSCAR = b'''Si
1
5.43 0 0
0 5.43 0
0 0 5.43
Si
1
Direct
0 0 0
'''


class PymatgenContractTests(unittest.TestCase):
    def test_defaults_and_finite_parameter_validation(self):
        self.assertEqual(validate_parameters('xrd', {})['wavelength_angstrom'], 1.54056)
        self.assertEqual(validate_parameters('supercell', {})['scaling'], [2, 2, 2])
        for operation, value in [('xrd', {'two_theta_min': 90, 'two_theta_max': 10}),
                                 ('xrd', {'wavelength_angstrom': float('nan')}),
                                 ('supercell', {'scaling': [True, 2, 2]}),
                                 ('compare', {'scale': True}), ('compare', {'allow_subset': True}),
                                 ('convert', {'unknown': 1}), ('unknown', {})]:
            with self.subTest(operation=operation, value=value), self.assertRaises(ValueError):
                validate_parameters(operation, value)

    def test_input_count_size_and_explicit_poscar_elements(self):
        self.assertEqual(validate_input_bytes(CIF, '结构.cif'), 'cif')
        self.assertEqual(validate_input_bytes(POSCAR, 'POSCAR'), 'poscar')
        self.assertEqual(len(validate_inputs([{'filename': 'a.cif', 'content': CIF}], 'analyze')), 1)
        invalid = [(b'x' * (512 * 1024 + 1), 'a.cif'), (CIF, '../a.cif'),
                   (POSCAR.replace(b'\nSi\n1\nDirect', b'\n1\nDirect'), 'POSCAR'),
                   (CIF + b'\x00', 'a.cif')]
        for content, filename in invalid:
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                validate_input_bytes(content, filename)
        with self.assertRaises(ValueError):
            validate_inputs([{'filename': 'a.cif', 'content': CIF}], 'compare')

    def sample_bundle(self):
        from highscore_pymatgen.artifacts import build_bundle, report_text, structure_files
        from highscore_pymatgen.contract import PROFILE_VERSION, PYMATGEN_CORE_VERSION, PYMATGEN_VERSION
        inputs = [{'filename': 'a.cif', 'content': CIF}]
        model = {'name': 'structure', 'lattice_matrix_angstrom': [[5.43, 0, 0], [0, 5.43, 0], [0, 0, 5.43]],
                 'lattice_parameters': [5.43, 5.43, 5.43, 90.0, 90.0, 90.0], 'volume_angstrom3': 5.43 ** 3,
                 'symbols': ['Si'], 'fractional_positions': [[0.0, 0.0, 0.0]], 'atom_count': 1,
                 'composition': {'Si': 1}, 'formula': 'Si', 'occupancy': 'full', 'source': {}}
        result = {'profile_version': PROFILE_VERSION, 'operation': 'convert', 'parameters': {},
                  'input_structures': [model], 'structures': [model],
                  'summary': {'operation': 'convert', 'operation_label': '格式转换', 'formula': 'Si', 'atom_count': 1},
                  'details': {'roundtrip_verified': True},
                  'versions': {'pymatgen': PYMATGEN_VERSION, 'pymatgen-core': PYMATGEN_CORE_VERSION},
                  'runtime': {'seconds': 0.1}, 'methods': {}}
        files = structure_files(model)
        files['报告.txt'] = report_text(result)
        bundle = build_bundle(inputs, 'convert', {}, result, files)
        return inputs, bundle

    def test_bundle_binds_input_and_cross_checks_exports(self):
        from highscore_pymatgen.artifacts import validate_bundle
        inputs, bundle = self.sample_bundle()
        self.assertEqual(validate_bundle(bundle, inputs, 'convert', {})['summary']['atom_count'], 1)
        with self.assertRaises(ValueError):
            validate_bundle(bundle, [{'filename': 'a.cif', 'content': CIF + b'\n'}], 'convert', {})
        # 摘要重写也不能掩盖 CSV 和结构 JSON 间的数据不一致。
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            changed = {name: archive.read(name) for name in archive.namelist()}
        changed['sites.csv'] = changed['sites.csv'].replace(b'Si,0', b'Si,0.1', 1)
        import hashlib
        receipt = json.loads(changed['receipt.json'])
        receipt['files']['sites.csv'] = {'size': len(changed['sites.csv']), 'sha256': hashlib.sha256(changed['sites.csv']).hexdigest()}
        changed['receipt.json'] = json.dumps(receipt).encode()
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            for name, content in changed.items():
                archive.writestr(name, content)
        with self.assertRaises(ValueError):
            validate_bundle(output.getvalue(), inputs, 'convert', {})

    def test_archive_rejects_lzma_and_bzip2_before_payload_decompression(self):
        from highscore_pymatgen.artifacts import validate_bundle
        from unittest.mock import patch
        inputs, bundle = self.sample_bundle()
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        for compression in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w', compression=compression) as archive:
                for name, content in files.items():
                    archive.writestr(name, content)
            with patch.object(zipfile.ZipFile, 'read', side_effect=AssertionError('禁止先解压不受支持的算法')):
                with self.assertRaises(ValueError):
                    validate_bundle(output.getvalue(), inputs, 'convert', {})

    def test_near_degenerate_low_angle_hkls_use_engine_angular_tolerance(self):
        from highscore_pymatgen.artifacts import _check_xrd
        params = validate_parameters('xrd', {'two_theta_min': .1, 'two_theta_max': 2})
        from highscore_pymatgen.contract import extinction_error_recipe
        model = {'lattice_matrix_angstrom': [[100, 0, 0], [0, 100.0009, 0], [0, 0, 100]], 'atom_count': 1}
        details = {'symprec': 0, 'intensity_model': 'static_independent_atoms_lorentz_polarization',
                   'two_theta_merge_tolerance_degree': 1e-5, 'peak_retention_relative_to_range_maximum_percent': 0.0,
                   'numerical_extinction_filter': {'recipe': extinction_error_recipe(1), 'checked_reflections': 2,
                       'applied_relative_cutoff_percent': 0.0, 'considered_peaks': 1, 'discarded_peak_count': 0,
                       'discarded_peak_details_limit': 256, 'discarded_peak_details_truncated': False, 'discarded_peaks': [],
                       'minimum_amplitude_error_bound': 1e-12, 'maximum_amplitude_error_bound': 1e-12},
                   'peaks': [{'two_theta_degree': math.degrees(2 * math.asin(1.54056 / 200)),
                              'd_angstrom': 100, 'intensity_raw_au': 1000, 'intensity_relative_max100': 100,
                              'numerical_resolution': {'checked_reflections': 2, 'resolved_reflections': 2,
                                  'minimum_amplitude_error_bound': 1e-12, 'maximum_amplitude_error_bound': 1e-12,
                                  'maximum_amplitude_to_error_bound_ratio': 1e4, 'intensity_envelope_upper_bound_au': 1e-15},
                              'hkls': [{'hkl': [1, 0, 0], 'multiplicity': 2}, {'hkl': [0, 1, 0], 'multiplicity': 2}]}]}
        _check_xrd(details, model, params)
        # 两个近简并反射的d相对差超过旧固定阈值，但仍在引擎的同一角桶宽内。
        model['lattice_matrix_angstrom'][1][1] = 100.002
        with self.assertRaises(ValueError):
            _check_xrd(details, model, params)

    def test_rms_diagnostic_maximum_is_distinct_from_fit_minimax_criterion(self):
        from highscore_pymatgen.artifacts import _check_comparison
        params = validate_parameters('compare', {})
        # 另一组最小平均RMS配对可有超过stol的单点最大位移。
        details = {'matched': True, 'rms_displacement_normalized': math.sqrt((.35 ** 2 + 3 * .1 ** 2) / 4),
                   'max_displacement_normalized': .35}
        _check_comparison(details, [{'atom_count': 4}, {'atom_count': 4}], params)
        details['rms_displacement_normalized'] = .31
        with self.assertRaises(ValueError):
            _check_comparison(details, [{'atom_count': 4}, {'atom_count': 4}], params)
        details['rms_displacement_normalized'] = .1
        details['max_displacement_normalized'] = .9
        with self.assertRaises(ValueError):
            _check_comparison(details, [{'atom_count': 4}, {'atom_count': 4}], params)

    def test_geometry_matching_accepts_export_rounding_across_old_decimal_bucket(self):
        from highscore_pymatgen.artifacts import _same_geometry
        original = {'composition': {'Si': 1}, 'symbols': ['Si'],
                    'fractional_positions': [[0.12345678500000001, 0, 0]],
                    'lattice_matrix_angstrom': [[5.430000000000001, 0, 0], [0, 5.43, 0], [0, 0, 5.43]]}
        exported = copy.deepcopy(original)
        exported['fractional_positions'][0][0] = 0.123456785
        exported['lattice_matrix_angstrom'][0][0] = 5.43
        self.assertTrue(_same_geometry(original, exported))
        exported['fractional_positions'][0][0] += 1e-7
        self.assertFalse(_same_geometry(original, exported))
        exported = copy.deepcopy(original)
        exported['lattice_matrix_angstrom'][0][0] += 1e-6
        self.assertFalse(_same_geometry(original, exported))

    def test_periodic_sites_match_bijectively_across_boundary_and_preserve_elements(self):
        from highscore_pymatgen.artifacts import periodic_sites_equal
        tolerance = 1e-10
        self.assertTrue(periodic_sites_equal(['Si'], [[1 - 4e-11, 0, 0]], ['Si'], [[4e-11, 0, 0]]))
        # A0有两个邻居，A1只有B0：单纯贪心可能失败，双射算法须找到正确配对。
        a = [[.25, 0, 0], [.25 + 1.5 * tolerance, 0, 0]]
        b = [[.25 + .75 * tolerance, 0, 0], [.25 - .75 * tolerance, 0, 0]]
        self.assertTrue(periodic_sites_equal(['Si', 'Si'], a, ['Si', 'Si'], b))
        self.assertFalse(periodic_sites_equal(['Si', 'O'], a, ['O', 'Si'], a))
        self.assertFalse(periodic_sites_equal(['Si', 'Si'], [[0, 0, 0], [3e-10, 0, 0]],
                                            ['Si', 'Si'], [[0, 0, 0], [0, 0, 0]]))

    def test_periodic_matching_handles_4096_sites_and_bounds_dense_candidates(self):
        from highscore_pymatgen.artifacts import periodic_sites_equal
        a = [[index / 4096, 0, 0] for index in range(4096)]
        b = list(reversed([[(point[0] + 2e-12) % 1, 0, 0] for point in a]))
        self.assertTrue(periodic_sites_equal(['Si'] * 4096, a, ['Si'] * 4096, b))
        b[-1][0] += 1e-6
        self.assertFalse(periodic_sites_equal(['Si'] * 4096, a, ['Si'] * 4096, b))
        with self.assertRaises(ValueError):
            periodic_sites_equal(['Si'] * 4096, [[.1, 0, 0]] * 4096,
                                 ['Si'] * 4096, [[.1 + 1e-12, 0, 0]] * 4096)

    def test_http_bundle_geometry_accepts_roundoff_but_rejects_real_coordinate_change(self):
        from highscore_pymatgen.artifacts import build_bundle, report_text, structure_files, validate_bundle
        from highscore_pymatgen.contract import GEOMETRY_VERIFICATION
        _, original_bundle = self.sample_bundle()
        with zipfile.ZipFile(io.BytesIO(original_bundle)) as archive:
            result = json.loads(archive.read('result.json'))
        inputs = [{'filename': 'a.cif', 'content': CIF.replace(b'Si1 Si 0 0 0', b'Si1 Si 0.12345678500000001 0 0')}]
        result['input_structures'][0]['fractional_positions'][0][0] = 0.12345678500000001
        result['structures'][0]['fractional_positions'][0][0] = 0.123456785
        def bundle():
            files = structure_files(result['structures'][0])
            files['报告.txt'] = report_text(result)
            return build_bundle(inputs, 'convert', {}, result, files)
        checked = validate_bundle(bundle(), inputs, 'convert', {})
        self.assertEqual(checked['receipt']['geometry_verification'], GEOMETRY_VERIFICATION)
        self.assertEqual(checked['result']['methods']['geometry_verification'], GEOMETRY_VERIFICATION)
        self.assertEqual(checked['files']['input.cif'], inputs[0]['content'])
        result['structures'][0]['fractional_positions'][0][0] += 1e-7
        with self.assertRaises(ValueError):
            validate_bundle(bundle(), inputs, 'convert', {})


if __name__ == '__main__':
    unittest.main()
