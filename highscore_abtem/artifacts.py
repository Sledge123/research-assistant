"""科学结果平铺包；不加载 NumPy，独立交叉核对数值与四种导出。"""
import ast
import csv
import hashlib
import io
import json
import math
import re
import struct
import zipfile
import zlib

from .contract import ABTEM_VERSION, MAX_ATOMS, MAX_GRID, MAX_LATERAL_ANGSTROM, PROFILE_VERSION, validate_cif_bytes, validate_parameters

MAX_BUNDLE_BYTES = 12 * 1024 * 1024
MAX_UNPACKED_BYTES = 24 * 1024 * 1024
MAX_FILE_BYTES = 6 * 1024 * 1024
FILES = {'input.cif', 'model.cif', 'model.json', 'parameters.json', 'receipt.json'} | {
    kind + '.' + extension for kind in ('saed', 'hrtem') for extension in ('npy', 'csv', 'tiff', 'png')}
SYMBOLS = set(('H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn '
               'Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce '
               'Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn '
               'Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og').split())


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('结果JSON包含重复字段。')
        result[key] = value
    return result


def _load(content):
    if len(content) > 2 * 1024 * 1024:
        raise ValueError('结果元数据超出限制。')
    return json.loads(content.decode('utf-8'), object_pairs_hook=_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('结果含非有限值。')))


