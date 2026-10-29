"""在锁定独立科学环境中执行真实结构、XRD与格式数值回归。"""
import importlib.util
import io
import json
import unittest

from highscore_pymatgen.contract import validate_parameters
from tests.test_pymatgen_contract import CIF, POSCAR


def silicon_cif():
    sites = [(0, 0, 0), (0, .5, .5), (.5, 0, .5), (.5, .5, 0),
             (.25, .25, .25), (.25, .75, .75), (.75, .25, .75), (.75, .75, .25)]
    header = CIF.decode().split('Si1 Si')[0].replace('5.43', '5.431')
    return (header + '\n'.join('Si%d Si %.12g %.12g %.12g' % (index, *point)
                              for index, point in enumerate(sites, 1)) + '\n').encode()


@unittest.skipUnless(importlib.util.find_spec('pymatgen') is not None, '仅在锁定独立科学环境运行')
class PymatgenScientificTests(unittest.TestCase):
    def execute(self, operation, inputs=None, parameters=None):
        from highscore_pymatgen.artifacts import validate_bundle
        from highscore_pymatgen.core import run
        inputs = inputs or [{'filename': 'silicon.cif', 'content': silicon_cif()}]
        parameters = parameters or {}
        bundle = run(inputs, operation, parameters)
        return validate_bundle(bundle, inputs, operation, parameters)

    def test_silicon_xrd_111_angle_and_200_extinction(self):
        result = self.execute('xrd')
        details = result['result']['details']
        peaks = details['peaks']
        self.assertAlmostEqual(peaks[0]['two_theta_degree'], 28.442, delta=.015)
        self.assertAlmostEqual(max(peak['intensity_relative_max100'] for peak in peaks), 100, places=9)
        self.assertTrue(any(family['hkl'] == [1, 1, 1] for family in peaks[0]['hkls']))
        self.assertFalse(any(sorted(map(abs, family['hkl'])) == [0, 0, 2] for peak in peaks for family in peak['hkls']))
        self.assertGreater(peaks[0]['intensity_raw_au'], 100)
        self.assertFalse(result['receipt']['independently_recomputed_intensities'])
        self.assertTrue(result['files']['xrd.png'].startswith(b'\x89PNG'))
        self.assertIn(b'<svg', result['files']['xrd.svg'])

    def test_silicon_200_and_222_extinction_only_intervals_do_not_normalize_roundoff(self):
        from pymatgen.analysis.diffraction.core import AbstractDiffractionPatternCalculator
        original_cutoff = AbstractDiffractionPatternCalculator.SCALED_INTENSITY_TOL
        for lower, upper in ((32, 34), (58, 60)):
            with self.subTest(interval=(lower, upper)):
                result = self.execute('xrd', parameters={'two_theta_min': lower, 'two_theta_max': upper})
                self.assertEqual(result['summary']['peak_count'], 0)
                audit = result['result']['details']['numerical_extinction_filter']
                self.assertGreater(audit['discarded_peak_count'], 0)
                self.assertEqual(audit['applied_relative_cutoff_percent'], 0)
                self.assertGreater(audit['maximum_amplitude_error_bound'], 0)
                for peak in audit['discarded_peaks']:
                    self.assertLessEqual(peak['numerical_resolution']['maximum_amplitude_to_error_bound_ratio'], 1)
                    self.assertLessEqual(peak['intensity_raw_au'], peak['numerical_resolution']['intensity_envelope_upper_bound_au'])
        self.assertEqual(AbstractDiffractionPatternCalculator.SCALED_INTENSITY_TOL, original_cutoff)

    def test_neighboring_allowed_reflections_and_actual_weak_peak_are_retained(self):
        for lower, upper in ((27, 30), (46, 49), (55, 57), (68, 70)):
            with self.subTest(interval=(lower, upper)):
                result = self.execute('xrd', parameters={'two_theta_min': lower, 'two_theta_max': upper})
                self.assertGreater(result['summary']['peak_count'], 0)
        # 极小的真实位置偏移解除200消光；即使相对强度远小于旧0.001%显示阈值也须保留。
        weak = silicon_cif().replace(b'Si1 Si 0 0 0', b'Si1 Si 0.000000001 0 0')
        result = self.execute('xrd', [{'filename': 'weak.cif', 'content': weak}])
        peaks = [peak for peak in result['result']['details']['peaks'] if 32 < peak['two_theta_degree'] < 34]
        self.assertEqual(len(peaks), 1)
        self.assertGreater(peaks[0]['intensity_raw_au'], 0)
        self.assertLess(peaks[0]['intensity_relative_max100'], .001)
        self.assertGreater(peaks[0]['numerical_resolution']['maximum_amplitude_to_error_bound_ratio'], 1000)
        self.assertEqual(result['files']['input.cif'], weak)

    def test_degenerate_cubic_metric_does_not_assume_representative_hkl_extinction(self):
        # P1几何只有100相消，010/001允许；同一d角桶内必须检查全部实际反射。
        content = CIF.replace(b'Si1 Si 0 0 0', b'Si1 Si 0 0 0\nSi2 Si 0.5 0 0')
        result = self.execute('xrd', [{'filename': 'p1.cif', 'content': content}],
                              {'two_theta_min': 15, 'two_theta_max': 18})
        self.assertEqual(result['summary']['peak_count'], 1)
        resolution = result['result']['details']['peaks'][0]['numerical_resolution']
        self.assertGreater(resolution['resolved_reflections'], 0)
        self.assertGreater(resolution['checked_reflections'], resolution['resolved_reflections'])

    def test_adjacent_engine_bins_cannot_rescue_extinct_reflection(self):
        content = CIF.replace(b'_cell_length_b 5.43', b'_cell_length_b 5.430002').replace(
            b'_cell_length_c 5.43', b'_cell_length_c 6').replace(
            b'Si1 Si 0 0 0', b'Si1 Si 0 0 0\nSi2 Si 0.5 0 0')
        result = self.execute('xrd', [{'filename': 'adjacent-bins.cif', 'content': content}],
                              {'two_theta_min': 15, 'two_theta_max': 18})
        peaks = result['result']['details']['peaks']
        self.assertEqual(len(peaks), 1)
        self.assertAlmostEqual(peaks[0]['two_theta_degree'], 16.31055334107, places=9)
        self.assertTrue(any(family['hkl'] == [0, 1, 0] for family in peaks[0]['hkls']))
        removed = result['result']['details']['numerical_extinction_filter']['discarded_peaks']
        self.assertEqual(len(removed), 1)
        self.assertAlmostEqual(removed[0]['two_theta_degree'], 16.31055938954, places=9)
        self.assertLess(abs(peaks[0]['two_theta_degree'] - removed[0]['two_theta_degree']), 1.1e-5)
        self.assertNotEqual(peaks[0]['numerical_resolution']['angular_bin_key'],
                            removed[0]['numerical_resolution']['angular_bin_key'])
        self.assertEqual(peaks[0]['numerical_resolution']['checked_reflections'], 1)
        self.assertEqual(removed[0]['numerical_resolution']['resolved_reflections'], 0)

    def test_hexagonal_four_indices_and_bragg_cross_check(self):
        content = CIF.replace(b'_cell_length_a 5.43', b'_cell_length_a 2.95').replace(
            b'_cell_length_b 5.43', b'_cell_length_b 2.95').replace(
            b'_cell_length_c 5.43', b'_cell_length_c 4.68').replace(
            b'_cell_angle_gamma 90', b'_cell_angle_gamma 120').replace(
            b'Si1 Si 0 0 0', b'Ti1 Ti 0 0 0\nTi2 Ti 0.666666666666667 0.333333333333333 0.5')
        result = self.execute('xrd', [{'filename': 'hexagonal.cif', 'content': content}])
        hkls = [family['hkl'] for peak in result['result']['details']['peaks'] for family in peak['hkls']]
        self.assertTrue(hkls)
        self.assertTrue(all(len(hkl) == 4 and hkl[0] + hkl[1] + hkl[2] == 0 for hkl in hkls))

    def test_analyze_silicon_and_preserve_input_declaration(self):
        result = self.execute('analyze')
        self.assertEqual(result['summary']['spacegroup_number'], 227)
        self.assertEqual(result['summary']['atom_count'], 8)
        original = result['result']['input_structures'][0]
        self.assertEqual(original['source']['symmetry_expansion']['number'], '1')
        self.assertEqual(result['files']['input.cif'], silicon_cif())

    def test_explicit_shifted_inversion_and_nondefault_setting(self):
        from highscore_pymatgen.core import load_structure
        content = CIF.replace(b'_space_group_IT_number 1', b"_space_group_IT_number 2\n_space_group_name_H-M_alt 'P -1'").replace(
            b"'x,y,z'", b"'x,y,z'\n'-x+1/2,-y,-z'").replace(b'Si1 Si 0 0 0', b'Si1 Si 0.1 0.2 0.3')
        model, source = load_structure(content, 'inversion.cif')
        import numpy as np
        self.assertEqual(len(model), 2)
        self.assertEqual(sorted(tuple(np.round(point, 7)) for point in model.frac_coords), [(0.1, .2, .3), (.4, .8, .7)])
        content = CIF.replace(b'_space_group_IT_number 1', b"_space_group_IT_number 14\n_space_group_name_H-M_alt 'P 21/n'").replace(
            b"loop_\n_space_group_symop_operation_xyz\n'x,y,z'\n", b'').replace(b'Si1 Si 0 0 0', b'Si1 Si 0.1 0.2 0.3')
        model, source = load_structure(content, 'setting.cif')
        self.assertEqual(len(model), 4)
        self.assertEqual(source['symmetry_expansion']['resolved_setting'], 2)
        self.assertEqual(source['symmetry_expansion']['hm_symbol'], 'P 21/n')

    def test_cif_and_poscar_roundtrip_and_rotated_cell_metric(self):
        result = self.execute('convert')
        self.assertTrue(result['result']['details']['roundtrip_verified'])
        for name in ('structure.cif', 'structure.poscar', 'sites.csv'):
            self.assertIn(name, result['files'])
        rotated = POSCAR.replace(b'5.43 0 0\n0 5.43 0', b'0 5.43 0\n-5.43 0 0')
        result = self.execute('convert', [{'filename': 'POSCAR', 'content': rotated}])
        self.assertEqual(result['result']['structures'][0]['lattice_matrix_angstrom'][0], [0, 5.43, 0])

    def test_poscar_decimal_rounding_boundary_is_a_valid_geometry_roundtrip(self):
        for coordinate in ('0.12345678500000001', '0.9999999998'):
            with self.subTest(coordinate=coordinate):
                content = POSCAR.replace(b'0 0 0\n', (coordinate + ' 0 0\n').encode())
                result = self.execute('convert', [{'filename': 'POSCAR', 'content': content}])
                self.assertTrue(result['result']['details']['roundtrip_verified'])
                self.assertEqual(result['files']['input.poscar'], content)
                self.assertEqual(result['receipt']['geometry_verification']['fractional_coordinate_absolute_tolerance'], 1e-10)

    def test_nonorthogonal_cell_roundoff_and_supercell_coordinates_use_same_tolerance(self):
        content = POSCAR.replace(b'5.43 0 0\n0 5.43 0\n0 0 5.43',
            b'5.430000000000001 0.12345678500000001 0.2\n0 5.41 0.17\n0 0 6.120000000000001').replace(
            b'0 0 0\n', b'0.12345678500000001 0.23456789500000003 0.34567891500000004\n')
        result = self.execute('supercell', [{'filename': 'POSCAR', 'content': content}], {'scaling': [2, 2, 1]})
        self.assertTrue(result['result']['details']['roundtrip_verified'])
        self.assertEqual(result['summary']['atom_count'], 4)
        original, expanded = result['result']['input_structures'][0], result['result']['structures'][0]
        self.assertAlmostEqual(expanded['volume_angstrom3'], original['volume_angstrom3'] * 4, places=8)

    def test_poscar_non_geometric_attributes_are_explicitly_outside_conversion_scope(self):
        content = POSCAR.replace(b'Direct\n0 0 0', b'Selective dynamics\nDirect\n0 0 0 T F T\n\n0.1 0.2 0.3')
        result = self.execute('convert', [{'filename': 'POSCAR', 'content': content}])
        source = result['result']['input_structures'][0]['source']
        self.assertEqual(source['conversion_scope'], ['elements', 'lattice', 'periodic_fractional_coordinates'])
        attributes = source['non_geometric_attributes']
        self.assertTrue(attributes['selective_dynamics_present'])
        self.assertIn('selective_dynamics', attributes['parsed_site_property_names'])
        self.assertFalse(attributes['selective_dynamics_applied'])
        self.assertFalse(attributes['velocities_parsed'])
        self.assertFalse(attributes['velocities_applied'])
        self.assertGreater(attributes['nonempty_trailing_lines_after_coordinates'], 0)
        self.assertEqual(result['files']['input.poscar'], content)
        self.assertNotIn(b'Selective', result['files']['structure.poscar'])
        self.assertFalse(result['result']['methods']['other_non_geometric_source_properties_transferred'])

    def test_supercell_volume_elements_and_coordinates(self):
        result = self.execute('supercell', parameters={'scaling': [2, 3, 1]})
        original, expanded = result['result']['input_structures'][0], result['result']['structures'][0]
        self.assertEqual(expanded['composition'], {'Si': 48})
        self.assertAlmostEqual(expanded['volume_angstrom3'], original['volume_angstrom3'] * 6, places=8)
        self.assertEqual(result['summary']['supercell'], [2, 3, 1])

    def test_derived_supercell_above_input_cell_length_keeps_valid_roundtrip(self):
        from highscore_pymatgen.core import ToolError, load_structure
        content = CIF.replace(b'5.43', b'100')
        result = self.execute('supercell', [{'filename': 'long.cif', 'content': content}], {'scaling': [3, 1, 1]})
        self.assertEqual(result['result']['structures'][0]['lattice_parameters'][0], 300)
        self.assertTrue(result['result']['details']['roundtrip_verified'])
        # 派生结果允许较长晶胞，不放宽上传输入和原abTEM的200Å上限。
        with self.assertRaises(ToolError):
            load_structure(result['files']['structure.cif'], 'derived.cif')

    def test_xrd_valid_empty_interval_has_empty_peak_exports(self):
        from unittest.mock import patch
        with patch('pymatgen.analysis.diffraction.xrd.XRDCalculator.get_pattern', side_effect=AssertionError('空倒易区间不调用引擎空max')):
            result = self.execute('xrd', parameters={'two_theta_min': 5, 'two_theta_max': 6})
        self.assertEqual(result['summary']['peak_count'], 0)
        self.assertEqual(result['result']['details']['peaks'], [])
        self.assertEqual(len(result['files']['xrd.csv'].decode().splitlines()), 1)
        self.assertTrue(result['files']['xrd.png'].startswith(b'\x89PNG'))

    def test_compare_same_element_changes_and_scale_are_distinct(self):
        inputs = [{'filename': 'a.cif', 'content': silicon_cif()}, {'filename': 'b.cif', 'content': silicon_cif()}]
        matched = self.execute('compare', inputs)
        self.assertTrue(matched['summary']['matched'])
        self.assertAlmostEqual(matched['result']['details']['rms_displacement_normalized'], 0, places=7)
        self.assertIn('a-input.cif', matched['files'])
        self.assertIn('b-structure.poscar', matched['files'])
        inputs[1]['content'] = silicon_cif().replace(b'Si', b'Ge')
        self.assertFalse(self.execute('compare', inputs)['summary']['matched'])
        inputs[1]['content'] = silicon_cif().replace(b'5.431', b'8.0')
        self.assertFalse(self.execute('compare', inputs)['summary']['matched'])

    def test_source_oxidation_information_is_preserved_for_element_comparison(self):
        inputs = [{'filename': 'a.cif', 'content': CIF.replace(b'Si1 Si ', b'Si1 Si4+ ')}, {'filename': 'b.cif', 'content': CIF}]
        result = self.execute('compare', inputs)
        self.assertTrue(result['summary']['matched'])
        self.assertEqual(result['result']['input_structures'][0]['source']['original_fields']['_atom_site_type_symbol'], ['Si4+'])

    def test_partial_disorder_and_unknown_setting_are_rejected(self):
        from highscore_pymatgen.core import ToolError, load_structure
        partial = CIF.replace(b'_atom_site_fract_z\n', b'_atom_site_fract_z\n_atom_site_occupancy\n').replace(b'Si1 Si 0 0 0', b'Si1 Si 0 0 0 0.5')
        with self.assertRaises(ToolError) as caught:
            load_structure(partial, 'partial.cif')
        self.assertEqual(caught.exception.code, 'unsupported_occupancy')
        disorder = CIF.replace(b'_atom_site_fract_z\n', b'_atom_site_fract_z\n_atom_site_disorder_group\n').replace(b'Si1 Si 0 0 0', b'Si1 Si 0 0 0 1')
        with self.assertRaises(ToolError) as caught:
            load_structure(disorder, 'disorder.cif')
        self.assertEqual(caught.exception.code, 'unsupported_disorder')
        unknown = CIF.replace(b'_space_group_IT_number 1', b'_space_group_IT_number 14').replace(
            b"loop_\n_space_group_symop_operation_xyz\n'x,y,z'\n", b'')
        with self.assertRaises(ToolError):
            load_structure(unknown, 'unknown.cif')

    def test_large_reciprocal_enumeration_is_rejected_before_calculation(self):
        from unittest.mock import patch
        from highscore_pymatgen.core import ToolError, run
        large = CIF.replace(b'5.43', b'200')
        with patch('pymatgen.analysis.diffraction.xrd.XRDCalculator.get_pattern', side_effect=AssertionError('禁止开始大枚举')):
            with self.assertRaises(ToolError) as caught:
                run([{'filename': 'large.cif', 'content': large}], 'xrd', {})
        self.assertEqual(caught.exception.code, 'unsupported_scale')

    def test_supercell_atom_limit_is_checked_before_expansion(self):
        from highscore_pymatgen.core import ToolError
        content = POSCAR.replace(b'5.43', b'16').replace(b'\nSi\n1\nDirect\n0 0 0',
            ('\nSi\n512\nDirect\n' + '\n'.join('%s %s %s' % (i / 8, j / 8, k / 8)
              for i in range(8) for j in range(8) for k in range(8))).encode())
        with self.assertRaises(ToolError) as caught:
            self.execute('supercell', [{'filename': 'POSCAR', 'content': content}], {'scaling': [4, 4, 4]})
        self.assertEqual(caught.exception.code, 'unsupported_scale')


if __name__ == '__main__':
    unittest.main()
