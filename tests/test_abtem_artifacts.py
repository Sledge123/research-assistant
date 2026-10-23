"""真实小端浮点结果包的跨格式与身份回归，不依赖科学库。"""
import hashlib
import io
import json
import struct
import unittest
import warnings
import zipfile

from highscore_abtem.artifacts import build_bundle, validate_bundle
from highscore_abtem.contract import DEFAULT_PARAMETERS, validate_parameters


INPUT = b'data_synthetic\n_cell_length_a 10\n'


def bundle():
    parameters = dict(DEFAULT_PARAMETERS)
    model = {'cell_angstrom': [10, 12, 20], 'symbols': ['Si'], 'fractional_positions': [[0.1, 0.2, 0.3]],
             'pbc': [True, True, False], 'zone_axis': [0, 0, 1], 'occupancy': 'full', 'thermal_model': 'static'}
    data = struct.pack('<64f', *(index / 64 for index in range(64)))
    measurements = {
        'hrtem': {'shape': [8, 8], 'data': data, 'x_start': 0, 'y_start': 0,
                  'x_step': 10 / 8, 'y_step': 12 / 8, 'coordinate_unit': 'angstrom'},
        'saed': {'shape': [8, 8], 'data': data, 'x_start': -4 / 10, 'y_start': -4 / 12,
                 'x_step': 1 / 10, 'y_step': 1 / 12, 'coordinate_unit': '1/angstrom'}}
    return build_bundle(INPUT, parameters, model, measurements, {'abtem': '1.0.10'},
                        {'seconds': 1.0, 'peak_rss_bytes': 1048576})


def changed(original, name, transform, *, rehash=True):
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        files = {item.filename: archive.read(item) for item in archive.infolist()}
    files[name] = transform(files[name])
    if rehash and name != 'receipt.json':
        receipt = json.loads(files['receipt.json'])
        receipt['files'][name] = {'bytes': len(files[name]), 'sha256': hashlib.sha256(files[name]).hexdigest()}
        files['receipt.json'] = json.dumps(receipt, ensure_ascii=False).encode('utf-8')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


class AbtemArtifactTests(unittest.TestCase):
    def test_complete_flat_bundle_and_input_unchanged(self):
        checked = validate_bundle(bundle(), INPUT, DEFAULT_PARAMETERS)
        self.assertEqual(len(checked['files']), 13)
        self.assertEqual(checked['files']['input.cif'], INPUT)
        self.assertTrue(all('/' not in name and '\\' not in name for name in checked['files']))
        self.assertTrue(checked['files']['hrtem.tiff'].startswith(b'II*\x00'))
        self.assertTrue(checked['files']['hrtem.npy'].startswith(b'\x93NUMPY'))
        self.assertEqual(len(checked['files']['hrtem.csv'].splitlines()), 65)

    def test_csv_changed_with_recomputed_hash_still_rejected(self):
        bad = changed(bundle(), 'hrtem.csv', lambda value: value.replace(b',0.5\n', b',0.6\n'))
        self.assertNotEqual(bad, bundle())
        with self.assertRaisesRegex(ValueError, '数值不一致'):
            validate_bundle(bad, INPUT, DEFAULT_PARAMETERS)

    def test_tiff_payload_changed_with_recomputed_hash_still_rejected(self):
        bad = changed(bundle(), 'saed.tiff', lambda value: value[:-4] + struct.pack('<f', 0.1))
        with self.assertRaises(ValueError):
            validate_bundle(bad, INPUT, DEFAULT_PARAMETERS)

    def test_npy_nonfinite_or_negative_values_rejected(self):
        for value in (float('nan'), float('inf'), -1.0):
            bad = changed(bundle(), 'saed.npy', lambda data: data[:-4] + struct.pack('<f', value))
            with self.assertRaises(ValueError):
                validate_bundle(bad, INPUT, DEFAULT_PARAMETERS)

    def test_input_parameter_and_model_binding(self):
        original = bundle()
        with self.assertRaises(ValueError):
            validate_bundle(original, INPUT + b'# other\n', DEFAULT_PARAMETERS)
        with self.assertRaises(ValueError):
            validate_bundle(original, INPUT, {**DEFAULT_PARAMETERS, 'defocus_nm': 20})
        bad = changed(original, 'model.cif', lambda value: value.replace(b' 1\n', b' 0.5\n'))
        with self.assertRaises(ValueError):
            validate_bundle(bad, INPUT, DEFAULT_PARAMETERS)

    def test_duplicate_or_path_file_rejected(self):
        for filename in ('input.cif', '../input.cif'):
            output = io.BytesIO(bundle())
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                with zipfile.ZipFile(output, 'a') as archive:
                    archive.writestr(filename, b'other')
            with self.assertRaises(ValueError):
                validate_bundle(output.getvalue(), INPUT, DEFAULT_PARAMETERS)

    def test_coordinate_unit_and_grid_consistency(self):
        def wrong_unit(value):
            data = json.loads(value)
            data['coordinates']['saed']['coordinate_unit'] = 'angstrom'
            return json.dumps(data).encode('utf-8')
        with self.assertRaises(ValueError):
            validate_bundle(changed(bundle(), 'receipt.json', wrong_unit), INPUT, DEFAULT_PARAMETERS)

    def test_parameters_reject_nonfinite_boolean_unknown_or_zero_direction(self):
        for value in ({'energy_kev': True}, {'energy_kev': float('nan')}, {'zone_axis': [0, 0, 0]},
                      {'zone_axis': [True, 0, 1]}, {'python': 'anything'}):
            with self.assertRaises(ValueError):
                validate_parameters(value)
        self.assertEqual(validate_parameters({'zone_axis': [0, 0, 2]})['zone_axis'], [0, 0, 1])