def _number(value, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError('结果数值无效。')
    return float(value)


def _model(model, parameters):
    if not isinstance(model, dict):
        raise ValueError('缺少实际原子模型。')
    lengths = model.get('cell_angstrom')
    symbols, positions = model.get('symbols'), model.get('fractional_positions')
    if not isinstance(lengths, list) or len(lengths) != 3:
        raise ValueError('实际模拟晶胞无效。')
    lengths = [_number(item, positive=True) for item in lengths]
    if max(lengths[:2]) > MAX_LATERAL_ANGSTROM + 1e-7 or not math.isclose(lengths[2], parameters['thickness_nm'] * 10, rel_tol=1e-9):
        raise ValueError('实际晶胞尺寸与任务参数不一致。')
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= MAX_ATOMS or any(item not in SYMBOLS for item in symbols):
        raise ValueError('实际模型元素或原子数量无效。')
    if not isinstance(positions, list) or len(positions) != len(symbols):
        raise ValueError('实际模型坐标数量不一致。')
    for position in positions:
        if not isinstance(position, list) or len(position) != 3 or any(not -1e-8 <= _number(item) < 1.00000001 for item in position):
            raise ValueError('实际模型分数坐标无效。')
    if model.get('pbc') != [True, True, False] or model.get('zone_axis') != parameters['zone_axis']:
        raise ValueError('模型方向或边界条件与任务不一致。')
    if model.get('occupancy') != 'full' or model.get('thermal_model') != 'static':
        raise ValueError('当前只支持有序、满占有的静态晶体。')
    # 拒绝藏入非有限元数据；变换与原晶胞作为可追溯数值保留。
    _json(model)
    return lengths, symbols, positions


def model_cif(model, parameters):
    lengths, symbols, positions = _model(model, parameters)
    lines = ['data_abtem_simulation_cell', '# Derived finite specimen; periodic in x/y only; see model.json.',
             '_cell_length_a %.12g' % lengths[0], '_cell_length_b %.12g' % lengths[1],
             '_cell_length_c %.12g' % lengths[2], '_cell_angle_alpha 90', '_cell_angle_beta 90',
             '_cell_angle_gamma 90', "_space_group_name_H-M_alt 'P 1'", '_space_group_IT_number 1',
             'loop_', '_space_group_symop_operation_xyz', "'x,y,z'", 'loop_',
             '_atom_site_label', '_atom_site_type_symbol', '_atom_site_fract_x',
             '_atom_site_fract_y', '_atom_site_fract_z', '_atom_site_occupancy']
    for index, (symbol, position) in enumerate(zip(symbols, positions), 1):
        lines.append('%s%d %s %.12g %.12g %.12g 1' % (symbol, index, symbol, *position))
    return ('\n'.join(lines) + '\n').encode('utf-8')


def _array(shape, data):
    if (not isinstance(shape, (list, tuple)) or len(shape) != 2
            or any(type(item) is not int or not 8 <= item <= MAX_GRID for item in shape)):
        raise ValueError('模拟数组维度超出限制。')
    rows, cols = shape
    if not isinstance(data, bytes) or len(data) != rows * cols * 4:
        raise ValueError('浮点强度数组长度不一致。')
    values = [item[0] for item in struct.iter_unpack('<f', data)]
    if any(not math.isfinite(item) or item < 0 for item in values) or not any(item > 0 for item in values):
        raise ValueError('模拟强度必须为有限、非负且非全零的浮点数。')
    return rows, cols, values


def _npy(shape, data):
    header = repr({'descr': '<f4', 'fortran_order': False, 'shape': tuple(shape)})
    padding = (64 - ((10 + len(header) + 1) % 64)) % 64
    header = (header + ' ' * padding + '\n').encode('ascii')
    return b'\x93NUMPY\x01\x00' + struct.pack('<H', len(header)) + header + data


def _read_npy(content):
    if not content.startswith(b'\x93NUMPY\x01\x00') or len(content) < 10:
        raise ValueError('NPY格式无效。')
    length = struct.unpack('<H', content[8:10])[0]
    if length > 1024 or 10 + length > len(content):
        raise ValueError('NPY头部无效。')
    header = ast.literal_eval(content[10:10 + length].decode('ascii').strip())
    if not isinstance(header, dict) or set(header) != {'descr', 'fortran_order', 'shape'} or header['descr'] != '<f4' or header['fortran_order'] is not False:
        raise ValueError('NPY只接受小端float32二维数组。')
    data = content[10 + length:]
    rows, cols, values = _array(header['shape'], data)
    return [rows, cols], data, values


def _coordinates(kind, item, lengths):
    required = {'shape', 'x_start', 'y_start', 'x_step', 'y_step', 'coordinate_unit'}
    if set(item) != required:
        raise ValueError('强度坐标字段无效。')
    rows, cols = item['shape']
    x_step, y_step = _number(item['x_step'], positive=True), _number(item['y_step'], positive=True)
    x_start, y_start = _number(item['x_start']), _number(item['y_start'])
    if kind == 'saed':
        expected = (1 / lengths[0], 1 / lengths[1], -(cols // 2) * x_step, -(rows // 2) * y_step, '1/angstrom')
    else:
        expected = (lengths[0] / cols, lengths[1] / rows, 0.0, 0.0, 'angstrom')
    if item['coordinate_unit'] != expected[4] or any(not math.isclose(value, wanted, rel_tol=1e-8, abs_tol=1e-10)
            for value, wanted in zip((x_step, y_step, x_start, y_start), expected[:4])):
        raise ValueError('强度坐标与实际晶胞或单位不一致。')


def _csv(shape, values, axes):
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(('x', 'y', 'intensity'))
    rows, cols = shape
    for row in range(rows):
        for column in range(cols):
            writer.writerow(('%.12g' % (axes['x_start'] + column * axes['x_step']),
                             '%.12g' % (axes['y_start'] + row * axes['y_step']),
                             '%.9g' % values[row * cols + column]))
    return output.getvalue().encode('ascii')


def _pixels(kind, values):
    if kind == 'saed':
        maximum = max(values)
        mapped = [math.log1p(item / maximum * 10000) for item in values]
    else:
        mapped = values
    lower, upper = min(mapped), max(mapped)
    if upper <= lower:
        return bytes(len(values))
    return bytes(max(0, min(255, int((item - lower) / (upper - lower) * 255 + 0.5))) for item in mapped)


def _chunk(name, content):
    return struct.pack('>I', len(content)) + name + content + struct.pack('>I', zlib.crc32(name + content) & 0xffffffff)


def _png(shape, pixels):
    rows, cols = shape
    scanlines = b''.join(b'\x00' + pixels[row * cols:(row + 1) * cols] for row in range(rows))
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', struct.pack('>IIBBBBB', cols, rows, 8, 0, 0, 0, 0))
            + _chunk(b'IDAT', zlib.compress(scanlines, 6)) + _chunk(b'IEND', b''))


def _check_png(content, shape, pixels):
    if not content.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('PNG格式无效。')
    offset, chunks = 8, []
    while offset < len(content):
        if offset + 12 > len(content):
            raise ValueError('PNG截断。')
        length = struct.unpack('>I', content[offset:offset + 4])[0]
        name = content[offset + 4:offset + 8]
        payload = content[offset + 8:offset + 8 + length]
        if offset + length + 12 > len(content) or struct.unpack('>I', content[offset + 8 + length:offset + 12 + length])[0] != (zlib.crc32(name + payload) & 0xffffffff):
            raise ValueError('PNG摘要无效。')
        chunks.append((name, payload)); offset += length + 12
    rows, cols = shape
    if [name for name, _ in chunks] != [b'IHDR', b'IDAT', b'IEND'] or chunks[0][1] != struct.pack('>IIBBBBB', cols, rows, 8, 0, 0, 0, 0) or chunks[2][1]:
        raise ValueError('PNG尺寸或像素类型不一致。')
    decoder = zlib.decompressobj()
    scanlines = decoder.decompress(chunks[1][1], rows * (cols + 1) + 1)
    expected = b''.join(b'\x00' + pixels[row * cols:(row + 1) * cols] for row in range(rows))
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or scanlines != expected:
        raise ValueError('PNG与原始强度显示映射不一致。')


def _tiff(shape, data):
    rows, cols = shape
    # TIFF 6.0，未压缩、小端、单通道 IEEE float32，单 strip。
    tags = [(256, 4, cols), (257, 4, rows), (258, 3, 32), (259, 3, 1),
            (262, 3, 1), (273, 4, 8 + 2 + 10 * 12 + 4), (277, 3, 1),
            (278, 4, rows), (279, 4, len(data)), (339, 3, 3)]
    output = bytearray(b'II*\x00' + struct.pack('<I', 8) + struct.pack('<H', len(tags)))
    for tag, kind, value in tags:
        output.extend(struct.pack('<HHII', tag, kind, 1, value))
    output.extend(struct.pack('<I', 0)); output.extend(data)
    return bytes(output)


def _parameter_file(parameters):
    return _json({'profile_version': PROFILE_VERSION, 'parameters': parameters,
                  'physics': {'potential': 'lobato', 'model': 'static_iam',
                              'thermal_motion': 'not_included', 'intensity': 'relative_incident_plane_wave'}})


def build_bundle(input_cif, parameters, model, measurements, versions, runtime):
    validate_cif_bytes(input_cif)
    parameters = validate_parameters(parameters)
    lengths, _, _ = _model(model, parameters)
    if set(measurements) != {'saed', 'hrtem'}:
        raise ValueError('必须同时生成电子衍射和HRTEM。')
    files = {'input.cif': input_cif, 'model.json': _json(model), 'model.cif': model_cif(model, parameters),
             'parameters.json': _parameter_file(parameters)}
    coordinates = {}
    for kind in ('saed', 'hrtem'):
        value = measurements[kind]
        shape, data = list(value['shape']), value['data']
        _, _, values = _array(shape, data)
        axes = {key: item for key, item in value.items() if key != 'data'}
        axes['shape'] = shape
        _coordinates(kind, axes, lengths)
        coordinates[kind] = axes
        files[kind + '.npy'] = _npy(shape, data)
        files[kind + '.csv'] = _csv(shape, values, axes)
        files[kind + '.tiff'] = _tiff(shape, data)
        files[kind + '.png'] = _png(shape, _pixels(kind, values))
    receipt = {'schema': 1, 'profile_version': PROFILE_VERSION, 'input_sha256': hashlib.sha256(input_cif).hexdigest(),
               'parameters': parameters, 'model': {'atom_count': len(model['symbols']), 'cell_angstrom': lengths},
               'coordinates': coordinates, 'versions': versions, 'runtime': runtime,
               'scientific_type': 'static_iam_simulation',
               'files': {name: {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                         for name, content in sorted(files.items())}}
    files['receipt.json'] = _json(receipt)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)
    result = output.getvalue()
    validate_bundle(result, input_cif, parameters)
    return result


def validate_bundle(bundle, input_cif, parameters):
    """任何一项不一致均拒绝，成功返回可展示文件与可审计元数据。"""
    try:
        validate_cif_bytes(input_cif)
        parameters = validate_parameters(parameters)
        if not isinstance(bundle, bytes) or not bundle or len(bundle) > MAX_BUNDLE_BYTES:
            raise ValueError('模拟结果包大小无效。')
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            members = archive.infolist()
            if len(members) != len(FILES) or {item.filename for item in members} != FILES:
                raise ValueError('模拟结果文件缺失、重复或包含额外文件。')
            if sum(item.file_size for item in members) > MAX_UNPACKED_BYTES:
                raise ValueError('模拟结果展开大小超出限制。')
            files = {}
            for item in members:
                if item.is_dir() or item.file_size > MAX_FILE_BYTES or item.flag_bits & 1 or (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('模拟结果包含不支持的文件。')
                if item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError('模拟结果压缩格式不支持。')
                files[item.filename] = archive.read(item)
        if files['input.cif'] != input_cif or files['parameters.json'] != _parameter_file(parameters):
            raise ValueError('模拟结果不属于本次输入与参数。')
        receipt = _load(files['receipt.json'])
        if (receipt.get('schema') != 1 or receipt.get('profile_version') != PROFILE_VERSION
                or receipt.get('scientific_type') != 'static_iam_simulation'
                or receipt.get('input_sha256') != hashlib.sha256(input_cif).hexdigest()
                or receipt.get('parameters') != parameters):
            raise ValueError('模拟结果身份不一致。')
        hashes = receipt.get('files')
        if not isinstance(hashes, dict) or set(hashes) != FILES - {'receipt.json'}:
            raise ValueError('模拟结果摘要清单无效。')
        for name in hashes:
            content = files[name]
            if hashes[name] != {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}:
                raise ValueError('模拟结果文件摘要不一致。')
        model = _load(files['model.json'])
        lengths, symbols, _ = _model(model, parameters)
        if files['model.cif'] != model_cif(model, parameters) or receipt.get('model') != {'atom_count': len(symbols), 'cell_angstrom': lengths}:
            raise ValueError('模拟模型的JSON/CIF不一致。')
        versions = receipt.get('versions')
        if not isinstance(versions, dict) or versions.get('abtem') != ABTEM_VERSION or any(not isinstance(value, str) or len(value) > 80 for value in versions.values()):
            raise ValueError('模拟软件版本记录无效。')
        runtime = receipt.get('runtime')
        if not isinstance(runtime, dict) or _number(runtime.get('seconds')) < 0 or type(runtime.get('peak_rss_bytes')) is not int or not 0 < runtime['peak_rss_bytes'] <= 640 * 1024 * 1024:
            raise ValueError('模拟运行回执无效。')
        _json(receipt)
        coordinates = receipt.get('coordinates')
        if not isinstance(coordinates, dict) or set(coordinates) != {'saed', 'hrtem'}:
            raise ValueError('模拟强度缺少坐标。')
        for kind in ('saed', 'hrtem'):
            shape, data, values = _read_npy(files[kind + '.npy'])
            axes = coordinates[kind]
            if not isinstance(axes, dict) or axes.get('shape') != shape:
                raise ValueError('模拟强度尺寸不一致。')
            _coordinates(kind, axes, lengths)
            # 逐值重建精确 float32 导出，禁止CSV被改后只重算文件摘要。
            expected_csv = _csv(shape, values, axes)
            if files[kind + '.csv'] != expected_csv or files[kind + '.tiff'] != _tiff(shape, data):
                raise ValueError('NPY/CSV/TIFF数值不一致。')
            _check_png(files[kind + '.png'], shape, _pixels(kind, values))
        return {'metadata': receipt, 'files': files}
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, OverflowError, struct.error,
            zipfile.BadZipFile, RuntimeError, zlib.error, SyntaxError, RecursionError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError('模拟结果格式或交叉复验失败。') from None
