"""仅在锁定科学环境中核对真实CIF对称展开和数值导出。"""
import importlib.util
import io
import unittest

from highscore_abtem.contract import DEFAULT_PARAMETERS

TEMPLATE = '''data_synthetic_setting
_cell_length_a 8
_cell_length_b 8
_cell_length_c 8
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
{group}
{operations}
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
_atom_site_occupancy
Si1 Si 0.1 0.2 0.3 {occupancy}
'''


def cif(number=1, name='P 1', operations="loop_\n_space_group_symop_operation_xyz\n'x,y,z'", occupancy=1):
    return TEMPLATE.format(group="_space_group_IT_number %s\n_space_group_name_H-M_alt '%s'" % (number, name),
                           operations=operations, occupancy=occupancy).encode('ascii')


@unittest.skipUnless(importlib.util.find_spec('abtem') is not None, '仅在独立Linux科学环境运行')
class AbtemScientificTests(unittest.TestCase):
    def prepare(self, content, **values):
        from highscore_abtem.core import prepare_model
        return prepare_model(content, {**DEFAULT_PARAMETERS, 'thickness_nm': 0.8, **values})

    def test_explicit_shifted_inversion_is_not_doubled(self):
        import numpy as np
        atoms, model, _ = self.prepare(cif(2, 'P -1', "loop_\n_space_group_symop_operation_xyz\n'x,y,z'\n'-x+1/2,-y,-z'"))
        self.assertEqual(len(atoms), 2)
        self.assertEqual(sorted(tuple(np.round(value, 7)) for value in atoms.get_scaled_positions()),
                         [(0.1, 0.2, 0.3), (0.4, 0.8, 0.7)])
        self.assertEqual(model['symmetry_expansion']['operation_count'], 2)

    def test_nondefault_hm_symbol_setting_is_preserved(self):
        import numpy as np
        atoms, model, _ = self.prepare(cif(14, 'P 21/n', ''))
        self.assertEqual(model['symmetry_expansion']['resolved_setting'], 2)
        self.assertEqual(sorted(tuple(np.round(value, 7)) for value in atoms.get_scaled_positions()),
                         sorted([(0.1,0.2,0.3),(0.4,0.7,0.2),(0.9,0.8,0.7),(0.6,0.3,0.8)]))

    def test_incomplete_operation_set_and_ambiguous_number_only_rejected(self):
        from highscore_abtem.core import SimulationError
        with self.assertRaises(SimulationError):
            self.prepare(cif(1, 'P 1', "loop_\n_space_group_symop_operation_xyz\n'x,y,z'\n'x+1/3,y,z'"))
        ambiguous = cif(14, 'P 21/n', '').replace(b"_space_group_name_H-M_alt 'P 21/n'\n", b'')
        with self.assertRaises(SimulationError):
            self.prepare(ambiguous)
        with self.assertRaises(SimulationError):
            self.prepare(cif(2, 'P -1'))

    def test_partial_and_disordered_structures_rejected(self):
        from highscore_abtem.core import SimulationError
        with self.assertRaises(SimulationError) as partial:
            self.prepare(cif(occupancy=0.5))
        self.assertEqual(partial.exception.code, 'unsupported_occupancy')
        disordered = cif().replace(b'_atom_site_occupancy\n', b'_atom_site_occupancy\n_atom_site_disorder_group\n').replace(b'0.3 1\n', b'0.3 1 1\n')
        with self.assertRaises(SimulationError) as disorder:
            self.prepare(disordered)
        self.assertEqual(disorder.exception.code, 'unsupported_disorder')

    def test_rotated_model_cif_keeps_periodic_boundary_coordinates(self):
        import json
        import numpy as np
        from ase.build import bulk
        from ase.io import read, write
        from highscore_abtem.artifacts import model_cif
        buffer = io.BytesIO(); write(buffer, bulk('Si', a=5.43, cubic=True), format='cif')
        params = {**DEFAULT_PARAMETERS, 'zone_axis': [1, 1, 1]}
        _, model, _ = self.prepare(buffer.getvalue(), **params)
        reconstructed = read(io.BytesIO(model_cif(model, params)), format='cif')
        self.assertTrue(np.allclose(reconstructed.get_scaled_positions(), model['fractional_positions'], atol=1e-9))
        beam = np.asarray(params['zone_axis']) @ np.asarray(model['original_cell_angstrom'])
        beam /= np.linalg.norm(beam)
        self.assertGreater(float(np.dot(beam, np.asarray(model['orientation_basis_cartesian'])[2])), 1 - 1e-10)

    def test_finite_slab_never_counts_negative_boundary_atom(self):
        content = cif().replace(b'0.1 0.2 0.3 1', b'0.1 0.2 0.99999995 1')
        atoms, _, _ = self.prepare(content, thickness_nm=10.0)
        self.assertEqual(len(atoms), 12)
        self.assertGreaterEqual(float(atoms.positions[:, 2].min()), 0.0)
        self.assertLess(float(atoms.positions[:, 2].max()), 100.0)

    def test_real_numerical_arrays_read_with_independent_numpy_and_pillow(self):
        import numpy as np
        from PIL import Image
        from highscore_abtem.artifacts import validate_bundle
        from highscore_abtem.core import simulate
        parameters = {**DEFAULT_PARAMETERS, 'thickness_nm': 0.8}
        content = cif()
        checked = validate_bundle(simulate(content, parameters), content, parameters)
        for kind in ('saed', 'hrtem'):
            array = np.load(io.BytesIO(checked['files'][kind + '.npy']), allow_pickle=False)
            self.assertTrue(np.isfinite(array).all())
            self.assertTrue((array >= 0).all())
            with Image.open(io.BytesIO(checked['files'][kind + '.tiff'])) as image:
                self.assertTrue(np.array_equal(np.asarray(image), array))
